"""SQLite-backed run-event ledger and the import/query/export API.

Import protocol (one JSONL file == one batch == one transaction):

  1. The file is read line by line and never loaded whole: each line is
     structurally validated and inserted into a TEMP staging table.
  2. After the whole file is consumed, set-based checks verify event_id
     identity against committed rows and within the batch, duplicate seq
     inside one (run_id, request_id), and each affected request's
     reconstructed state machine over committed + staged events.
  3. Only if every check passes are staged rows copied into the durable
     ``events`` table and COMMITed.  Any problem -- including a process
     kill before commit -- rolls the whole batch back, so the ledger can
     never hold a partial batch or half a request's state.

Replaying an already-known event_id with byte-identical content is a
no-op (it adds no rows); the same id with different content rejects the
batch.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Union

from .errors import (
    EventConflictError,
    EventProblem,
    LedgerNotFoundError,
    ValidationError,
)
from .statemachine import (
    OUTCOME_CANCELLED,
    OUTCOME_FAILED,
    OUTCOME_QUEUED,
    OUTCOME_RUNNING,
    OUTCOME_SUCCESS,
    OUTCOME_WAITING_RETRY,
    validate_stream,
    final_response_time,
)
from .validate import parse_event_time, parse_line

PathLike = Union[str, "Path"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    request_id    TEXT NOT NULL,
    attempt       INTEGER NOT NULL CHECK (attempt >= 1),
    seq           INTEGER NOT NULL CHECK (seq >= 1),
    event_type    TEXT NOT NULL,
    event_time    TEXT NOT NULL,            -- original ISO-8601 string
    event_time_us INTEGER NOT NULL,         -- same instant, epoch us
    payload       TEXT NOT NULL,            -- full original event JSON
    PRIMARY KEY (run_id, request_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_event_id ON events(event_id);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, request_id, seq);
"""

_STAGING = """
CREATE TEMP TABLE IF NOT EXISTS staging (
    line_no       INTEGER PRIMARY KEY,
    event_id      TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    request_id    TEXT NOT NULL,
    attempt       INTEGER NOT NULL,
    seq           INTEGER NOT NULL,
    event_type    TEXT NOT NULL,
    event_time    TEXT NOT NULL,
    event_time_us INTEGER NOT NULL,
    payload       TEXT NOT NULL,
    payload_sig   TEXT NOT NULL
)
"""


@dataclass
class ImportResult:
    """Outcome of importing one batch file."""

    path: str
    lines_seen: int = 0
    events_inserted: int = 0
    events_replayed: int = 0          # identical event_id, no new rows
    committed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "lines_seen": self.lines_seen,
            "events_inserted": self.events_inserted,
            "events_replayed": self.events_replayed,
            "committed": self.committed,
        }


@dataclass
class RequestTimeline:
    """Reconstructed life of one request inside one run."""

    run_id: str
    request_id: str
    outcome: str
    attempt_count: int
    final_status_code: Optional[int]
    first_started_time: Optional[str]
    final_time: Optional[str]
    duration_seconds: Optional[float]
    waiting_next_attempt: Optional[int]
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "request_id": self.request_id,
            "outcome": self.outcome,
            "attempt_count": self.attempt_count,
            "final_status_code": self.final_status_code,
            "first_started_time": self.first_started_time,
            "final_time": self.final_time,
            "duration_seconds": self.duration_seconds,
            "waiting_next_attempt": self.waiting_next_attempt,
            "events": self.events,
        }


@dataclass
class RunSummary:
    """Aggregate view of one run."""

    run_id: str
    requests_total: int
    success: int
    failed: int
    cancelled: int
    waiting_retry: int
    running: int
    queued: int
    # Success rate denominator = requests with a definitive success or
    # failure response/failure outcome (cancelled and unfinished excluded).
    decided: int
    success_rate: Optional[float]
    # Durations exist only for requests with both first started and a
    # terminal response/failed; unfinished requests are never timed.
    durations_seconds: list[float] = field(default_factory=list)
    request_ids: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "run_id": self.run_id,
            "requests_total": self.requests_total,
            "success": self.success,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "waiting_retry": self.waiting_retry,
            "running": self.running,
            "queued": self.queued,
            "decided": self.decided,
            "success_rate": self.success_rate,
            "duration_seconds": {
                "count": len(self.durations_seconds),
                "min": min(self.durations_seconds) if self.durations_seconds else None,
                "max": max(self.durations_seconds) if self.durations_seconds else None,
                "avg": (sum(self.durations_seconds) / len(self.durations_seconds)
                        if self.durations_seconds else None),
                "values": list(self.durations_seconds),
            },
            "request_ids": self.request_ids,
        }
        return d


class Ledger:
    """Open or create an event ledger at ``db_path``."""

    def __init__(self, db_path: PathLike):
        self.db_path = str(db_path)
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        # Keep TEMP tables (the streaming staging area) on disk rather
        # than in RAM, so importing a large file cannot balloon process
        # memory; and use the rollback journal (not WAL) so an external
        # kill before COMMIT leaves no partial transaction behind.
        self.conn.execute("PRAGMA temp_store=FILE")
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with self.conn:
            self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ #
    # Import
    # ------------------------------------------------------------------ #
    def import_file(self, jsonl_path: PathLike,
                    pre_commit_hook: Optional[Callable[[], None]] = None
                    ) -> ImportResult:
        """Stream-validate and atomically import one JSONL batch.

        ``pre_commit_hook`` (used by the crash drill) runs inside the
        transaction after all validation and before COMMIT; raising or
        exiting the process there leaves zero imported rows.
        """
        path = os.fspath(jsonl_path)
        result = ImportResult(path=path)
        conn = self.conn
        problems: list[EventProblem] = []

        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DROP TABLE IF EXISTS temp.staging")
            conn.execute("DROP TABLE IF EXISTS temp.staging_new")
            conn.execute(_STAGING)
            # Dedup identity inside the batch is delegated to the staging
            # table's unique indexes, not Python sets, so memory stays
            # bounded while the file is streamed line by line.
            conn.execute(
                "CREATE UNIQUE INDEX ux_staging_key ON staging "
                "(run_id, request_id, seq)")
            conn.execute(
                "CREATE UNIQUE INDEX ux_staging_event_id ON staging "
                "(event_id)")
            intra_replays = 0

            insert_staging = conn.cursor()
            insert_sql = (
                "INSERT INTO temp.staging (line_no, event_id, run_id, "
                "request_id, attempt, seq, event_type, event_time, "
                "event_time_us, payload, payload_sig) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)")
            with open(path, "r", encoding="utf-8") as fh:
                for line_no, raw in enumerate(fh, start=1):
                    if raw.endswith("\n"):
                        raw = raw[:-1]
                    event, problem = parse_line(raw, line_no)
                    if problem is not None:
                        problems.append(problem)
                        continue
                    result.lines_seen += 1

                    payload = json.dumps(event, sort_keys=True,
                                         ensure_ascii=False,
                                         separators=(",", ":"))
                    # A byte-identical event_id already staged in this
                    # batch is an intra-batch replay: count it and move on.
                    prev = conn.execute(
                        "SELECT payload_sig FROM temp.staging "
                        "WHERE event_id=?",
                        (event["event_id"],)).fetchone()
                    if prev is not None:
                        if prev["payload_sig"] == payload:
                            intra_replays += 1
                        else:
                            problems.append(EventProblem(
                                reason="same event_id used with different "
                                       "content inside the batch",
                                line=line_no, run_id=event["run_id"],
                                request_id=event["request_id"],
                                event_id=event["event_id"]))
                        continue
                    dt = parse_event_time(event["event_time"])
                    us = int(dt.timestamp() * 1_000_000)
                    try:
                        insert_staging.execute(insert_sql, (
                            line_no, event["event_id"], event["run_id"],
                            event["request_id"], event["attempt"],
                            event["seq"], event["event_type"],
                            event["event_time"], us,
                            json.dumps(event, ensure_ascii=False), payload))
                    except sqlite3.IntegrityError:
                        # Distinct event_id but a repeated (run, request,
                        # seq): the key unique index rejects it.
                        problems.append(EventProblem(
                            reason=f"duplicate seq {event['seq']} within "
                                   f"batch for request",
                            line=line_no, run_id=event["run_id"],
                            request_id=event["request_id"],
                            event_id=event["event_id"]))

            if result.lines_seen == 0 and not problems:
                problems.append(EventProblem(reason="file contains no events"))

            # Partition staged rows into replays (event_id already known
            # with identical content) and genuinely new rows.  All later
            # checks and the state-machine replay use staging_new only.
            conn.execute(
                "CREATE TEMP TABLE staging_new AS "
                "SELECT s.* FROM temp.staging s WHERE NOT EXISTS ("
                "SELECT 1 FROM events e WHERE e.event_id = s.event_id "
                "AND e.payload = s.payload_sig)")
            replayed = conn.execute(
                "SELECT COUNT(*) c FROM temp.staging s WHERE EXISTS ("
                "SELECT 1 FROM events e WHERE e.event_id = s.event_id "
                "AND e.payload = s.payload_sig)").fetchone()["c"]
            replayed += intra_replays

            # Identity/seq checks are set-based (constant SQL, no
            # Python-sized IN lists and no per-request round trips).
            self._check_event_identity(conn, problems)
            self._check_committed_seq(conn, problems)
            if problems:
                conn.rollback()
                if any("different content" in p.reason for p in problems):
                    raise EventConflictError(problems)
                raise ValidationError(problems)

            # One ordered cursor, grouped per request: total work is
            # linear in the affected event count with at most one
            # request's events held in memory at a time.
            for run_id, request_id, group in self._stream_merged_groups(conn):
                state = validate_stream(run_id, request_id, group)
                state.final_time = final_response_time(group, state)
                line_by_id = {e["event_id"]: e.get("_line") for e in group}
                for p in state.problems:
                    if p.line is None and p.event_id in line_by_id:
                        object.__setattr__(p, "line", line_by_id[p.event_id])
                    problems.append(p)
            if problems:
                conn.rollback()
                raise ValidationError(problems)

            # Single set-based merge; staging_new contains new ids only.
            inserted = conn.execute(
                "INSERT INTO events (event_id, run_id, request_id, attempt, "
                "seq, event_type, event_time, event_time_us, payload) "
                "SELECT event_id, run_id, request_id, attempt, seq, "
                "event_type, event_time, event_time_us, payload_sig "
                "FROM staging_new").rowcount
            result.events_inserted = inserted
            result.events_replayed = replayed

            if pre_commit_hook is not None:
                pre_commit_hook()

            conn.commit()
            result.committed = True
            return result
        except ValidationError:
            raise
        except Exception:
            conn.rollback()
            raise

    def _check_event_identity(self, conn: sqlite3.Connection,
                              problems: list[EventProblem]) -> None:
        # Same event_id, different canonical content vs committed rows.
        rows = conn.execute(
            "SELECT s.line_no, s.run_id, s.request_id, s.event_id "
            "FROM staging_new s JOIN events e ON e.event_id = s.event_id "
            "WHERE e.payload != s.payload_sig").fetchall()
        for r in rows:
            problems.append(EventProblem(
                reason="event_id already exists with different content",
                line=r["line_no"], run_id=r["run_id"],
                request_id=r["request_id"], event_id=r["event_id"]))

    def _check_committed_seq(self, conn: sqlite3.Connection,
                             problems: list[EventProblem]) -> None:
        rows = conn.execute(
            "SELECT s.line_no, s.run_id, s.request_id, s.seq, s.event_id "
            "FROM staging_new s JOIN events e "
            "ON e.run_id=s.run_id AND e.request_id=s.request_id "
            "AND e.seq=s.seq").fetchall()
        for r in rows:
            problems.append(EventProblem(
                reason=f"seq {r['seq']} of request already exists "
                       f"(duplicate seq across batches)",
                line=r["line_no"], run_id=r["run_id"],
                request_id=r["request_id"], event_id=r["event_id"]))

    def _stream_merged_groups(
        self, conn: sqlite3.Connection
    ) -> Iterator[tuple[str, str, list[dict[str, Any]]]]:
        """Yield each affected request's committed + new events in seq
        order, using one forward-only cursor.

        A UNION ALL over the durable table and the new-only staging
        table, ordered by (run_id, request_id, seq), lets us group rows
        in Python with memory bounded by one request's event count; the
        file itself is never materialised as a whole.
        """
        sql = (
            "SELECT run_id, request_id, seq, payload, line_no FROM ("
            "  SELECT run_id, request_id, seq, payload, "
            "         CAST(NULL AS INTEGER) AS line_no FROM events "
            "  WHERE (run_id, request_id) IN ("
            "      SELECT DISTINCT run_id, request_id FROM staging_new)"
            "  UNION ALL "
            "  SELECT run_id, request_id, seq, payload_sig AS payload, "
            "         line_no FROM staging_new"
            ") ORDER BY run_id, request_id, seq"
        )
        current_key: Optional[tuple[str, str]] = None
        group: list[dict[str, Any]] = []
        for r in conn.execute(sql):
            key = (r["run_id"], r["request_id"])
            if current_key is not None and key != current_key:
                yield current_key[0], current_key[1], group
                group = []
            current_key = key
            event = json.loads(r["payload"])
            event["_line"] = r["line_no"]
            group.append(event)
        if current_key is not None:
            yield current_key[0], current_key[1], group

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def _request_events(self, run_id: str, request_id: str,
                        check_exists: bool = True) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT payload FROM events WHERE run_id=? AND request_id=? "
            "ORDER BY attempt, seq", (run_id, request_id)).fetchall()
        if check_exists and not rows:
            raise LedgerNotFoundError(
                f"no events for run_id={run_id!r} request_id={request_id!r}")
        return [json.loads(r["payload"]) for r in rows]

    def request_timeline(self, run_id: str, request_id: str) -> RequestTimeline:
        events = self._request_events(run_id, request_id)
        state = validate_stream(run_id, request_id,
                                [{**e, "_line": None} for e in events])
        final_time = final_response_time(
            [{**e, "_line": None} for e in events], state)
        if state.terminal_event == "failed":
            final_time = state.final_time
        duration = None
        if state.first_started_time is not None and final_time is not None:
            start = parse_event_time(state.first_started_time)
            end = parse_event_time(final_time)
            duration = (end - start).total_seconds()
        waiting = state.next_attempt_expected if state.waiting_retry else None
        return RequestTimeline(
            run_id=run_id, request_id=request_id, outcome=state.outcome,
            attempt_count=max(state.started_attempts) if state.started_attempts
            else (1 if state.last_attempt else 0),
            final_status_code=state.final_status_code,
            first_started_time=state.first_started_time,
            final_time=final_time,
            duration_seconds=duration,
            waiting_next_attempt=waiting,
            events=events)

    def list_requests(self, run_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT request_id FROM events WHERE run_id=? "
            "ORDER BY request_id", (run_id,)).fetchall()
        if not rows:
            raise LedgerNotFoundError(f"no events for run_id={run_id!r}")
        return [r["request_id"] for r in rows]

    def list_runs(self) -> list[str]:
        return [r["run_id"] for r in self.conn.execute(
            "SELECT DISTINCT run_id FROM events ORDER BY run_id")]

    def run_summary(self, run_id: str) -> RunSummary:
        buckets: dict[str, list[str]] = {
            OUTCOME_SUCCESS: [], OUTCOME_FAILED: [], OUTCOME_CANCELLED: [],
            OUTCOME_WAITING_RETRY: [], OUTCOME_RUNNING: [], OUTCOME_QUEUED: []}
        durations: list[float] = []
        total = 0
        saw_any = False
        # Single forward-only cursor grouped per request: O(events) with
        # one request's events in memory at a time, not one query per
        # request.
        sql = ("SELECT request_id, seq, payload FROM events WHERE run_id=? "
               "ORDER BY request_id, seq")
        current_request: Optional[str] = None
        group: list[dict[str, Any]] = []

        def flush(request_id: Optional[str],
                  grp: list[dict[str, Any]]) -> None:
            if request_id is None:
                return
            state = validate_stream(run_id, request_id, grp)
            final_time = final_response_time(grp, state)
            if state.terminal_event == "failed":
                final_time = state.final_time
            buckets[state.outcome].append(request_id)
            if state.first_started_time is not None and final_time is not None:
                duration = (parse_event_time(final_time)
                            - parse_event_time(state.first_started_time)
                            ).total_seconds()
                durations.append(duration)

        for r in self.conn.execute(sql, (run_id,)):
            saw_any = True
            if current_request is not None and r["request_id"] != current_request:
                flush(current_request, group)
                group = []
                total += 1
            current_request = r["request_id"]
            group.append({**json.loads(r["payload"]), "_line": None})
        if current_request is not None:
            flush(current_request, group)
            total += 1
        if not saw_any:
            raise LedgerNotFoundError(f"no events for run_id={run_id!r}")
        decided = len(buckets[OUTCOME_SUCCESS]) + len(buckets[OUTCOME_FAILED])
        rate = (len(buckets[OUTCOME_SUCCESS]) / decided) if decided else None
        return RunSummary(
            run_id=run_id, requests_total=total,
            success=len(buckets[OUTCOME_SUCCESS]),
            failed=len(buckets[OUTCOME_FAILED]),
            cancelled=len(buckets[OUTCOME_CANCELLED]),
            waiting_retry=len(buckets[OUTCOME_WAITING_RETRY]),
            running=len(buckets[OUTCOME_RUNNING]),
            queued=len(buckets[OUTCOME_QUEUED]),
            decided=decided, success_rate=rate,
            durations_seconds=durations, request_ids=buckets)

    def event_count(self, run_id: Optional[str] = None) -> int:
        if run_id is None:
            return self.conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        return self.conn.execute(
            "SELECT COUNT(*) c FROM events WHERE run_id=?",
            (run_id,)).fetchone()["c"]

    # ------------------------------------------------------------------ #
    # Deterministic JSONL export
    # ------------------------------------------------------------------ #
    def export_jsonl(self, out_path: PathLike,
                     run_id: Optional[str] = None) -> int:
        """Write all events (or one run's) in a stable order.

        Order: run_id, request_id, seq -- the primary-key order -- and
        every line is canonical JSON with sorted object keys, so repeated
        exports of the same ledger are byte-identical.  Re-importing the
        export adds no events because each event_id keeps its content.
        """
        sql = ("SELECT payload FROM events")
        params: tuple = ()
        if run_id is not None:
            sql += " WHERE run_id=?"
            params = (run_id,)
        sql += " ORDER BY run_id, request_id, seq"
        count = 0
        with open(os.fspath(out_path), "w", encoding="utf-8", newline="\n") as fh:
            for r in self.conn.execute(sql, params):
                event = json.loads(r["payload"])
                fh.write(json.dumps(event, sort_keys=True,
                                    ensure_ascii=False, separators=(",", ":")))
                fh.write("\n")
                count += 1
        return count

    def iter_events(self, run_id: Optional[str] = None
                    ) -> Iterator[dict[str, Any]]:
        sql = "SELECT payload FROM events"
        params: tuple = ()
        if run_id is not None:
            sql += " WHERE run_id=?"
            params = (run_id,)
        sql += " ORDER BY run_id, request_id, seq"
        for r in self.conn.execute(sql, params):
            yield json.loads(r["payload"])

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
