"""Structured logging: each record carries an event name plus JSON-encoded fields."""

import json
import logging
from typing import Mapping

_FIELDS_ATTR = "uare_fields"


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{record.levelname.lower()} {record.name} {record.getMessage()}"
        fields: Mapping[str, object] | None = getattr(record, _FIELDS_ATTR, None)
        if not fields:
            return base
        return f"{base} {json.dumps(fields, ensure_ascii=False, default=str, sort_keys=True)}"


def configure(level: int) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter())
    root = logging.getLogger("uare")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False


def event(logger: logging.Logger, level: int, name: str, **fields: object) -> None:
    logger.log(level, name, extra={_FIELDS_ATTR: fields})
