"""Structured, credential-redacted logs with operation correlation IDs."""
import json
import logging
import re
import sys
from datetime import datetime, timezone

from app.core.branding import public_metadata, public_text

_SECRET = re.compile(r"sk-[A-Za-z0-9_-]{12,}|Bearer\s+[^\s\"']+|((?:api[_-]?key|password|secret|token)[\"']?\s*[:=]\s*[\"']?)[^\s,;\"']+", re.I)
_AUTH_URL = re.compile(r"(https?://)[^/@\s:]+:[^/@\s]+@", re.I)


def redact(value):
    text = str(value)
    return public_text(_AUTH_URL.sub(r"\1[REDACTED]@", _SECRET.sub("[REDACTED]", text)))


class StructuredFormatter(logging.Formatter):
    def format(self, record):
        from app.services.observability import context_values
        output = {"time": datetime.now(timezone.utc).isoformat(), "level": record.levelname,
                  "logger": public_text(record.name), "message": redact(record.getMessage()),
                  **public_metadata(context_values())}
        if record.exc_info:
            output["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(output, ensure_ascii=False)


class PublicFormatter(logging.Formatter):
    """Keep a host server's log format while branding its complete output."""

    def __init__(self, delegate=None):
        super().__init__()
        self.delegate = delegate or logging.Formatter()

    def format(self, record):
        return public_text(self.delegate.format(record))


def setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredFormatter())
        root.addHandler(handler)
    for logger in (root, logging.getLogger("uvicorn"), logging.getLogger("uvicorn.error"),
                   logging.getLogger("uvicorn.access")):
        for handler in logger.handlers:
            if not isinstance(handler.formatter, (StructuredFormatter, PublicFormatter)):
                handler.setFormatter(PublicFormatter(handler.formatter))
    logging.getLogger("alembic").setLevel(logging.WARNING)


setup_logging()
