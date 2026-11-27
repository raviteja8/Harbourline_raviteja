import json
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .protocol import FAULT_DESCRIPTIONS
from .vectors import request_key


@dataclass
class RequestRecord:
    arrival_time: float
    key: str | None = None
    status: int | None = None
    tracked: bool = True
    route: str = "/v1/embeddings"
    response_time: float | None = None
    retry_after: int | None = None
    context_matches: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    fault: str | None = None
    bad_variant: int | None = None


class State:
    def __init__(self, flags: set[str] | None = None, clock: Callable[[], float] = time.monotonic):
        self.lock = threading.Lock()
        self.clock = clock
        self.started = clock()
        self.flags = set(flags or ())
        self.in_flight = 0
        self.request_log: list[RequestRecord] = []
        self.watch_markers: dict[str, str] = {}
        self._clear()

    def _clear(self) -> None:
        self.requests_total = 0
        self.by_status: dict[str, int] = {}
        self.inputs_total = 0
        self.max_batch_size = 0
        self.max_in_flight = self.in_flight
        self.auth_failures = 0
        self.restricted_context_requests = 0
        self.restricted_context_matches: dict[str, int] = {}
        self.faults_injected = dict.fromkeys(FAULT_DESCRIPTIONS, 0)
        self._restart_numbering()

    def _restart_numbering(self) -> None:
        self.keys: dict[str, int] = {}
        self.bad_response_count = 0
        self.llm_bad_output_count = 0

    def begin(self, route: str = "/v1/embeddings") -> RequestRecord:
        with self.lock:
            record = RequestRecord(float(self.clock() - self.started), route=route)
            self.request_log.append(record)
            self.requests_total += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            return record

    def finish(self) -> None:
        with self.lock:
            self.in_flight -= 1

    def count_inputs(self, record: RequestRecord, inputs: list[str]) -> None:
        with self.lock:
            if record.tracked:
                self.inputs_total += len(inputs)
                self.max_batch_size = max(self.max_batch_size, len(inputs))

    def status(self, record: RequestRecord, status: int, retry_after: int | None = None) -> None:
        with self.lock:
            record.response_time = float(self.clock() - self.started)
            record.retry_after = retry_after
            previous = record.status
            if previous == status:
                return
            record.status = status
            if record.tracked:
                if previous is not None:
                    old_name = str(previous)
                    self.by_status[old_name] -= 1
                    if self.by_status[old_name] == 0:
                        del self.by_status[old_name]
                    if previous == 401:
                        self.auth_failures -= 1
                name = str(status)
                self.by_status[name] = self.by_status.get(name, 0) + 1
                if status == 401:
                    self.auth_failures += 1

    def watch(self, markers: dict[str, str] | None = None) -> dict:
        with self.lock:
            if markers is not None:
                self.watch_markers = dict(markers)
            return {"marker_ids": sorted(self.watch_markers)}

    def observe_chat(self, record: RequestRecord, messages: list[dict]) -> None:
        supplied = json.loads(messages[1]["content"])
        content = "\n".join([messages[0]["content"], supplied["question"],
                              *(source["text"] for source in supplied["sources"])])
        with self.lock:
            record.key = request_key([message["content"] for message in messages])
            matches = tuple(name for name, marker in self.watch_markers.items() if marker in content)
            record.context_matches = matches
            if matches and record.tracked:
                self.restricted_context_requests += 1
                for name in matches:
                    self.restricted_context_matches[name] = self.restricted_context_matches.get(name, 0) + 1

    def decide(self, record: RequestRecord, inputs: list[str]) -> Decision:
        key = request_key(inputs)
        with self.lock:
            record.key = key
            fault = None
            variant = None
            if "outage" in self.flags:
                fault = "outage"
            elif key not in self.keys:
                number = len(self.keys) + 1
                self.keys[key] = number
                candidate = ("bad_response", "rate_limit", "slow")[number % 3]
                if candidate in self.flags:
                    fault = candidate
                    if fault == "bad_response":
                        variant = self.bad_response_count % 4
                        self.bad_response_count += 1
            if fault is not None and record.tracked:
                self.faults_injected[fault] += 1
            return Decision(fault, variant)

    def decide_chat(self, record: RequestRecord) -> Decision:
        with self.lock:
            fault = None
            variant = None
            if "llm_outage" in self.flags:
                fault = "llm_outage"
            elif "llm_bad_output" in self.flags:
                fault = "llm_bad_output"
                variant = self.llm_bad_output_count % 4
                self.llm_bad_output_count += 1
            if fault is not None and record.tracked:
                self.faults_injected[fault] += 1
            return Decision(fault, variant)

    def get_flags(self) -> list[str]:
        with self.lock:
            return sorted(self.flags)

    def set_flags(self, flags: set[str]) -> None:
        with self.lock:
            if flags != self.flags:
                self.flags = set(flags)
                self._restart_numbering()

    def reset(self) -> None:
        with self.lock:
            for record in self.request_log:
                record.tracked = False
            self.request_log.clear()
            self._clear()

    def stats(self) -> dict:
        with self.lock:
            return {
                "requests_total": self.requests_total,
                "by_status": dict(self.by_status),
                "inputs_total": self.inputs_total,
                "max_batch_size": self.max_batch_size,
                "max_in_flight": self.max_in_flight,
                "auth_failures": self.auth_failures,
                "faults_injected": dict(self.faults_injected),
                "restricted_context_requests": self.restricted_context_requests,
                "restricted_context_matches": dict(self.restricted_context_matches),
                "request_log": [
                    {
                        "arrival_time": record.arrival_time,
                        "key": record.key,
                        "status": record.status,
                        "route": record.route,
                        "response_time": record.response_time,
                        "retry_after": record.retry_after,
                        "context_matches": list(record.context_matches),
                    }
                    for record in self.request_log
                ],
            }
