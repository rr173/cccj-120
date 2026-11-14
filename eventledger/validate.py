"""Structural validation and time handling for run events.

A single JSONL line is one event object.  The seven common fields are
required on every event; the six event types each carry their own
payload fields.  ``response`` keeps only the HTTP status code and the
response byte count -- response bodies are never part of the ledger.

Parsing rules enforced here (per physical line):
  * the line must be a UTF-8 JSON object (duplicate JSON keys rejected,
    non-finite numbers rejected);
  * run_id / request_id / event_id / event_type / event_time are strings;
  * attempt and seq are positive integers (JSON booleans are not ints);
  * event_time must be ISO-8601 with an explicit timezone/offset;
  * type-specific fields and ranges are checked;
  * any unexpected/unknown field is still *kept* (the ledger preserves
    original event fields verbatim) but must have a JSON-compatible type.

Cross-line rules (seq ordering, attempt progression, state machine,
event_id identity) live in :mod:`eventledger.ledger`.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .errors import EventProblem

EVENT_TYPES = frozenset(
    ("queued", "started", "response", "retry_scheduled", "failed", "cancelled")
)
COMMON_FIELDS = ("run_id", "request_id", "attempt", "seq",
                 "event_id", "event_type", "event_time")

# event_type -> required extra field -> accepted Python type(s)
TYPE_FIELDS: dict[str, dict[str, tuple[type, ...]]] = {
    "queued": {},
    "started": {},
    "response": {
        "status_code": (int,),
        "response_bytes": (int,),
    },
    "retry_scheduled": {
        "next_attempt": (int,),
    },
    "failed": {
        "error": (str,),
    },
    "cancelled": {
        "reason": (str,),
    },
}


class _RejectLine(Exception):
    """Internal: this physical line is invalid; carries the reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _UniqueKeyDecoder:
    """json.loads object_pairs_hook that rejects duplicate object keys."""

    @staticmethod
    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        for key, value in pairs:
            if key in obj:
                raise _RejectLine(f"duplicate JSON object key {key!r}")
            obj[key] = value
        return obj


def parse_event_time(value: str) -> datetime:
    """Parse an ISO-8601 instant that MUST carry a timezone.

    The original string is preserved on the event for faithful export;
    this parsed instant is what duration math uses, so events arriving
    with different offsets compare by their real point on the timeline.

    Naive datetimes (no offset) are rejected.  A trailing ``Z`` is
    accepted on Python versions whose fromisoformat does not handle it.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("event_time must be a non-empty ISO-8601 string")
    text = value
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"event_time {value!r} is not ISO-8601: {exc}") from None
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"event_time {value!r} lacks a timezone/offset")
    return dt


def _is_plain_int(value: Any) -> bool:
    # bool is a subclass of int; attempt/seq must be real JSON integers.
    return isinstance(value, int) and not isinstance(value, bool)


def _require_str(obj: dict[str, Any], field: str) -> str:
    value = obj.get(field)
    if not isinstance(value, str) or not value:
        raise _RejectLine(f"field {field!r} must be a non-empty string")
    return value


def parse_line(raw: str, line_no: int) -> tuple[dict[str, Any], EventProblem | None]:
    """Parse and structurally validate one JSONL line.

    Returns ``(event, None)`` on success or ``({}, problem)`` on failure.
    Never raises for content problems; only programming errors would.
    """
    try:
        if not raw.strip():
            raise _RejectLine("empty line is not a valid event")
        try:
            obj = json.loads(raw, object_pairs_hook=_UniqueKeyDecoder.hook,
                             parse_constant=_reject_constant)
        except _RejectLine as exc:
            raise
        except json.JSONDecodeError as exc:
            raise _RejectLine(f"invalid JSON: {exc.msg} at column {exc.colno}")
        if not isinstance(obj, dict):
            raise _RejectLine("event must be a JSON object")

        for field in ("run_id", "request_id", "event_id", "event_type",
                      "event_time"):
            _require_str(obj, field)
        for field in ("attempt", "seq"):
            if not _is_plain_int(obj.get(field)):
                raise _RejectLine(f"field {field!r} must be an integer")
            if obj[field] < 1:
                raise _RejectLine(f"field {field!r} must be >= 1")

        event_type = obj["event_type"]
        if event_type not in EVENT_TYPES:
            raise _RejectLine(
                f"event_type {event_type!r} is not one of "
                f"{sorted(EVENT_TYPES)}")

        # Validate (but do not reject on presence of) type-specific fields.
        required = TYPE_FIELDS[event_type]
        for field, types in required.items():
            if field not in obj:
                raise _RejectLine(
                    f"{event_type} event requires field {field!r}")
            value = obj[field]
            if not (isinstance(value, types) and not
                    (types == (int,) and isinstance(value, bool))):
                raise _RejectLine(
                    f"field {field!r} must be of type "
                    f"{types[0].__name__}")

        if event_type == "response":
            if not 100 <= obj["status_code"] <= 599:
                raise _RejectLine(
                    f"status_code {obj['status_code']} outside 100..599")
            if obj["response_bytes"] < 0:
                raise _RejectLine("response_bytes must be >= 0")
        if event_type == "retry_scheduled":
            if obj["next_attempt"] < 1:
                raise _RejectLine("next_attempt must be >= 1")

        # Timezone check up front so a bad instant cannot reach the DB.
        try:
            parse_event_time(obj["event_time"])
        except ValueError as exc:
            raise _RejectLine(str(exc)) from None
        return obj, None
    except _RejectLine as exc:
        return {}, EventProblem(
            reason=exc.reason, line=line_no,
            run_id=raw_lookup(raw, "run_id"),
            request_id=raw_lookup(raw, "request_id"),
            event_id=raw_lookup(raw, "event_id"))


def raw_lookup(raw_json: str, field: str) -> str | None:
    """Best-effort extraction of an id for diagnostics on broken lines."""
    try:
        obj = json.loads(raw_json)
        value = obj.get(field)
        return value if isinstance(value, str) else None
    except (json.JSONDecodeError, AttributeError):
        return None


def _reject_constant(value: str) -> Any:
    # NaN / Infinity / -Infinity are not valid JSON for our events.
    raise _RejectLine(f"non-finite JSON constant {value} is not allowed")
