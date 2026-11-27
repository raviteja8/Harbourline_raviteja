"""Small observable evaluator; reports evidence, never a hiring score."""

import argparse
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

import httpx

TRUSTED = Path(__file__).resolve().parents[1]
SCENARIOS = ("clean", "faulted-ingest", "wrong-key", "query-outage", "llm-outage", "llm-bad-output")
LOG_LIMIT = 1_000_000


class HarnessError(RuntimeError):
    pass


class BudgetExpired(RuntimeError):
    pass


class CandidateTimeout(RuntimeError):
    pass


def remaining_seconds(deadline: float | None, ceiling: float) -> float:
    remaining = ceiling if deadline is None else min(ceiling, deadline - time.monotonic())
    if remaining <= 0:
        raise BudgetExpired()
    return remaining


def free_port() -> int:
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


class Process:
    def __init__(self, command: list[str], cwd: Path, env: dict[str, str]):
        self.output = bytearray()
        self.overflow = False
        self.stopped = False
        self.process = subprocess.Popen(
            command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        while chunk := self.process.stdout.read1(4096):
            remaining = LOG_LIMIT - len(self.output)
            self.output.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self.overflow = True
                self.process.kill()
                break

    def wait(self, timeout: float) -> int:
        try:
            code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.stop()
            return 124
        self.reader.join(timeout=2)
        return code

    def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        if os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(self.process.pid, signal.SIGKILL)
            else:
                self.process.kill()
            self.process.wait(timeout=2)
        if os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.reader.join(timeout=2)
        if self.process.stdout is not None:
            self.process.stdout.close()


def environment(project: Path, store: Path, port: int, sim_port: int, key: str) -> dict[str, str]:
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR")
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    env.update({
        "PYTHONPATH": os.pathsep.join((str(project / "src"), str(project))),
        "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "PORT": str(port), "SIM_PORT": str(sim_port), "DATA_DIR": str(store),
        "EMBED_URL": f"http://127.0.0.1:{sim_port}", "EMBED_API_KEY": key,
        "SIM_API_KEY": "test-embed-key", "SIM_FAULTS": "",
    })
    return env


def wait_ready(process: Process, url: str, timeout: float = 30) -> bool:
    deadline = time.monotonic() + timeout
    with httpx.Client(trust_env=False, timeout=0.3) as client:
        while time.monotonic() < deadline and process.process.poll() is None:
            try:
                if client.get(url).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            threading.Event().wait(0.03)
    return False


def request(client: httpx.Client, port: int, path: str, question: dict, top_k: int = 5,
            deadline: float | None = None) -> dict:
    started = time.monotonic()
    timeout = remaining_seconds(deadline, 10)

    async def fetch() -> tuple[int, bytes]:
        async with asyncio.timeout(timeout), httpx.AsyncClient(trust_env=False, timeout=timeout) as bounded:
            async with bounded.stream(
                "POST", f"http://127.0.0.1:{port}{path}",
                headers={"Authorization": f"Bearer {question['token']}"},
                json={"query": question["query"], "top_k": top_k},
            ) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 2_000_000:
                        raise ValueError("candidate_response_too_large")
                return response.status_code, bytes(content)

    try:
        status, content = asyncio.run(fetch())
        body = json.loads(content)
        if not isinstance(body, dict):
            body = {}
        return {"status": status, "body": body,
                "seconds": round(time.monotonic() - started, 3)}
    except (httpx.HTTPError, ValueError, TimeoutError, RecursionError):
        return {"status": 0, "body": {}, "seconds": round(time.monotonic() - started, 3)}


def results(observation: dict) -> list[dict]:
    value = observation["body"].get("results", [])
    return [item for item in value if isinstance(item, dict)
            and all(isinstance(item.get(name), str) for name in ("doc_id", "chunk_id", "snippet"))
            and type(item.get("revision")) is int] if isinstance(value, list) else []


def error_code(observation: dict) -> str | None:
    error = observation["body"].get("error")
    return error.get("code") if isinstance(error, dict) else None


def identities(observation: dict) -> list[tuple]:
    return [(item.get("doc_id"), item.get("revision"), item.get("chunk_id"))
            for item in results(observation)]


def transport_observations(records: list[dict]) -> dict:
    previous = {}
    retries = []
    retry_after = []
    first_401 = next((index for index, item in enumerate(records) if item["status"] == 401), None)
    for item in records:
        key = (item.get("route"), item.get("key"))
        earlier = previous.get(key) if key[1] is not None else None
        if earlier:
            retries.append({"route": key[0], "previous_status": earlier["status"], "status": item["status"]})
            if earlier.get("retry_after") is not None and earlier.get("response_time") is not None:
                wait = item["arrival_time"] - earlier["response_time"]
                retry_after.append({"required_seconds": earlier["retry_after"],
                                    "observed_seconds": round(wait, 3),
                                    "respected": wait + 0.02 >= earlier["retry_after"]})
        previous[key] = item
    return {"requests": len(records), "responses_401": sum(item["status"] == 401 for item in records),
            "calls_after_first_401": len(records) - first_401 - 1 if first_401 is not None else 0,
            "identical_retries": retries, "retry_after": retry_after,
            "retry_after_violations": sum(not item["respected"] for item in retry_after),
            "note": "Timing tolerance 20ms; null keys cannot establish identical retries. Read code for unexercised paths."}


def model_stats(client: httpx.Client, sim_port: int) -> dict:
    response = client.get(f"http://127.0.0.1:{sim_port}/sim/stats")
    response.raise_for_status()
    return response.json()


def scenario_summary(report: dict) -> dict:
    state = report.get("state", "complete")
    failures = []
    review_flags = []
    operational = report.get("operational", {})
    controls = report.get("controls", []) + report.get("extra_questions", {}).get("controls", [])
    if any(item.get("cause", "candidate_limit") == "candidate_limit" for item in report.get("candidate_timeouts", [])):
        failures.append("candidate_timeout")
    if report.get("restricted_context_requests", 0) or any(item.get("restricted_context_requests", 0) for item in controls):
        failures.append("restricted_model_context_observed")
    if any(item.get("leak_observed") for item in controls):
        failures.append("unauthorized_response_observed")
    if state == "complete":
        if operational.get("ingest_exit") != 0:
            failures.append("initial_ingest_failed")
        if not operational.get("startup_ready"):
            failures.append("startup_failed")
        if any(not item.get("passed", False) for item in controls):
            failures.append("control_failed")
        if report.get("scenario") == "clean":
            persistence = operational.get("persistence", {})
            if not operational.get("authentication", {}).get("passed"):
                failures.append("authentication_check_failed")
            if not all(persistence.get(name) for name in ("observed", "repeat_consistent", "no_duplicate_selected_chunks")):
                failures.append("persistence_or_rerun_failed")
        else:
            if not operational.get("baseline_answer", {}).get("answer_present"):
                failures.append("baseline_answer_missing")
            if report.get("scenario") in {"faulted-ingest", "wrong-key"}:
                if not operational.get("expected_exit_observed") or not operational.get("previous_complete_results_preserved"):
                    failures.append("fault_ingest_policy_failed")
                if any(not item.get("passed") for item in operational.get("wrong_key_query", [])):
                    failures.append("wrong_key_query_failed")
    logs = report.get("logs", {})
    if logs.get("canary_matches") or logs.get("truncated"):
        failures.append("unsafe_or_excessive_logs")
    stages = operational.get("ingest_attempts", []) + operational.get("api_calls", [])
    if any(item.get("transport", {}).get("retry_after_violations", 0) for item in stages):
        failures.append("retry_after_violation")
    if any(item.get("transport", {}).get("calls_after_first_401", 0) for item in operational.get("ingest_attempts", [])):
        failures.append("ingest_calls_after_401")
    fault_attempts = operational.get("ingest_attempts", [])[1:]
    injected = operational.get("simulator_stats", {}).get("faults_injected", {})
    if (state == "complete" and report.get("scenario") == "faulted-ingest" and fault_attempts
            and any(injected.get(name, 0) for name in ("rate_limit", "slow", "bad_response"))
            and not any(item.get("transport", {}).get("identical_retries") for item in fault_attempts)):
        review_flags.append("faulted_ingest_no_retries_observed")
    if state == "harness_error" or report.get("harness_error"):
        status = "unavailable"
    elif failures:
        status = "fail"
    elif review_flags:
        status = "incomplete"
    elif state == "complete":
        status = "pass"
    elif state.startswith("not_run"):
        status = "not_run"
    else:
        status = "incomplete"
    measurement = report.get("measurement") or {}
    return {"scenario": report.get("scenario"), "status": status, "reasons": failures + review_flags,
            "hits": measurement.get("hits"), "retrieval_questions": measurement.get("retrieval_questions"),
            "controls_passed": sum(item.get("passed", False) for item in controls),
            "controls_total": len(controls),
            "note": "Observed checks only, not a rating or automatic safety verdict; retrieval quality has no cutoff."}


def citation_observation(observation: dict) -> dict:
    body = observation["body"]
    citations = body.get("citations", [])
    mapping = {(item.get("doc_id"), item.get("revision"), item.get("chunk_id")): item
               for item in results(observation)}
    valid = isinstance(citations, list)
    quotes = []
    if valid:
        for citation in citations:
            if not isinstance(citation, dict):
                valid = False
                break
            key = (citation.get("doc_id"), citation.get("revision"), citation.get("chunk_id"))
            quote = citation.get("quote")
            if (
                not isinstance(key[0], str)
                or type(key[1]) is not int
                or not isinstance(key[2], str)
                or key not in mapping
                or not isinstance(quote, str)
                or not 1 <= len(quote) <= 500
            ):
                valid = False
                break
            quotes.append(quote)
    answer = body.get("answer")
    valid = valid and len(quotes) <= 3 and (
        (answer is None and not quotes) or (bool(quotes) and answer == "\n".join(quotes))
    )
    return {"answer_present": isinstance(answer, str) and bool(answer),
            "reason": body.get("reason"), "citation_mapping_observed": valid,
            "citation_count": len(quotes)}


def measure(client: httpx.Client, port: int, questions: list[dict], deadline: float | None = None,
        sim_port: int | None = None, query: Callable | None = None) -> tuple[list, list]:
    retrieval = []
    controls = []
    send = query or (lambda path, question, top_k=5: request(client, port, path, question, top_k, deadline))
    for question in questions:
        if question["kind"] == "retrieval":
            observation = send("/search", question)
            returned = [item.get("doc_id") for item in results(observation)[:5]]
            retrieval.append({"id": question["id"], "status": observation["status"],
                              "hit": observation["status"] == 200 and bool(set(returned) & set(question["expected_doc_ids"])),
                              "returned_doc_ids": returned,
                              "snippets": [item.get("snippet", "") for item in results(observation)[:5]],
                              "useful_passage": question.get("useful_passage", ""),
                              "useful_passage_observed": any(question.get("useful_passage", "") in item["snippet"] for item in results(observation)[:5]) if question.get("useful_passage") else None,
                              "seconds": observation["seconds"]})
        elif question["kind"] == "access":
            forbidden = set(question["forbidden_doc_ids"])
            markers = question.get("context_markers", [])
            before_context = 0
            if sim_port is not None:
                client.post(f"http://127.0.0.1:{sim_port}/sim/watch", json={"markers": markers}).raise_for_status()
                before_context = model_stats(client, sim_port)["restricted_context_requests"]
            try:
                denied = [send(endpoint, question, 20) for endpoint in ("/search", "/answer")]
                context_count = model_stats(client, sim_port)["restricted_context_requests"] - before_context if sim_port is not None else 0
            finally:
                if sim_port is not None:
                    client.post(f"http://127.0.0.1:{sim_port}/sim/watch", json={"markers": []}).raise_for_status()
            exposed = set()
            for observation in denied:
                exposed.update(item.get("doc_id") for item in results(observation))
                citations = observation["body"].get("citations", [])
                if isinstance(citations, list):
                    exposed.update(item["doc_id"] for item in citations if isinstance(item, dict) and isinstance(item.get("doc_id"), str))
            positive = send("/search", {**question, "token": question["positive_token"]}, 20)
            positive_hit = positive["status"] == 200 and bool(forbidden & {item.get("doc_id") for item in results(positive)})
            public_positive = all(
                bool(set(question.get("public_doc_ids", [])) & {item.get("doc_id") for item in results(observation)})
                for observation in denied
            ) if question.get("public_doc_ids") else True
            leak = bool(forbidden & exposed)
            controls.append({"id": question["id"], "kind": "access", "leak_observed": leak,
                             "useful_passage": question.get("useful_passage", ""),
                             "restricted_context_requests": context_count,
                             "context_probe": "unique_fixture_phrases" if markers and sim_port is not None else "unobservable_identical_text_or_no_markers",
                             "positive_control": positive_hit,
                             "public_positive_control": public_positive,
                             "passed": all(item["status"] == 200 for item in denied) and positive_hit and public_positive and not leak and context_count == 0,
                             "note": "Human-confirm context matches. No match does not prove filtering; identical bodies require code/tests."})
        else:
            observation = send("/answer", question)
            answer = citation_observation(observation)
            success = answer["answer_present"] if question["kind"] == "answer" else (
                observation["body"].get("answer") is None and answer["reason"] == "no_evidence"
            )
            controls.append({"id": question["id"], "kind": question["kind"], **answer,
                             "useful_passage": question.get("useful_passage", ""),
                             "passed": observation["status"] == 200 and success and answer["citation_mapping_observed"]})
    return retrieval, controls


def validate_questions(questions: list[dict], trusted: Path) -> None:
    documents = {row["doc_id"]: row for row in (
        json.loads(line) for line in (trusted / "data/round1/manifest.jsonl").read_text().splitlines()
    )}
    tokens = json.loads((trusted / "identities.json").read_text())["tokens"]
    if len({item["id"] for item in questions}) != len(questions):
        raise HarnessError("duplicate_question_ids")
    for item in questions:
        if item["kind"] not in {"retrieval", "access", "answer", "abstention"}:
            raise HarnessError("invalid_question_kind")
        groups = tokens[item.get("positive_token", item["token"])]
        for doc_id in item["expected_doc_ids"]:
            allowed = documents[doc_id]["groups"]
            if not isinstance(allowed, list) or not ("public" in allowed or set(allowed) & set(groups)):
                raise HarnessError("invalid_positive_control")


def evaluate(project: Path, questions: list[dict], scenario: str = "clean", trusted: Path = TRUSTED,
             *, extra_questions: list[dict] | None = None,
             deadline: float | None = None, progress: Callable[[dict], None] | None = None) -> dict:
    started = time.monotonic()
    report = {"schema_version": "1.1", "scenario": scenario, "measurement": None,
              "controls": [], "operational": {}, "harness_error": None,
              "state": "running", "candidate_timeouts": []}
    processes: list[Process] = []
    application: list[Process] = []
    try:
        validate_questions(questions + (extra_questions or []), trusted)
        scratch = project / ".evaluation-tmp"
        scratch.mkdir(exist_ok=True)
        with ExitStack() as stack:
            temporary = stack.enter_context(tempfile.TemporaryDirectory(dir=scratch))
            stack.callback(lambda: [process.stop() for process in reversed(processes)])
            port, sim_port = free_port(), free_port()
            while port == sim_port:
                sim_port = free_port()
            env = environment(project, Path(temporary) / "store", port, sim_port, "test-embed-key")
            simulator = Process([sys.executable, "-I", "-S", "-c", "import sys; sys.path.insert(0, sys.argv[1]); from sim.__main__ import main; raise SystemExit(main())", str(trusted)], trusted, env)
            processes.append(simulator)
            if not wait_ready(simulator, f"http://127.0.0.1:{sim_port}/", remaining_seconds(deadline, 30)):
                raise HarnessError("simulator_startup")

            def ingest() -> int:
                limit = remaining_seconds(deadline, 180)
                with httpx.Client(trust_env=False, timeout=5) as observer:
                    start_index = len(model_stats(observer, sim_port)["request_log"])
                process = Process([sys.executable, "-c", "from harbourline_search.cli import ingest; raise SystemExit(ingest())", "round1"], project, env)
                processes.append(process)
                application.append(process)
                code = process.wait(limit)
                with httpx.Client(trust_env=False, timeout=5) as observer:
                    stats = model_stats(observer, sim_port)
                report["operational"]["simulator_stats"] = stats
                report["operational"].setdefault("ingest_attempts", []).append({"exit": code, "limit_seconds": round(limit, 3),
                    "transport": transport_observations(stats["request_log"][start_index:])})
                if code == 124:
                    report["candidate_timeouts"].append({"stage": "ingest", "limit_seconds": round(limit, 3),
                        "ceiling_seconds": 180, "cause": "review_budget" if limit < 180 else "candidate_limit"})
                    if limit < 180:
                        raise BudgetExpired()
                    raise CandidateTimeout()
                if progress:
                    progress(report)
                return code

            def serve() -> tuple[Process, bool]:
                limit = remaining_seconds(deadline, 30)
                process = Process([sys.executable, "-c", "from harbourline_search.cli import serve; raise SystemExit(serve())"], project, env)
                processes.append(process)
                application.append(process)
                ready = wait_ready(process, f"http://127.0.0.1:{port}/health", limit)
                if not ready and process.process.poll() is None:
                    report["candidate_timeouts"].append({"stage": "startup", "limit_seconds": round(limit, 3),
                        "ceiling_seconds": 30, "cause": "review_budget" if limit < 30 else "candidate_limit"})
                    if limit < 30:
                        raise BudgetExpired()
                    raise CandidateTimeout()
                return process, ready

            with httpx.Client(trust_env=False, timeout=10) as client:
                def query(path: str, question: dict, top_k: int = 5) -> dict:
                    start_index = len(model_stats(client, sim_port)["request_log"])
                    observation = request(client, port, path, question, top_k, deadline)
                    stats = model_stats(client, sim_port)
                    report["operational"]["simulator_stats"] = stats
                    report["operational"].setdefault("api_calls", []).append({
                        "endpoint": path, "status": observation["status"],
                        "transport": transport_observations(stats["request_log"][start_index:]),
                    })
                    return observation

                def flags(*names: str) -> None:
                    response = client.post(f"http://127.0.0.1:{sim_port}/sim/faults", json={"flags": list(names)})
                    if response.status_code != 200:
                        raise HarnessError("fault_control")

                initial_ingest = ingest()
                report["operational"]["ingest_exit"] = initial_ingest
                service, ready = serve()
                report["operational"]["startup_ready"] = ready
                if scenario == "clean":
                    before_auth = client.get(f"http://127.0.0.1:{sim_port}/sim/stats").json()["requests_total"]
                    unauthorized = [query(endpoint, {"token": "unknown-fixture-token", "query": "refund"}) for endpoint in ("/search", "/answer")]
                    after_auth = client.get(f"http://127.0.0.1:{sim_port}/sim/stats").json()["requests_total"]
                    report["operational"]["authentication"] = {
                        "statuses": [item["status"] for item in unauthorized],
                        "no_model_calls": before_auth == after_auth,
                        "passed": all(item["status"] == 401 for item in unauthorized) and before_auth == after_auth,
                    }
                    retrieval, controls = measure(client, port, questions, deadline, sim_port, query)
                    report["measurement"] = {"retrieval_questions": len(retrieval),
                                             "hits": sum(item["hit"] for item in retrieval),
                                             "hit_at_5": sum(item["hit"] for item in retrieval) / len(retrieval) if retrieval else None,
                                             "questions": retrieval}
                    report["controls"] = controls
                    if extra_questions:
                        extra_retrieval, extra_controls = measure(client, port, extra_questions, deadline, sim_port, query)
                        report["extra_questions"] = {
                            "measurement": {"retrieval_questions": len(extra_retrieval),
                                            "hits": sum(item["hit"] for item in extra_retrieval),
                                            "questions": extra_retrieval},
                            "controls": extra_controls,
                        }
                    if progress:
                        progress(report)
                    probes = [item for item in questions if item["kind"] == "retrieval"][:2]
                    before = [identities(query("/search", item, 20)) for item in probes]
                    service.stop()
                    service, restart_ready = serve()
                    after_restart = [identities(query("/search", item, 20)) for item in probes]
                    service.stop()
                    report["operational"]["persistence"] = {
                        "observed": bool(before) and all(before) and restart_ready and before == after_restart,
                    }
                    if progress:
                        progress(report)
                    repeat_exit = ingest()
                    service, repeat_ready = serve()
                    after_repeat = [identities(query("/search", item, 20)) for item in probes]
                    report["operational"]["persistence"] = {
                        "observed": bool(before) and all(before) and restart_ready and before == after_restart,
                        "repeat_ingest_exit": repeat_exit,
                        "repeat_consistent": repeat_ready and repeat_exit == 0 and before == after_repeat and all(before),
                        "no_duplicate_selected_chunks": all(len(items) == len(set(items)) for items in after_repeat),
                    }
                else:
                    control = next(item for item in questions if item["kind"] == "answer")
                    baseline = query("/answer", control)
                    report["operational"]["baseline_answer"] = citation_observation(baseline)
                    if scenario in {"faulted-ingest", "wrong-key"}:
                        before = identities(query("/search", control, 20))
                        service.stop()
                        if scenario == "faulted-ingest":
                            flags("rate_limit", "slow", "bad_response")
                        else:
                            env["EMBED_API_KEY"] = "wrong-fixture-key"
                        code = ingest()
                        flags()
                        if scenario == "wrong-key":
                            service, _ = serve()
                            wrong_queries = [query(endpoint, control) for endpoint in ("/search", "/answer")]
                            report["operational"]["wrong_key_query"] = [
                                {"status": item["status"], "passed": item["status"] == 503 and error_code(item) == "embedding_unavailable"}
                                for item in wrong_queries
                            ]
                            service.stop()
                        env["EMBED_API_KEY"] = "test-embed-key"
                        service, ready = serve()
                        after = identities(query("/search", control, 20))
                        report["operational"].update({"fault_ingest_exit": code,
                            "previous_complete_results_preserved": ready and bool(before) and before == after,
                            "expected_exit_observed": code in ({0, 1} if scenario == "faulted-ingest" else {2})})
                    else:
                        flags({"query-outage": "outage", "llm-outage": "llm_outage", "llm-bad-output": "llm_bad_output"}[scenario])
                        observations = []
                        for _ in range(4 if scenario == "llm-bad-output" else 1):
                            observation = query("/answer", control)
                            body = observation["body"]
                            expected_reason = "model_unavailable" if scenario == "llm-outage" else "invalid_model_output"
                            passed = (observation["status"] == 503 and error_code(observation) == "embedding_unavailable") if scenario == "query-outage" else (
                                observation["status"] == 200 and body.get("answer") is None and body.get("citations") == [] and body.get("reason") == expected_reason and body.get("degraded") is True and bool(results(observation))
                            )
                            observations.append({"status": observation["status"], "reason": body.get("reason"), "passed": passed, "seconds": observation["seconds"]})
                        report["controls"] = observations
                        if scenario == "query-outage":
                            search_outage = query("/search", control)
                            report["controls"].append({"endpoint": "/search", "status": search_outage["status"],
                                "passed": search_outage["status"] == 503 and error_code(search_outage) == "embedding_unavailable"})
                guest = json.loads((trusted / "identities.json").read_text())["tokens"]
                token = next(iter(guest))
                query("/answer", {"token": token, "query": "QUERY_CANARY_7f3 DOC_CANARY_7f3 imported note"})
                report["operational"]["simulator_stats"] = client.get(f"http://127.0.0.1:{sim_port}/sim/stats").json()
                service.stop()
                report["state"] = "complete"
    except CandidateTimeout:
        report["state"] = "candidate_timeout"
    except BudgetExpired:
        report["state"] = "budget_exhausted"
    except (HarnessError, OSError, KeyError, ValueError, httpx.HTTPError) as error:
        report["harness_error"] = str(error) if isinstance(error, HarnessError) else "harness_input_or_execution_failure"
        report["state"] = "harness_error"
    finally:
        for process in reversed(processes):
            process.stop()
        combined = b"\n".join(bytes(process.output) for process in application)
        canaries = ("QUERY_CANARY_7f3", "TOKEN_CANARY_7f3", "DOC_CANARY_7f3", "MODEL_OUTPUT_CANARY")
        report["logs"] = {"canary_matches": [name for name in canaries if name.encode() in combined],
                          "truncated": any(process.overflow for process in application),
                          "bytes_captured": len(combined), "human_confirmation_required": True}
        report["seconds"] = round(time.monotonic() - started, 3)
        stats = report["operational"].get("simulator_stats", {})
        report["transport"] = transport_observations(stats.get("request_log", []))
        report["transport"]["scope"] = "Whole scenario; repeated inputs can be separate calls. Use operational stage slices for retry conclusions."
        report["restricted_context_requests"] = stats.get("restricted_context_requests", 0)
        report["summary"] = scenario_summary(report)
        if progress:
            progress(report)
    return {"summary": report.pop("summary"), **report}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--questions", type=Path, default=TRUSTED / "questions.json")
    parser.add_argument("--output", type=Path, default=Path("submission/evaluation.json"))
    parser.add_argument("--scenario", choices=SCENARIOS, default="clean")
    args = parser.parse_args()
    try:
        questions = json.loads(args.questions.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("Evaluator input unavailable; no measurement.", file=sys.stderr)
        return 2
    report = evaluate(args.project.resolve(), questions, args.scenario)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    if report["harness_error"]:
        print("Evaluator harness failed; no measurement.")
        return 2
    measurement = report["measurement"]
    if measurement is not None:
        print(f"hit@5: {measurement['hits']}/{measurement['retrieval_questions']}; controls: {sum(item['passed'] for item in report['controls'])}/{len(report['controls'])}")
    else:
        print(f"Scenario complete: {args.scenario}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
