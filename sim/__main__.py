import os
import signal
import sys

from .protocol import FAULT_DESCRIPTIONS
from .server import SimulatorServer


def settings(environ) -> tuple[int, str, set[str]]:
    value = environ.get("SIM_PORT", "8090")
    digits = value.lstrip("0") or "0"
    if not value.isascii() or not value.isdigit() or len(digits) > 5:
        raise ValueError("Invalid SIM_PORT; expected ASCII digits from 1 to 65535.")
    port = int(digits)
    if not 1 <= port <= 65535:
        raise ValueError("Invalid SIM_PORT; expected ASCII digits from 1 to 65535.")
    key = environ.get("SIM_API_KEY", "test-embed-key")
    if not key or any(character.isspace() for character in key) or not key.isascii():
        raise ValueError("Invalid SIM_API_KEY; expected a non-empty ASCII key without whitespace.")
    flags = {flag.strip() for flag in environ.get("SIM_FAULTS", "").split(",") if flag.strip()}
    if not flags <= FAULT_DESCRIPTIONS.keys():
        raise ValueError("Invalid SIM_FAULTS; unknown fault flag.")
    return port, key, flags


def _stop(signum, frame) -> None:
    raise KeyboardInterrupt()


def main() -> int:
    try:
        port, key, flags = settings(os.environ)
        server = SimulatorServer(port, key, flags)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError:
        print("Simulator could not bind to 127.0.0.1:SIM_PORT.", file=sys.stderr)
        return 2
    previous_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        with server:
            for signum in previous_handlers:
                signal.signal(signum, _stop)
            print(f"Simulator listening on 127.0.0.1:{port}", file=sys.stderr)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
