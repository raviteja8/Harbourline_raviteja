import argparse
import sys

import uvicorn

from .app import create_app
from .logging import configure_logging
from .settings import SettingsError, load_settings


def serve() -> int:
    try:
        settings = load_settings()
    except SettingsError as error:
        print(str(error), file=sys.stderr)
        return 2
    configure_logging()
    uvicorn.run(
        create_app(), host="127.0.0.1", port=settings.port, access_log=False, log_config=None
    )
    return 0


def ingest() -> int:
    parser = argparse.ArgumentParser(prog="ingest")
    parser.add_argument("round", choices=("round1",), help="The supplied snapshot: round1.")
    parser.parse_args()
    try:
        load_settings()
    except SettingsError as error:
        print(str(error), file=sys.stderr)
        return 2
    print("not implemented")
    return 3


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m harbourline_search.cli")
    parser.add_argument("command", choices=("serve", "ingest"))
    arguments, remaining = parser.parse_known_args()
    if arguments.command == "serve":
        if remaining:
            parser.error("serve does not take positional arguments")
        return serve()
    sys.argv = ["ingest", *remaining]
    return ingest()


if __name__ == "__main__":
    raise SystemExit(main())
