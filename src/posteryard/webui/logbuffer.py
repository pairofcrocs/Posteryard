"""Keeps the latest log records in memory for the web UI's activity page."""

import itertools
import logging
import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any

from posteryard import logfmt

SIZE = 2000
LEVELS = {logging.DEBUG: "debug", logging.INFO: "info", logging.WARNING: "warning", logging.ERROR: "error"}
ORDER = {"debug": 0, "info": 1, "warning": 2, "error": 3}


class LogBuffer(logging.Handler):
    def __init__(self, size: int = SIZE) -> None:
        super().__init__(logging.DEBUG)
        self._records: deque[dict[str, Any]] = deque(maxlen=size)
        self._seq = itertools.count(1)
        self._guard = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            fields = {key: _text(raw) for key, raw in logfmt.fields(record).items()}
            if record.exc_info and record.exc_info[1] is not None:
                fields["error"] = f"{type(record.exc_info[1]).__name__}: {record.exc_info[1]}"
            entry: dict[str, Any] = {
                "seq": 0,
                "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
                "level": LEVELS.get(record.levelno, "error" if record.levelno > logging.ERROR else "debug"),
                "logger": record.name,
                "msg": record.getMessage(),
                "fields": fields,
            }
            with self._guard:
                entry["seq"] = next(self._seq)
                self._records.append(entry)
        except Exception:  # Logging must never raise.
            self.handleError(record)

    def since(self, after: int = 0, level: str = "debug", limit: int = 500) -> list[dict[str, Any]]:
        floor = ORDER.get(level, 0)
        with self._guard:
            found = [r for r in self._records if r["seq"] > after and ORDER[r["level"]] >= floor]
        return found[-limit:]

    def last(self) -> int:
        with self._guard:
            return self._records[-1]["seq"] if self._records else 0


def _text(raw: object) -> str:
    """Plain text for the page: no logfmt quoting, which only matters on a log line."""
    if raw is None:
        return ""
    return ("true" if raw else "false") if isinstance(raw, bool) else str(raw)


_buffer: LogBuffer | None = None


def install() -> LogBuffer:
    """Attach one buffer to the root logger. It sees what the root's level lets through."""
    global _buffer  # noqa: PLW0603
    if _buffer is None:
        _buffer = LogBuffer()
    # logfmt.setup() replaces the root's handlers, so attach again whenever it is gone.
    root = logging.getLogger()
    if _buffer not in root.handlers:
        root.addHandler(_buffer)
    return _buffer
