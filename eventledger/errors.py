"""Exception hierarchy for eventledger.

Every rejected import raises (or, on the CLI, reports) a diagnostic that
identifies run_id, request_id, the 1-based JSONL line number and the
reason, so an operator can fix the input and re-run the batch safely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


class EventLedgerError(Exception):
    """Base class for all eventledger errors."""


class ValidationError(EventLedgerError):
    """A batch failed structural or state validation.

    The whole batch is rejected; no row from it remains in the ledger.
    ``problems`` holds every detected problem (the first one is also used
    as the exception message).  Each problem carries run_id, request_id,
    the 1-based source line number (``line`` is None for cross-line
    problems that are not tied to one physical line) and the reason.
    """

    def __init__(self, problems: Optional[list["EventProblem"]] = None,
                 message: Optional[str] = None):
        self.problems: list[EventProblem] = list(problems or [])
        if message is None:
            message = str(self.problems[0]) if self.problems else "validation failed"
        super().__init__(message)


class EventConflictError(ValidationError):
    """An event_id already exists with different content.

    Replaying the *same* event_id with identical content is a harmless
    no-op; different content for a known id rejects the whole batch.
    """


class LedgerNotFoundError(EventLedgerError):
    """A requested run or request is not present in the ledger."""


@dataclass(frozen=True)
class EventProblem:
    """One validation diagnostic."""

    reason: str
    line: Optional[int] = None
    run_id: Optional[str] = None
    request_id: Optional[str] = None
    event_id: Optional[str] = None

    def __str__(self) -> str:
        loc = []
        if self.run_id is not None:
            loc.append(f"run_id={self.run_id!r}")
        if self.request_id is not None:
            loc.append(f"request_id={self.request_id!r}")
        if self.event_id is not None:
            loc.append(f"event_id={self.event_id!r}")
        if self.line is not None:
            loc.append(f"line={self.line}")
        head = " ".join(loc)
        return f"{head}: {self.reason}" if head else self.reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "line": self.line,
            "run_id": self.run_id,
            "request_id": self.request_id,
            "event_id": self.event_id,
        }
