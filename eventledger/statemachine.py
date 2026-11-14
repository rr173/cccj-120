"""Per-request state machine reconstructed from ordered events.

The ledger never stores derived state as an authoritative source: for
every validation and every query it replays the events of one
``(run_id, request_id)`` in ``seq`` order.  Different runs are fully
independent even when they reuse the same request_id.

Lifecycle of one request (attempts start at 1 and increase by one):

    queued(1)
      started(1)
        response(1)                      -> retry_scheduled(next=2) or terminal
        retry_scheduled(1, next=2)
      started(2) ...                      (a retry only *starts* here)
      failed(n)                           terminal
      cancelled(...)                      terminal (before or after started)
    response(n) with no following retry  terminal success/failure outcome

Rules this module enforces on the merged stream (events already on disk
plus the events of the batch being imported):

  * the first event must be ``queued`` with attempt == 1;
  * seq values are strictly increasing (duplicate seq is rejected by the
    caller before replay);
  * attempt never decreases and a new attempt is entered exactly one
    number higher than the previous one;
  * ``started`` is the first event of every attempt > 1, and an attempt
    has at most one started;
  * an attempt gets at most one response / retry_scheduled / failed;
  * retry_scheduled must point at attempt+1, and the following attempt
    can only begin with started;
  * failed/cancelled and a final response (one not followed by a
    retry_scheduled) are terminal: no started/response/retry_scheduled
    may be appended afterwards;
  * nothing may follow failed/cancelled at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .errors import EventProblem

# Derived outcome labels used by summaries and timelines.
OUTCOME_SUCCESS = "success"
OUTCOME_FAILED = "failed"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_WAITING_RETRY = "waiting_retry"
OUTCOME_RUNNING = "running"
OUTCOME_QUEUED = "queued"


@dataclass
class ReplayState:
    """Result of replaying one request's event stream."""

    run_id: str
    request_id: str
    terminal: bool = False
    terminal_event: Optional[str] = None          # failed | cancelled | response
    terminal_attempt: Optional[int] = None
    waiting_retry: bool = False                    # last event is retry_scheduled
    next_attempt_expected: Optional[int] = None    # after retry_scheduled
    last_attempt: Optional[int] = None
    first_started_time: Optional[str] = None
    final_time: Optional[str] = None               # final response / failed instant
    final_status_code: Optional[int] = None
    started_attempts: set[int] = field(default_factory=set)
    problems: list[EventProblem] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        if self.terminal_event == "cancelled":
            return OUTCOME_CANCELLED
        if self.terminal_event == "failed":
            return OUTCOME_FAILED
        if self.terminal_event == "response":
            # A terminal response is a success for 2xx/3xx and a failure
            # otherwise (e.g. a 500 that was not retried).
            if self.final_status_code is not None and self.final_status_code < 400:
                return OUTCOME_SUCCESS
            return OUTCOME_FAILED
        if self.waiting_retry:
            return OUTCOME_WAITING_RETRY
        if self.first_started_time is not None:
            return OUTCOME_RUNNING
        return OUTCOME_QUEUED


def _problem(event: dict[str, Any], reason: str) -> EventProblem:
    return EventProblem(
        reason=reason, line=event.get("_line"),
        run_id=event.get("run_id"), request_id=event.get("request_id"),
        event_id=event.get("event_id"))


def validate_stream(run_id: str, request_id: str,
                    events: list[dict[str, Any]]) -> ReplayState:
    """Replay one request's merged, seq-sorted events.

    Each event dict may carry a private ``_line`` key (1-based source
    line; absent for events already committed).  All detected problems
    are collected on the returned state; the caller rejects the batch if
    any stream has problems.
    """
    st = ReplayState(run_id=run_id, request_id=request_id)
    prev_seq: Optional[int] = None
    attempt_kind: dict[int, set[str]] = {}
    saw_queued = False

    for ev in events:
        et = ev["event_type"]
        attempt = ev["attempt"]
        seq = ev["seq"]

        if prev_seq is not None and seq <= prev_seq:
            st.problems.append(_problem(
                ev, f"seq {seq} is not strictly greater than previous seq "
                    f"{prev_seq}"))
        prev_seq = seq

        if not saw_queued:
            if et != "queued" or attempt != 1:
                st.problems.append(_problem(
                    ev, f"first event of request must be queued with "
                        f"attempt=1, got {et} attempt={attempt}"))
                # Continue so later events still get useful diagnostics.
            if et == "queued":
                saw_queued = True
                if attempt != 1:
                    st.problems.append(_problem(
                        ev, f"queued event must use attempt=1, got {attempt}"))
        elif et == "queued":
            st.problems.append(_problem(
                ev, "duplicate queued event for request"))

        # Terminal lock: once failed/cancelled happened, nothing follows.
        if st.terminal_event in ("failed", "cancelled"):
            st.problems.append(_problem(
                ev, f"event {et} after terminal {st.terminal_event} "
                    f"event of attempt {st.terminal_attempt}"))
            continue

        # Terminal lock for failed/cancelled is absolute.  A terminal
        # response is only locked once the stream moves to a later event
        # (handled per event type below), because a retry_scheduled in
        # the same attempt legitimately revokes the response's terminal
        # status.
        kinds = attempt_kind.setdefault(attempt, set())

        # Attempt ordering.
        if st.last_attempt is not None and attempt < st.last_attempt:
            st.problems.append(_problem(
                ev, f"attempt {attempt} decreases from {st.last_attempt}"))
        if st.waiting_retry:
            expected = st.next_attempt_expected
            if et != "started" or attempt != expected:
                st.problems.append(_problem(
                    ev, f"after retry_scheduled the next event must be "
                        f"started attempt={expected}, got {et} "
                        f"attempt={attempt}"))
            if et == "started" and attempt == expected:
                st.waiting_retry = False
                st.next_attempt_expected = None

        if st.last_attempt is None or attempt > st.last_attempt:
            if st.last_attempt is not None and attempt != st.last_attempt + 1:
                st.problems.append(_problem(
                    ev, f"skipped attempt: jumped from {st.last_attempt} "
                        f"to {attempt}"))
            st.last_attempt = attempt

        if et == "queued":
            if attempt != 1:
                st.problems.append(_problem(
                    ev, f"queued event must use attempt=1, got {attempt}"))
            continue

        if et == "started":
            if "started" in kinds:
                st.problems.append(_problem(
                    ev, f"duplicate started event in attempt {attempt}"))
            kinds.add("started")
            st.started_attempts.add(attempt)
            if st.first_started_time is None:
                st.first_started_time = ev["event_time"]
            # Starting a new attempt after a response that had no
            # retry_scheduled means appending after a terminal response.
            if st.terminal_event == "response":
                st.problems.append(_problem(
                    ev, f"started for attempt {attempt} after terminal "
                        f"response of attempt {st.terminal_attempt}"))
            continue

        if et in ("response", "retry_scheduled", "failed"):
            if "started" not in kinds:
                st.problems.append(_problem(
                    ev, f"{et} in attempt {attempt} without a preceding "
                        f"started event"))
            if et == "response":
                if "response" in kinds:
                    st.problems.append(_problem(
                        ev, f"duplicate response event in attempt {attempt}"))
                kinds.add("response")
                st.final_status_code = ev["status_code"]
                if st.terminal_event == "response":
                    st.problems.append(_problem(
                        ev, f"response after terminal response of attempt "
                            f"{st.terminal_attempt}"))
                else:
                    # Tentatively terminal; revoked below if a
                    # retry_scheduled follows in this same attempt.
                    st.terminal = True
                    st.terminal_event = "response"
                    st.terminal_attempt = attempt
                    st.final_time = ev["event_time"]
            elif et == "retry_scheduled":
                if "retry_scheduled" in kinds:
                    st.problems.append(_problem(
                        ev, f"duplicate retry_scheduled in attempt {attempt}"))
                if st.terminal_event == "response" and (
                        st.terminal_attempt != attempt):
                    st.problems.append(_problem(
                        ev, "retry_scheduled after terminal response of "
                            f"attempt {st.terminal_attempt}"))
                if ev["next_attempt"] != attempt + 1:
                    st.problems.append(_problem(
                        ev, f"retry_scheduled next_attempt="
                            f"{ev['next_attempt']} must point to next "
                            f"attempt {attempt + 1}"))
                if "response" not in kinds:
                    st.problems.append(_problem(
                        ev, "retry_scheduled without a response in the "
                            "same attempt"))
                kinds.add("retry_scheduled")
                # The preceding response was not terminal after all.
                if st.terminal_event == "response":
                    st.terminal = False
                    st.terminal_event = None
                    st.terminal_attempt = None
                    st.final_time = None
                st.waiting_retry = True
                st.next_attempt_expected = attempt + 1
            else:  # failed
                if "failed" in kinds:
                    st.problems.append(_problem(
                        ev, f"duplicate failed event in attempt {attempt}"))
                kinds.add("failed")
                st.terminal = True
                st.terminal_event = "failed"
                st.terminal_attempt = attempt
                st.waiting_retry = False
                st.final_time = ev["event_time"]
            continue

        if et == "cancelled":
            if "cancelled" in kinds:
                st.problems.append(_problem(
                    ev, f"duplicate cancelled event in attempt {attempt}"))
            kinds.add("cancelled")
            st.terminal = True
            st.terminal_event = "cancelled"
            st.terminal_attempt = attempt
            st.waiting_retry = False
            continue

        st.problems.append(_problem(ev, f"unknown event_type {et!r}"))

    # If the stream ends on a response that was not followed by a retry,
    # that response is terminal.
    if not st.terminal and not st.waiting_retry and st.last_attempt is not None:
        kinds = attempt_kind.get(st.last_attempt, set())
        if "response" in kinds:
            st.terminal = True
            st.terminal_event = "response"
            st.terminal_attempt = st.last_attempt
            # final_time is filled by the caller from the last response.
    return st


def final_response_time(events: list[dict[str, Any]],
                        state: ReplayState) -> Optional[str]:
    """Instant of the terminal response (last response of last attempt)."""
    if state.terminal_event != "response":
        return None
    for ev in reversed(events):
        if ev["event_type"] == "response":
            return ev["event_time"]
    return None
