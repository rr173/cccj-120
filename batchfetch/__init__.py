"""batchfetch -- budgeted batch web fetcher (stdlib HTTP, local-first).

Given a JSONL manifest of ``{job_id, url}`` candidates and per-origin
budgets, run the whole batch with real HTTP requests while enforcing:

* a global in-flight cap and per-origin in-flight caps;
* a minimum interval between two request starts addressed to one origin,
  measured on a monotonic clock at the *actual* start instant (never at
  enqueue time);
* round-robin dispatch across the eligible origins with strict FIFO order
  inside each origin -- redirect hops re-join the target origin's queue;
* one-request timeouts, a streaming response body byte cap (SHA-256 and
  byte count computed chunk by chunk, body never retained);
* explicit, manual redirect handling (301/302/303/307/308 only), with
  relative ``Location`` resolution, loop/limit checks and full hop chain
  in the final result;
* cooperative cancellation (API ``cancel()`` or CLI ``SIGINT``): queued
  work never starts, in-flight exchanges finish within their timeout.

Nothing here changes sitemap discovery or robots admission; this module
only consumes an already accepted candidate list.
"""

from .clock import Clock, FakeClock, MonotonicClock
from .config import (BatchConfig, JobSpec, OriginBudget, config_from_mapping,
                     parse_manifest, read_manifest)
from .errors import (BatchConfigError, BatchFetchError, BatchRunError,
                     ManifestError)
from .exchange import (REDIRECT_STATUSES, ExchangeError, ExchangeResult,
                       perform_exchange)
from .models import (CANCELLED, COMPLETE, FAILED, HopRecord, JobResult,
                     TimelineEvent)
from .scheduler import BatchRunner

__all__ = [
    "BatchRunner",
    "BatchConfig",
    "OriginBudget",
    "JobSpec",
    "JobResult",
    "HopRecord",
    "TimelineEvent",
    "Clock",
    "MonotonicClock",
    "FakeClock",
    "ExchangeResult",
    "ExchangeError",
    "perform_exchange",
    "read_manifest",
    "parse_manifest",
    "config_from_mapping",
    "BatchFetchError",
    "BatchConfigError",
    "ManifestError",
    "BatchRunError",
    "COMPLETE",
    "FAILED",
    "CANCELLED",
    "REDIRECT_STATUSES",
    "run_batch",
]

__version__ = "1.0.0"


def run_batch(manifest_path: str, config: BatchConfig, report_path: str,
              *, clock: Clock | None = None) -> dict:
    """One-call convenience used by the CLI and by Python callers.

    Validates the manifest and configuration, runs the batch, writes the
    JSONL report (summary, per-job results in input order, exchange
    timeline) to ``report_path`` and returns the summary dictionary.
    """
    jobs = read_manifest(manifest_path)
    runner = BatchRunner(jobs, config, clock=clock)
    try:
        runner.run()
        with open(report_path, "w", encoding="utf-8") as fh:
            runner.write_report(fh)
        return runner.summary()
    finally:
        runner.close()
