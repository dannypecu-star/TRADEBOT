"""Structured logging setup.

Automated trading is only as trustworthy as its audit trail. When something goes wrong at
3am you need to reconstruct exactly what the bot saw and did. This module gives every
component:

  * a **JSON log line** per event (easy to ship to Loki/CloudWatch/ELK and query), and
  * simultaneous human-readable console output during development.

Every trade decision, fill, and error should go through a logger obtained here so the
whole system logs in one consistent, machine-parseable format.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Render a log record as a single JSON object (one line = one event)."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Attach any structured extras passed via logger.info(msg, extra={"extra": {...}}).
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(
    level: str = "INFO",
    log_file: str | None = None,
    json_console: bool = False,
) -> None:
    """Configure the root logger once, for the whole process.

    * ``log_file`` -- if set, JSON lines are appended here (the durable audit trail).
    * ``json_console`` -- emit JSON to stdout too (use in containers where a log shipper
      scrapes stdout); otherwise the console stays human-readable.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Clear existing handlers so repeated calls (e.g. in tests) don't duplicate output.
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler(sys.stdout)
    if json_console:
        console.setFormatter(JsonFormatter())
    else:
        console.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
        )
    root.addHandler(console)

    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
