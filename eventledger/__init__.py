"""eventledger — offline crawler run-event ledger.

A standalone, standard-library-only package that reconstructs what each
crawler request actually went through from batches of JSONL run events
(queued / started / response / retry_scheduled / failed / cancelled).

It deliberately has no dependency on, and never reads, the encrypted
archive store (``archivestore``) or any WARC body: input is local UTF-8
JSONL only and the durable ledger is a single SQLite file.

Public API:
    Ledger              open/create a SQLite ledger and import/query/export
    ImportResult        outcome of one batch import
    RequestTimeline     reconstructed per-request attempt timeline
    RunSummary          one run's counters, success rate and durations
    EventLedgerError    base error
    ValidationError     a rejected batch (nothing is written)
    EventConflictError  same event_id with different content
    parse_event_time    parse a timezone-required ISO-8601 instant
"""

from __future__ import annotations

from .errors import (
    EventConflictError,
    EventLedgerError,
    LedgerNotFoundError,
    ValidationError,
)
from .ledger import ImportResult, Ledger, RequestTimeline, RunSummary
from .validate import parse_event_time

__all__ = [
    "Ledger",
    "ImportResult",
    "RequestTimeline",
    "RunSummary",
    "EventLedgerError",
    "ValidationError",
    "EventConflictError",
    "LedgerNotFoundError",
    "parse_event_time",
]

__version__ = "1.0.0"
