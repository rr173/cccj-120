"""Repeatable, no-network drill for eventledger.

Run from the repository root:

    python3 -I demo/run_eventledger_drill.py

The script is fully self-contained (stdlib only): it writes its own
JSONL fixtures into a fresh temp directory, never reads the encrypted
archive store or any WARC, and prints one labelled section per scenario
with PASS/FAIL assertions and the measured numbers for the large-file
import.  Re-running it starts from an empty ledger every time.

Scenarios (mirrors the task contract):
  1. one run's queue / first attempt / retry result split over several
     files, imported out of order: waiting_retry -> running -> success,
     timeline and run summary checked at every step;
  2. replaying a batch keeps event count and statistics unchanged;
  3. unfinished requests stay queued/running, never failed or timed;
  4. HTTP 500 followed by a successful retry;
  5. cancellation;
  6. timezone offsets still give correct elapsed seconds;
  7. the same request_id used independently in two runs;
  8. stable export, byte-identical re-export, idempotent re-import;
  9. malformed batches: duplicate seq, skipped attempt, append after
     terminal, same event_id with different content, truncated last
     line -- each located with run_id/request_id/line/reason and proven
     to leave no partial batch;
 10. forced exit before COMMIT (separate killed process) then safe
     re-import after restart;
 11. large streaming file: input size, peak memory and actual memory
     growth during import, proving the file is never loaded whole.
"""

from __future__ import annotations

import json
import os
import resource
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from eventledger import (  # noqa: E402
    EventConflictError,
    Ledger,
    ValidationError,
)
from eventledger.statemachine import (  # noqa: E402
    OUTCOME_CANCELLED,
    OUTCOME_QUEUED,
    OUTCOME_RUNNING,
    OUTCOME_SUCCESS,
    OUTCOME_WAITING_RETRY,
)

PASS = "PASS"
FAIL = "FAIL"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    print(f"  [{status}] {label}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def ev(run, req, attempt, seq, etype, etime, **extra):
    base = {"run_id": run, "request_id": req, "attempt": attempt, "seq": seq,
            "event_id": f"{run}:{req}:{seq}", "event_type": etype,
            "event_time": etime}
    base.update(extra)
    return base


def write(path: Path, events) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="eventledger-drill-"))
    print(f"scratch directory: {work}")
    db = work / "ledger.db"

    # ---------------------------------------------------------------- #
    section("1. Split, out-of-order import: waiting_retry -> success")
    # ---------------------------------------------------------------- #
    t = "2026-11-14T09:00:{:02d}Z"
    queue = [ev("R1", "page", 1, 1, "queued", t.format(0))]
    first = [
        ev("R1", "page", 1, 2, "started", t.format(1)),
        ev("R1", "page", 1, 3, "response", t.format(2),
           status_code=503, response_bytes=50),
        ev("R1", "page", 1, 4, "retry_scheduled", t.format(3),
           next_attempt=2),
    ]
    retry = [
        ev("R1", "page", 2, 5, "started", t.format(4)),
        ev("R1", "page", 2, 6, "response", t.format(9),
           status_code=200, response_bytes=2048),
    ]
    f_queue, f_first, f_retry = work / "a_queue.jsonl", work / "b_first.jsonl", \
        work / "c_retry.jsonl"
    write(f_queue, queue)
    write(f_first, first)
    write(f_retry, retry)

    with Ledger(db) as ledger:
        r = ledger.import_file(f_queue)
        check("queue file imported", r.events_inserted == 1)
        tl = ledger.request_timeline("R1", "page")
        check("queued request shows outcome=queued",
              tl.outcome == OUTCOME_QUEUED, tl.outcome)

        ledger.import_file(f_first)
        tl = ledger.request_timeline("R1", "page")
        check("after retry_scheduled outcome=waiting_retry",
              tl.outcome == OUTCOME_WAITING_RETRY, tl.outcome)
        check("waiting for attempt 2", tl.waiting_next_attempt == 2)
        check("no fabricated duration while unfinished",
              tl.duration_seconds is None)
        summary = ledger.run_summary("R1")
        check("summary waiting_retry=1, decided=0, rate=None",
              summary.waiting_retry == 1 and summary.decided == 0
              and summary.success_rate is None)

        # Import the retry file, then prove a late-arriving earlier file
        # (already-known ids) changes nothing.
        ledger.import_file(f_retry)
        before = ledger.event_count()
        ledger.import_file(f_first)  # late duplicate batch (replay)
        check("replayed late batch adds no rows",
              ledger.event_count() == before)

        tl = ledger.request_timeline("R1", "page")
        check("final outcome success", tl.outcome == OUTCOME_SUCCESS,
              tl.outcome)
        check("two attempts reconstructed", tl.attempt_count == 2)
        check("timeline ordered by attempt/seq",
              [e["seq"] for e in tl.events] == [1, 2, 3, 4, 5, 6])
        check("duration 09:00:01 -> 09:00:09 = 8s",
              tl.duration_seconds == 8.0, str(tl.duration_seconds))
        summary = ledger.run_summary("R1")
        check("summary success=1 rate=1.0 duration=[8.0]",
              summary.success == 1 and summary.success_rate == 1.0
              and summary.durations_seconds == [8.0])

    # ---------------------------------------------------------------- #
    section("2. Replaying a whole batch changes nothing")
    # ---------------------------------------------------------------- #
    with Ledger(db) as ledger:
        before_count = ledger.event_count()
        before_summary = ledger.run_summary("R1").to_dict()
        for f in (f_queue, f_first, f_retry):
            r = ledger.import_file(f)
            check(f"replay {f.name} inserts 0",
                  r.events_inserted == 0 and r.events_replayed > 0)
        after_summary = ledger.run_summary("R1").to_dict()
        check("event count unchanged", ledger.event_count() == before_count)
        check("summary unchanged", before_summary == after_summary)

    # ---------------------------------------------------------------- #
    section("3. Unfinished requests never become failed/timed")
    # ---------------------------------------------------------------- #
    f_open = work / "open.jsonl"
    write(f_open, [
        ev("R1", "only-queued", 1, 1, "queued", t.format(0)),
        ev("R1", "in-flight", 1, 1, "queued", t.format(0)),
        ev("R1", "in-flight", 1, 2, "started", t.format(1)),
        ev("R1", "will-retry", 1, 1, "queued", t.format(0)),
        ev("R1", "will-retry", 1, 2, "started", t.format(1)),
        ev("R1", "will-retry", 1, 3, "response", t.format(2),
           status_code=500, response_bytes=4),
        ev("R1", "will-retry", 1, 4, "retry_scheduled", t.format(3),
           next_attempt=2),
    ])
    with Ledger(db) as ledger:
        ledger.import_file(f_open)
        check("only-queued -> queued",
              ledger.request_timeline("R1", "only-queued").outcome
              == OUTCOME_QUEUED)
        inflight = ledger.request_timeline("R1", "in-flight")
        check("in-flight -> running, no duration",
              inflight.outcome == OUTCOME_RUNNING
              and inflight.duration_seconds is None)
        check("will-retry -> waiting_retry",
              ledger.request_timeline("R1", "will-retry").outcome
              == OUTCOME_WAITING_RETRY)
        s = ledger.run_summary("R1")
        check("failed counter stays 0 at end of file", s.failed == 0)

    # ---------------------------------------------------------------- #
    section("4. HTTP 500 then successful retry (distinct request)")
    # ---------------------------------------------------------------- #
    f_500 = work / "http500.jsonl"
    write(f_500, [
        ev("R1", "flaky", 1, 1, "queued", "2026-11-14T10:00:00Z"),
        ev("R1", "flaky", 1, 2, "started", "2026-11-14T10:00:01Z"),
        ev("R1", "flaky", 1, 3, "response", "2026-11-14T10:00:02Z",
           status_code=500, response_bytes=0),
        ev("R1", "flaky", 1, 4, "retry_scheduled", "2026-11-14T10:00:02Z",
           next_attempt=2),
        ev("R1", "flaky", 2, 5, "started", "2026-11-14T10:00:10Z"),
        ev("R1", "flaky", 2, 6, "response", "2026-11-14T10:00:11Z",
           status_code=200, response_bytes=17),
    ])
    with Ledger(db) as ledger:
        ledger.import_file(f_500)
        tl = ledger.request_timeline("R1", "flaky")
        check("500-then-200 is success", tl.outcome == OUTCOME_SUCCESS)
        check("retry duration 10s", tl.duration_seconds == 10.0)

    # ---------------------------------------------------------------- #
    section("5. Cancellation")
    # ---------------------------------------------------------------- #
    f_cancel = work / "cancel.jsonl"
    write(f_cancel, [
        ev("R1", "aborted", 1, 1, "queued", "2026-11-14T11:00:00Z"),
        ev("R1", "aborted", 1, 2, "cancelled", "2026-11-14T11:00:02Z",
           reason="operator abort"),
    ])
    with Ledger(db) as ledger:
        ledger.import_file(f_cancel)
        tl = ledger.request_timeline("R1", "aborted")
        check("cancelled outcome", tl.outcome == OUTCOME_CANCELLED)
        check("cancellation excluded from success-rate denominator",
              ledger.run_summary("R1").decided
              == sum(1 for r in ledger.list_requests("R1")
                     if ledger.request_timeline("R1", r).outcome
                     in ("success", "failed")))

    # ---------------------------------------------------------------- #
    section("6. Timezone offsets: real-instant elapsed time")
    # ---------------------------------------------------------------- #
    f_tz = work / "tz.jsonl"
    write(f_tz, [
        ev("R1", "tzreq", 1, 1, "queued", "2026-11-14T03:00:00+03:00"),
        ev("R1", "tzreq", 1, 2, "started", "2026-11-13T19:00:00-05:00"),
        ev("R1", "tzreq", 1, 3, "response", "2026-11-14T02:00:30+02:00",
           status_code=200, response_bytes=9),
    ])
    with Ledger(db) as ledger:
        ledger.import_file(f_tz)
        tl = ledger.request_timeline("R1", "tzreq")
        check("30s across +03/-05/+02 offsets", tl.duration_seconds == 30.0)
        check("original offset string preserved",
              tl.events[1]["event_time"] == "2026-11-13T19:00:00-05:00")

    # ---------------------------------------------------------------- #
    section("7. Same request_id independent across runs")
    # ---------------------------------------------------------------- #
    f_runs = work / "runs.jsonl"
    write(f_runs, [
        ev("R2", "shared-id", 1, 1, "queued", "2026-11-14T00:00:00Z"),
        ev("R3", "shared-id", 1, 1, "queued", "2026-11-14T00:00:00Z"),
        ev("R3", "shared-id", 1, 2, "started", "2026-11-14T00:00:05Z"),
        ev("R3", "shared-id", 1, 3, "response", "2026-11-14T00:00:06Z",
           status_code=404, response_bytes=1),
    ])
    with Ledger(db) as ledger:
        ledger.import_file(f_runs)
        check("R2/shared-id queued independently",
              ledger.request_timeline("R2", "shared-id").outcome
              == OUTCOME_QUEUED)
        s3 = ledger.run_summary("R3")
        check("R3/shared-id failed (404 terminal), rate 0.0",
              s3.failed == 1 and s3.success_rate == 0.0)

    # ---------------------------------------------------------------- #
    section("8. Stable export and re-import adds no events")
    # ---------------------------------------------------------------- #
    out1, out2 = work / "export1.jsonl", work / "export2.jsonl"
    db2 = work / "ledger2.db"
    with Ledger(db) as ledger:
        n = ledger.export_jsonl(out1)
    with Ledger(db2) as ledger2:
        r = ledger2.import_file(out1)
        check("fresh ledger imports every exported event",
              r.events_inserted == n)
        ledger2.export_jsonl(out2)
        r2 = ledger2.import_file(out2)
        check("re-import of export is all replay",
              r2.events_inserted == 0 and r2.events_replayed == n)
    check("two exports byte-identical",
          out1.read_bytes() == out2.read_bytes())
    # Field-for-field equality with the source fixture events.
    source_records = set()
    for f in (f_queue, f_first, f_retry, f_open, f_500, f_cancel, f_tz,
              f_runs):
        for line in open(f, encoding="utf-8"):
            source_records.add(json.dumps(json.loads(line), sort_keys=True))
    exported_records = {json.dumps(json.loads(line), sort_keys=True)
                        for line in open(out1, encoding="utf-8")}
    check("exported events field-for-field equal to inputs",
          source_records.issubset(exported_records))

    # ---------------------------------------------------------------- #
    section("9. Invalid batches: precise location, no partial import")
    # ---------------------------------------------------------------- #
    def expect_rejection(name: str, events_or_raw, fragment: str,
                         conflict: bool = False):
        path = work / name
        if isinstance(events_or_raw, list):
            write(path, events_or_raw)
        else:
            path.write_text(events_or_raw, encoding="utf-8")
        with Ledger(db) as ledger:
            before = ledger.event_count()
            try:
                ledger.import_file(path)
                check(f"{name}: rejected", False, "no exception raised")
            except (EventConflictError, ValidationError) as exc:
                if conflict and not isinstance(exc, EventConflictError):
                    check(f"{name}: conflict type", False, type(exc).__name__)
                text = json.dumps([p.to_dict() for p in exc.problems],
                                  ensure_ascii=False)
                ok = fragment in text
                check(f"{name}: rejected mentioning {fragment!r}", ok, text)
            check(f"{name}: ledger row count unchanged",
                  ledger.event_count() == before)

    expect_rejection(
        "dup_seq.jsonl",
        [ev("RX", "q", 1, 1, "queued", "2026-11-14T00:00:00Z"),
         ev("RX", "q", 1, 1, "started", "2026-11-14T00:00:01Z",
            **{"event_id": "dup-other"})],
        "duplicate seq")
    expect_rejection(
        "skip_attempt.jsonl",
        [ev("RX", "q", 1, 1, "queued", "2026-11-14T00:00:00Z"),
         ev("RX", "q", 1, 2, "started", "2026-11-14T00:00:01Z"),
         ev("RX", "q", 1, 3, "response", "2026-11-14T00:00:02Z",
            status_code=500, response_bytes=1),
         ev("RX", "q", 1, 4, "retry_scheduled", "2026-11-14T00:00:03Z",
            next_attempt=2),
         ev("RX", "q", 3, 5, "started", "2026-11-14T00:00:04Z")],
        "skipped attempt")
    # Append after an already-committed terminal response.
    terminal = work / "terminal_base.jsonl"
    write(terminal, [
        ev("RX", "done", 1, 1, "queued", "2026-11-14T00:00:00Z"),
        ev("RX", "done", 1, 2, "started", "2026-11-14T00:00:01Z"),
        ev("RX", "done", 1, 3, "response", "2026-11-14T00:00:02Z",
           status_code=200, response_bytes=5),
    ])
    with Ledger(db) as ledger:
        ledger.import_file(terminal)
    expect_rejection(
        "after_terminal.jsonl",
        [ev("RX", "done", 1, 4, "started", "2026-11-14T00:00:09Z",
            **{"event_id": "late-start"})],
        "after terminal")
    # Same event_id, different content.
    id_base = work / "id_base.jsonl"
    write(id_base, [ev("RX", "idq", 1, 1, "queued",
                       "2026-11-14T00:00:00Z")])
    with Ledger(db) as ledger:
        ledger.import_file(id_base)
    changed = ev("RX", "idq", 1, 2, "started", "2026-11-14T00:00:01Z",
                 **{"event_id": "RX:idq:1"})
    expect_rejection("id_conflict.jsonl", [changed],
                     "different content", conflict=True)
    # Truncated last line.
    expect_rejection(
        "truncated.jsonl",
        json.dumps(ev("RX", "tq", 1, 1, "queued",
                      "2026-11-14T00:00:00Z")) + "\n"
        + '{"run_id":"RX","request_id":"tq","attempt":1,"seq":2',
        "invalid JSON")

    # ---------------------------------------------------------------- #
    section("10. Forced exit before COMMIT, then clean restart re-import")
    # ---------------------------------------------------------------- #
    kill_db = work / "killed.db"
    kill_input = work / "killme.jsonl"
    write(kill_input, [ev("RK", "q", 1, 1, "queued",
                          "2026-11-14T00:00:00Z")])
    worker = REPO / "tests" / "_kill_worker.py"
    proc = subprocess.run(
        [sys.executable, "-I", str(worker), str(kill_db), str(kill_input)],
        capture_output=True, text=True)
    check("killed worker did not exit 0", proc.returncode != 0,
          f"returncode={proc.returncode}")
    with Ledger(kill_db) as ledger:
        check("zero rows survived the kill", ledger.event_count() == 0)
        r = ledger.import_file(kill_input)
        check("re-import after restart succeeds",
              r.committed and r.events_inserted == 1)

    # ---------------------------------------------------------------- #
    section("11. Large streaming file: scale and memory measurements")
    # ---------------------------------------------------------------- #
    big = work / "big.jsonl"
    n_requests = 20_000
    events_per_request = 6       # queue, start, 500, retry, start, 200
    total_events = n_requests * events_per_request
    t0 = time.time()
    with open(big, "w", encoding="utf-8") as fh:
        for i in range(n_requests):
            req = f"req-{i:06d}"
            base = i * events_per_request
            for e in (
                ev("BIG", req, 1, base + 1, "queued", "2026-11-14T00:00:00Z"),
                ev("BIG", req, 1, base + 2, "started",
                   "2026-11-14T00:00:01Z"),
                ev("BIG", req, 1, base + 3, "response",
                   "2026-11-14T00:00:02Z", status_code=500,
                   response_bytes=10),
                ev("BIG", req, 1, base + 4, "retry_scheduled",
                   "2026-11-14T00:00:02Z", next_attempt=2),
                ev("BIG", req, 2, base + 5, "started",
                   "2026-11-14T00:00:03Z"),
                ev("BIG", req, 2, base + 6, "response",
                   "2026-11-14T00:00:05Z", status_code=200,
                   response_bytes=100),
            ):
                fh.write(json.dumps(e, separators=(",", ":")) + "\n")
    write_seconds = time.time() - t0
    size_bytes = big.stat().st_size

    big_db = work / "big.db"

    def rss_kb() -> int:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    baseline_rss = rss_kb()
    import time as _time
    start = _time.perf_counter()
    with Ledger(big_db) as ledger:
        r = ledger.import_file(big)
        import_seconds = _time.perf_counter() - start
        peak_rss = rss_kb()
        check("all big-file events inserted",
              r.events_inserted == total_events,
              f"inserted={r.events_inserted}")
        summary = ledger.run_summary("BIG")
        check("all requests succeeded after retry",
              summary.success == n_requests and summary.failed == 0)
        check("every duration 4s (01s -> 05s)",
              set(summary.durations_seconds) == {4.0})
        # Replay cost and idempotency on the big file.
        r2 = ledger.import_file(big)
        check("big-file replay inserts nothing",
              r2.events_inserted == 0 and r2.events_replayed == total_events)

    growth_kb = peak_rss - baseline_rss
    print()
    print(f"  input events ............ {total_events:,} "
          f"({n_requests:,} requests x {events_per_request} events)")
    print(f"  input file size ......... {size_bytes:,} bytes "
          f"({size_bytes / 1_048_576:.2f} MiB)")
    print(f"  file generation ......... {write_seconds:.2f}s")
    print(f"  import wall time ........ {import_seconds:.2f}s "
          f"({total_events / import_seconds:,.0f} events/s)")
    print(f"  RSS before import ....... {baseline_rss:,} KiB "
          f"({baseline_rss / 1_048_576:.1f} MiB)")
    print(f"  peak RSS (rusage) ....... {peak_rss:,} KiB "
          f"({peak_rss / 1_048_576:.1f} MiB)")
    print(f"  actual RSS growth ....... {growth_kb:,} KiB "
          f"({growth_kb / 1_048_576:.2f} MiB)")
    print(f"  growth / input size ..... "
          f"{growth_kb * 1024 / size_bytes:.3f} (ratio; <1 means the "
          f"whole file was never resident)")
    # Streaming claim: peak growth must be far smaller than the file.
    check("peak RSS growth is well below the input size",
          growth_kb * 1024 < size_bytes * 0.25,
          f"growth={growth_kb}KiB vs file={size_bytes}B")
    # SQLite file footprint for context.
    db_size = big_db.stat().st_size
    print(f"  ledger file size ........ {db_size:,} bytes "
          f"({db_size / 1_048_576:.2f} MiB)")

    # ---------------------------------------------------------------- #
    print("\n" + "=" * 72)
    if failures:
        print(f"DRILL RESULT: {len(failures)} FAILURE(S)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("DRILL RESULT: ALL SCENARIOS PASSED")
    print(f"scratch retained at: {work}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
