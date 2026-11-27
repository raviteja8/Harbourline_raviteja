import http.client
import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from .server import SimulatorServer


@dataclass(frozen=True)
class SimulatorFixture:
    server: SimulatorServer
    key: str = "test-embed-key"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def control(self, path: str, body: dict | None = None) -> dict:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("GET" if body is None else "POST", path,
                               body=None if body is None else json.dumps(body),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            data = json.loads(response.read())
            if response.status != 200:
                raise RuntimeError("Simulator control failed.")
            return data
        finally:
            connection.close()

    def flags(self, *names: str) -> dict:
        return self.control("/sim/faults", {"flags": list(names)})

    def reset(self) -> dict:
        return self.control("/sim/reset", {})

    def stats(self) -> dict:
        return self.control("/sim/stats")


@contextmanager
def running_simulator(sleeper: Callable[[float], None] = time.sleep) -> Iterator[SimulatorFixture]:
    server = SimulatorServer(0, sleeper=sleeper)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield SimulatorFixture(server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("Simulator did not stop.")
