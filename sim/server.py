import hmac
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .protocol import (
    FAULT_DESCRIPTIONS,
    METHODS,
    InvalidRequest,
    completion,
    decode,
    embeddings,
    error,
    limit_error,
    validate_flags,
    validate_inputs,
    validate_chat,
    validate_watch,
)
from .state import State


MAX_BODY_SIZE = 4 * 1024 * 1024
MAX_DISCARD_SIZE = 64 * 1024 * 1024
DISCARD_TIMEOUT = 10


class BodyTooLarge(InvalidRequest):
    def __init__(self, discard_length: int):
        self.discard_length = discard_length
        super().__init__()


class SimulatorServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(
        self,
        port: int = 8090,
        api_key: str = "test-embed-key",
        flags: set[str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.api_key = api_key
        self.state = State(flags, clock)
        self.sleeper = sleeper
        super().__init__(("127.0.0.1", port), Handler)

    def handle_error(self, request, client_address) -> None:
        pass


class Handler(BaseHTTPRequestHandler):
    server: SimulatorServer
    protocol_version = "HTTP/1.1"
    timeout = 10

    def log_message(self, format: str, *args) -> None:
        pass

    def handle(self) -> None:
        try:
            super().handle()
        except OSError:
            pass

    def parse_request(self) -> bool:
        parsed = super().parse_request()
        if parsed:
            self.raw_target = self.requestline.split()[1]
        return parsed

    def send_error(self, code: int, message=None, explain=None) -> None:
        self.request_version = "HTTP/1.1"
        self._write(400, error("invalid_request"), {})

    def _content_length(self) -> int | None:
        headers = getattr(self, "headers", None)
        lengths = headers.get_all("Content-Length", []) if headers is not None else []
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            return None
        digits = lengths[0].lstrip("0") or "0"
        if len(digits) > 8:
            raise BodyTooLarge(MAX_DISCARD_SIZE)
        length = int(digits)
        if length > MAX_BODY_SIZE:
            raise BodyTooLarge(min(length, MAX_DISCARD_SIZE))
        return length

    def _discard_oversized_body(self, length: int) -> None:
        remaining = min(length, MAX_DISCARD_SIZE)
        deadline = time.monotonic() + DISCARD_TIMEOUT
        while remaining:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                break
            try:
                self.connection.settimeout(timeout)
                chunk = self.rfile.read1(min(65536, remaining))
            except OSError:
                break
            if not chunk:
                break
            remaining -= len(chunk)

    def _read_body_bytes(self, length: int) -> bytes:
        if not getattr(self, "_body_read", False):
            self._body_read = True
            try:
                self._body_bytes = self.rfile.read(length)
            except OSError as exc:
                self._body_bytes = b""
                raise InvalidRequest() from exc
            if len(self._body_bytes) != length:
                raise InvalidRequest()
        return self._body_bytes

    def _body(self, allow_empty: bool = False):
        if self.headers.get_all("Transfer-Encoding"):
            raise InvalidRequest()
        length = self._content_length()
        if length is None and allow_empty and not self.headers.get_all("Content-Length"):
            return {}
        if length is None:
            raise InvalidRequest()
        body = self._read_body_bytes(length)
        if not body and allow_empty:
            return {}
        return decode(body)

    def _authenticated(self) -> bool:
        values = self.headers.get_all("Authorization", [])
        if len(values) != 1:
            return False
        parts = values[0].split()
        return (
            len(parts) == 2
            and parts[0].lower() == "bearer"
            and hmac.compare_digest(parts[1].encode("utf-8"), self.server.api_key.encode("utf-8"))
        )

    def _embedding(self, record):
        if not self._authenticated():
            return 401, error("unauthorized"), {"WWW-Authenticate": "Bearer"}
        inputs = validate_inputs(self._body())
        self.server.state.count_inputs(record, inputs)
        code = limit_error(inputs)
        if code is not None:
            return 400, error(code), {}
        decision = self.server.state.decide(record, inputs)
        if decision.fault == "outage":
            return 503, error("unavailable"), {"Retry-After": "2"}
        if decision.fault == "rate_limit":
            return 429, error("rate_limited"), {"Retry-After": "1"}
        self.server.state.status(record, 200)
        if decision.fault == "slow":
            self.server.sleeper(1)
        return 200, embeddings(inputs, decision.bad_variant), {}

    def _chat(self, record):
        if not self._authenticated():
            return 401, error("unauthorized"), {"WWW-Authenticate": "Bearer"}
        body = self._body()
        question, sources = validate_chat(body)
        self.server.state.observe_chat(record, body["messages"])
        decision = self.server.state.decide_chat(record)
        if decision.fault == "llm_outage":
            return 503, error("unavailable"), {"Retry-After": "2"}
        return 200, completion(question, sources, decision.bad_variant), {}

    def _control(self, path: str):
        if path == "/":
            return {
                "endpoints": {name: list(methods) for name, methods in METHODS.items()},
                "fault_flags": FAULT_DESCRIPTIONS,
            }
        if path == "/sim/faults":
            if self.command == "POST":
                self.server.state.set_flags(validate_flags(self._body()))
            return {"flags": self.server.state.get_flags()}
        if path == "/sim/reset":
            body = self._body(allow_empty=True)
            if type(body) is not dict or body != {}:
                raise InvalidRequest()
            self.server.state.reset()
            return {"reset": True}
        if path == "/sim/watch":
            return self.server.state.watch(validate_watch(self._body()) if self.command == "POST" else None)
        return self.server.state.stats()

    def _write(self, status: int, body, headers: dict[str, str]) -> None:
        discard_length = 0
        try:
            length = self._content_length()
            if length is not None:
                self._read_body_bytes(length)
        except BodyTooLarge as exc:
            discard_length = exc.discard_length
            status = 400
            body = error("invalid_request", "Request body too large")
            headers = {}
        except InvalidRequest:
            pass
        record = getattr(self, "_record", None)
        if record is not None:
            self.server.state.status(record, status, int(headers["Retry-After"]) if "Retry-After" in headers else None)
        encoded = json.dumps(
            body, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)
        self.wfile.flush()
        if discard_length:
            self._discard_oversized_body(discard_length)

    def _dispatch(self) -> None:
        target = self.raw_target
        path = target.split("?", 1)[0]
        if "#" in target or not target.startswith("/") or target.startswith("//"):
            path = ""
        methods = METHODS.get(path)
        if methods is None:
            self._write(404, error("not_found"), {})
            return
        if self.command not in methods:
            self._write(405, error("method_not_allowed"), {"Allow": ", ".join(methods)})
            return
        record = self.server.state.begin(path) if path in {"/v1/embeddings", "/v1/chat/completions"} else None
        self._record = record
        try:
            try:
                self._content_length()
                if path == "/v1/chat/completions":
                    status, body, headers = self._chat(record)
                elif record is not None:
                    status, body, headers = self._embedding(record)
                else:
                    status, body, headers = 200, self._control(path), {}
            except BodyTooLarge:
                status, body, headers = 400, error("invalid_request", "Request body too large"), {}
            except InvalidRequest:
                status, body, headers = 400, error("invalid_request"), {}
            except Exception:
                status, body, headers = 500, error("internal"), {}
            self._write(status, body, headers)
        except OSError:
            pass
        finally:
            if record is not None:
                self.server.state.finish()

    def __getattr__(self, name: str):
        if name.startswith("do_"):
            return self._dispatch
        raise AttributeError(name)
