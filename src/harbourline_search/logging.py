import json
import logging
import sys
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for name in ("doc_id", "count", "duration_ms", "reason"):
            value = getattr(record, name, None)
            if isinstance(value, (str, int, float, bool)):
                fields[name] = value
        return json.dumps(fields, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
        logger.disabled = name == "uvicorn.access"
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
