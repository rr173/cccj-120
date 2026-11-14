"""The batch executor: pacing, slots, redirects, cancellation, reporting.

Concurrency model
-----------------
There is one **scheduler thread** and a fixed pool of ``global_inflight``
worker threads.  Only the scheduler decides which exchange may start;
workers never reserve a global slot and then sleep for an origin gap --
a work item is handed to a worker only when *both* the global cap and the
target origin's cap and interval already permit the start, so every sleep
happens inside the scheduler while no slot of any kind is held by the
waiting job.

Per origin, work items are a FIFO: the earliest runnable job (including
redirect hops re-enqueued at their real target origin) goes first, and a
redirect can never overtake jobs already queued there.  When the head
origin is blocked on its interval or on a free connection, the scheduler
keeps scanning the other eligible origins round-robin instead of parking
on that head.

Redirects are ordinary work items of the same job: after each hop the
origin slots used by that hop are released before the next hop is queued,
so a cross-origin redirect never holds the old origin's budget while
waiting for the new one.

Cancellation
------------
After :meth:`BatchRunner.cancel` no queued request and no new redirect
starts.  Exchanges already in flight run to their own completion (bounded
by the configured request timeout) and keep their real results; everything
that never started is recorded as ``cancelled``.
"""

from __future__ import annotations

import json
import queue
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence
from urllib.parse import urljoin

from sitemapdiscover.urls import parse_url

from .clock import Clock, MonotonicClock
from .config import BatchConfig, JobSpec
from .errors import BatchConfigError, BatchRunError
from .exchange import ExchangeResult, perform_exchange
from .models import (CANCELLED, COMPLETE, FAILED, HopRecord, JobResult,
                     TimelineEvent)

ExchangeFn = Callable[..., ExchangeResult]


@dataclass
class _WorkItem:
    """One request waiting to run (initial request or a redirect hop)."""

    job_id: str
    hop: int
    url: str
    origin: str
    enqueued_mono: float
    waits: dict = field(default_factory=dict)


@dataclass
class _OriginState:
    queue: deque[_WorkItem] = field(default_factory=deque)
    inflight: int = 0
    last_start: Optional[float] = None


class BatchRunner:
    """Execute one manifest under one validated :class:`BatchConfig`.

    Parameters
    ----------
    jobs:
        validated manifest jobs in reception order.
    config:
        a :meth:`~batchfetch.config.BatchConfig.validated` configuration.
    clock:
        injectable monotonic clock (defaults to the real clock).
    exchange_fn:
        the function performing one HTTP exchange; override in tests with
        a scripted fake.  It must accept the same keyword arguments as
        :func:`batchfetch.exchange.perform_exchange`.
    """

    def __init__(self, jobs: Sequence[JobSpec], config: BatchConfig, *,
                 clock: Optional[Clock] = None,
                 exchange_fn: Optional[ExchangeFn] = None):
        if not jobs:
            raise BatchConfigError("cannot run an empty batch")
        if not isinstance(config, BatchConfig):
            raise BatchConfigError("config must be a BatchConfig")
        self.jobs = list(jobs)
        self.config = config
        self.clock = clock or MonotonicClock()
        self._exchange_fn = exchange_fn or perform_exchange
        self._job_index = {job.job_id: job for job in self.jobs}

        self._cond = self.clock.condition()
        self._origins: dict[str, _OriginState] = {}
        self._origin_order: list[str] = []
        self._rr_cursor = 0

        self.global_inflight = 0
        self._workers: list[threading.Thread] = []
        self._work_q: "queue.Queue[Optional[_WorkItem]]" = queue.Queue()
        self._idle_workers = config.global_inflight

        self._results: dict[str, JobResult] = {}
        self._job_hops: dict[str, list[HopRecord]] = {}
        self._timeline: list[TimelineEvent] = []
        self._cancelled = False
        self._cancel_mono: Optional[float] = None
        self._ran = False
        self._closed = False
        self._total_exchanges = 0
        self._start_mono: Optional[float] = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def run(self) -> "BatchRunner":
        if self._ran:
            raise BatchRunError("a BatchRunner can only be run once")
        self._ran = True
        self._start_mono = self.clock.now()
        self._timeline.append(TimelineEvent(
            kind="batch_start", mono=self._start_mono,
            wall=self.clock.wall(),
            detail={"jobs": len(self.jobs),
                    "global_inflight": self.config.global_inflight}))
        for job in self.jobs:
            self._enqueue(job.job_id, hop=0, url=job.url, origin=job.origin)
        for worker_id in range(self.config.global_inflight):
            thread = threading.Thread(
                target=self._worker_loop, name=f"batchfetch-w{worker_id}",
                daemon=True)
            thread.start()
            self._workers.append(thread)
        try:
            self._scheduler_loop()
        finally:
            for _ in self._workers:
                self._work_q.put(None)
            for thread in self._workers:
                thread.join()
            self._timeline.append(TimelineEvent(
                kind="batch_end", mono=self.clock.now(),
                wall=self.clock.wall(),
                detail={"cancelled": self._cancelled,
                        "exchanges": self._total_exchanges}))
        return self

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

    def __enter__(self) -> "BatchRunner":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # cancellation
    # ------------------------------------------------------------------
    def cancel(self) -> None:
        """Cancel the batch (safe from any thread, e.g. a SIGINT handler).

        Already-running exchanges keep running to completion within their
        configured timeout; nothing queued starts afterwards.
        """
        with self._cond:
            if self._cancelled:
                return
            self._cancelled = True
            self._cancel_mono = self.clock.now()
            self._timeline.append(TimelineEvent(
                kind="cancelled", mono=self._cancel_mono,
                wall=self.clock.wall(),
                detail={"global_inflight": self.global_inflight}))
            # Every still-queued item becomes a terminal cancellation.
            for origin, state in self._origins.items():
                while state.queue:
                    item = state.queue.popleft()
                    self._mark_cancelled(item)
            self.clock.wake_all(self._cond)

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    # ------------------------------------------------------------------
    # enqueue / state helpers (caller holds the condition)
    # ------------------------------------------------------------------
    def _origin_state(self, origin: str) -> _OriginState:
        state = self._origins.get(origin)
        if state is None:
            state = _OriginState()
            self._origins[origin] = state
            self._origin_order.append(origin)
        return state

    def _enqueue(self, job_id: str, *, hop: int, url: str,
                 origin: str) -> None:
        with self._cond:
            state = self._origin_state(origin)
            state.queue.append(_WorkItem(
                job_id=job_id, hop=hop, url=url, origin=origin,
                enqueued_mono=self.clock.now()))
            self.clock.wake_all(self._cond)

    def _record_event(self, event: TimelineEvent) -> None:
        self._timeline.append(event)

    # ------------------------------------------------------------------
    # scheduler
    # ------------------------------------------------------------------
    def _pending_count(self) -> int:
        return sum(len(state.queue) for state in self._origins.values())

    def _all_terminal(self) -> bool:
        return (len(self._results) == len(self.jobs)
                and self.global_inflight == 0
                and self._pending_count() == 0)

    def _scheduler_loop(self) -> None:
        with self._cond:
            while True:
                if self._all_terminal():
                    return
                if self._cancelled:
                    # In-flight exchanges still run in workers; wait for
                    # their completion events.
                    self._cond.wait(None)
                    continue
                if self._idle_workers == 0 or self._pending_count() == 0:
                    self._cond.wait(None)
                    continue
                dispatched, wait_deadline = self._dispatch_locked()
                if dispatched:
                    continue
                # Nothing dispatchable right now: explain why on the
                # timeline, then wait until the nearest origin interval
                # matures (or until a worker finishes / cancel arrives).
                self._record_wait_event(wait_deadline)
                self._cond.wait(None if wait_deadline is None
                                else max(0.0, wait_deadline
                                         - self.clock.now()))

    def _record_wait_event(self, wait_deadline: Optional[float]) -> None:
        blocked: list[dict] = []
        for origin in self._origin_order:
            state = self._origins[origin]
            if not state.queue:
                continue
            head = state.queue[0]
            budget = self.config.budget_for(origin)
            reason = (
                "origin_interval"
                if state.last_start is not None
                and self.clock.now() < (
                    state.last_start + budget.min_interval_seconds)
                else "origin_inflight"
                if state.inflight >= budget.inflight_cap
                else "global_inflight")
            blocked.append({
                "origin": origin,
                "job_id": head.job_id,
                "hop": head.hop,
                "reason": reason,
            })
        self._record_event(TimelineEvent(
            kind="wait", mono=self.clock.now(), wall=self.clock.wall(),
            detail={
                "global_inflight": self.global_inflight,
                "global_cap": self.config.global_inflight,
                "blocked": blocked,
                "next_ready_mono": wait_deadline,
            }))

    def _dispatch_locked(self) -> tuple[bool, Optional[float]]:
        """Hand out every currently legal start. Returns (did_work, next
        monotonic instant at which pacing could change)."""
        did_work = False
        nearest_ready: Optional[float] = None
        origins = self._origin_order
        if not origins:
            return False, None
        n = len(origins)
        # Round-robin: resume just after the origin served last time so the
        # eligible origins take turns instead of the first one dominating.
        for offset in range(n):
            if self._idle_workers == 0 or self.global_inflight >= \
                    self.config.global_inflight:
                break
            origin = origins[(self._rr_cursor + offset) % n]
            state = self._origins[origin]
            if not state.queue:
                continue
            head = state.queue[0]
            budget = self.config.budget_for(origin)
            ready_at = (state.last_start + budget.min_interval_seconds
                        if state.last_start is not None else None)
            now = self.clock.now()
            if state.inflight >= budget.inflight_cap:
                head.waits["origin_inflight"] = \
                    head.waits.get("origin_inflight", 0) + 1
                continue
            if ready_at is not None and now < ready_at:
                head.waits["origin_interval"] = \
                    head.waits.get("origin_interval", 0) + 1
                if nearest_ready is None or ready_at < nearest_ready:
                    nearest_ready = ready_at
                continue
            if self.global_inflight >= self.config.global_inflight:
                head.waits["global_inflight"] = \
                    head.waits.get("global_inflight", 0) + 1
                break
            # Dispatch this head item: both caps and the interval permit it.
            state.queue.popleft()
            state.inflight += 1
            state.last_start = now
            self.global_inflight += 1
            self._idle_workers -= 1
            self._rr_cursor = (self._rr_cursor + offset + 1) % n
            self._work_q.put(head)
            did_work = True
        return did_work, nearest_ready

    # ------------------------------------------------------------------
    # workers
    # ------------------------------------------------------------------
    def _worker_loop(self) -> None:
        while True:
            item = self._work_q.get()
            if item is None:
                return
            with self._cond:
                cancelled = self._cancelled
            if cancelled:
                # The item was dispatched (slots taken, origin queue popped)
                # before cancel() ran, so it is not in the sweep's queues;
                # it must never make an HTTP request, and the slots it
                # already owns are released exactly once here.
                with self._cond:
                    self._release_slots(item)
                    self._mark_cancelled(item)
                    self._cond.notify_all()
                continue
            self._run_exchange(item)

    def _run_exchange(self, item: _WorkItem) -> None:
        start_mono = self.clock.now()
        start_wall = self.clock.wall()
        with self._cond:
            occupancy = {
                "global_inflight": self.global_inflight,
                "global_cap": self.config.global_inflight,
                "origin_inflight": self._origins[item.origin].inflight,
                "origin_cap": self.config.budget_for(
                    item.origin).inflight_cap,
            }
            self._record_event(TimelineEvent(
                kind="exchange_start", mono=start_mono, wall=start_wall,
                job_id=item.job_id, hop=item.hop, url=item.url,
                origin=item.origin,
                detail={"occupancy": occupancy,
                        "wait_reasons": dict(item.waits),
                        "queued_for_seconds":
                            round(start_mono - item.enqueued_mono, 6)}))
        try:
            result = self._exchange_fn(
                item.url, item.origin,
                timeout=self.config.request_timeout_seconds,
                max_body_bytes=self.config.max_body_bytes)
        except Exception as exc:  # defensive: a fake must not kill workers
            result = ExchangeResult(
                ok=False, url=item.url, origin=item.origin,
                failure_kind="worker_error", failure_reason=str(exc))
        end_mono = self.clock.now()
        self._handle_exchange_end(item, result, start_mono, end_mono)

    def _handle_exchange_end(self, item: _WorkItem, result: ExchangeResult,
                             start_mono: float, end_mono: float) -> None:
        with self._cond:
            self._total_exchanges += 1
            self._record_event(TimelineEvent(
                kind="exchange_end", mono=end_mono,
                wall=self.clock.wall(), job_id=item.job_id, hop=item.hop,
                url=item.url, origin=item.origin,
                detail={"duration_seconds": round(end_mono - start_mono, 6),
                        "status": result.status, "ok": result.ok,
                        "failure_kind": result.failure_kind,
                        "body_bytes": result.body_bytes,
                        "body_sha256": result.body_sha256}))
            hop = HopRecord(
                hop=item.hop, url=item.url, origin=item.origin,
                status=result.status, location=result.location,
                body_bytes=result.body_bytes,
                body_sha256=result.body_sha256,
                failure_kind=result.failure_kind,
                failure_reason=result.failure_reason)
            self._job_hops.setdefault(item.job_id, []).append(hop)
            # Release every slot this exchange held before deciding what
            # (if anything) is queued next.
            self._release_slots(item)

            if not result.ok:
                self._finish_failed(item, result)
            elif result.is_redirect:
                self._handle_redirect(item, result)
            else:
                self._finish_complete(item, result)
            self._cond.notify_all()

    def _release_slots(self, item: _WorkItem) -> None:
        state = self._origins[item.origin]
        state.inflight -= 1
        self.global_inflight -= 1
        self._idle_workers += 1

    # ------------------------------------------------------------------
    # job terminal transitions
    # ------------------------------------------------------------------
    def _finish_complete(self, item: _WorkItem, result: ExchangeResult) -> None:
        job = self._job(item.job_id)
        self._results[item.job_id] = JobResult(
            job_id=item.job_id, index=job.index,
            original_url=job.raw_url, final_url=item.url,
            outcome=COMPLETE, status=result.status,
            chain=list(self._job_hops[item.job_id]))

    def _finish_failed(self, item: _WorkItem, result: ExchangeResult) -> None:
        job = self._job(item.job_id)
        self._results[item.job_id] = JobResult(
            job_id=item.job_id, index=job.index,
            original_url=job.raw_url, final_url=item.url,
            outcome=FAILED, status=result.status,
            chain=list(self._job_hops[item.job_id]),
            failure_kind=result.failure_kind or "fetch_failed",
            failure_reason=result.failure_reason)

    def _mark_cancelled(self, item: _WorkItem) -> None:
        # Called under the condition; only the first terminal state wins.
        if item.job_id in self._results:
            return
        job = self._job(item.job_id)
        chain = list(self._job_hops.get(item.job_id, ()))
        final_url = chain[-1].url if chain else None
        self._results[item.job_id] = JobResult(
            job_id=item.job_id, index=job.index,
            original_url=job.raw_url, final_url=final_url,
            outcome=CANCELLED, chain=chain,
            failure_kind="cancelled",
            failure_reason="batch cancelled before this request started")

    def _handle_redirect(self, item: _WorkItem, result: ExchangeResult) -> None:
        job_id = item.job_id
        # Cancellation wins over any further hop: no new redirect starts.
        if self._cancelled:
            self._mark_cancelled(item)
            return
        next_hop = item.hop + 1
        if next_hop > self.config.max_redirects:
            self._redirect_failure(
                item, "redirect_limit",
                f"more than max_redirects={self.config.max_redirects} "
                f"redirects")
            return
        raw_location = result.location
        if not raw_location:
            self._redirect_failure(
                item, "redirect_error",
                f"status {result.status} response has no Location header")
            return
        try:
            next_url_text = urljoin(item.url, raw_location)
            parsed = parse_url(next_url_text)
        except Exception as exc:
            self._redirect_failure(
                item, "redirect_error",
                f"illegal Location {raw_location!r}: {exc}")
            return
        next_url = parsed.identity
        if not next_url.startswith(("http://", "https://")):
            self._redirect_failure(
                item, "redirect_error",
                f"non-HTTP redirect target {next_url!r}")
            return
        seen = {hop.url for hop in self._job_hops[job_id]}
        if next_url in seen:
            self._redirect_failure(
                item, "redirect_loop",
                f"redirect returns to an already requested URL {next_url!r}")
            return
        # Re-queue at the *real* target origin as an ordinary work item, so
        # it must take its place behind that origin's existing FIFO and
        # re-earn global + origin slots (the old slots are already freed).
        state = self._origin_state(parsed.origin)
        state.queue.append(_WorkItem(
            job_id=job_id, hop=next_hop, url=next_url,
            origin=parsed.origin, enqueued_mono=self.clock.now()))

    def _redirect_failure(self, item: _WorkItem, kind: str,
                          reason: str) -> None:
        job = self._job(item.job_id)
        self._results[item.job_id] = JobResult(
            job_id=item.job_id, index=job.index,
            original_url=job.raw_url, final_url=item.url, outcome=FAILED,
            status=self._job_hops[item.job_id][-1].status,
            chain=list(self._job_hops[item.job_id]),
            failure_kind=kind, failure_reason=reason)

    def _job(self, job_id: str) -> JobSpec:
        return self._job_index[job_id]

    # ------------------------------------------------------------------
    # outputs
    # ------------------------------------------------------------------
    def iter_results(self) -> list[JobResult]:
        """Terminal results in manifest reception order."""
        return [self._results[job.job_id] for job in self.jobs]

    @property
    def timeline(self) -> list[TimelineEvent]:
        return list(self._timeline)

    def summary(self) -> dict:
        results = self._results.values()
        counts = {COMPLETE: 0, FAILED: 0, CANCELLED: 0}
        for result in results:
            counts[result.outcome] += 1
        return {
            "type": "summary",
            "jobs": len(self.jobs),
            "complete": counts[COMPLETE],
            "failed": counts[FAILED],
            "cancelled": counts[CANCELLED],
            "exchanges": self._total_exchanges,
            "origins": sorted(self._origins),
            "cancelled_flag": self._cancelled,
            "success": (counts[FAILED] == 0 and counts[CANCELLED] == 0),
            "config": self.config.to_dict(),
        }

    def write_report(self, out) -> None:
        """Write summary, one result per job (input order), and timeline."""
        if hasattr(out, "write"):
            fh, close = out, False
        else:
            fh, close = open(out, "w", encoding="utf-8"), True
        try:
            fh.write(json.dumps(self.summary(), ensure_ascii=False) + "\n")
            for result in self.iter_results():
                fh.write(json.dumps(result.to_dict(),
                                    ensure_ascii=False) + "\n")
            for event in self._timeline:
                fh.write(json.dumps(event.to_dict(),
                                    ensure_ascii=False) + "\n")
        finally:
            if close:
                fh.close()
