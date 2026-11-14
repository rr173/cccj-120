"""Injectable clocks and wait primitives.

The scheduler never calls :func:`time.monotonic`, :func:`time.sleep` or
:class:`threading.Condition` directly.  Everything timing-related goes
through a :class:`Clock`, which has two implementations:

* :class:`MonotonicClock` -- the real clock used in production.  ``now()``
  is ``time.monotonic()``; the condition is an ordinary
  :class:`threading.Condition`; ``wait`` really sleeps until the deadline
  or until another thread calls ``wake_all``.
* :class:`FakeClock` -- a virtual clock for unit tests.  Time only moves
  when the test calls :meth:`FakeClock.advance`, so interval/pacing tests
  need no real ``sleep``.  Conditions obtained from one fake clock register
  their wait state with the clock; ``advance`` moves virtual time and
  releases every wait whose deadline has passed (and every wait notified
  by ``wake_all``).

Both clocks also expose :meth:`Clock.wall`, a wall timestamp in the same
units as ``now`` but sourced from :func:`time.time`, used only to annotate
the human-readable timeline.  Under the fake clock ``wall`` follows the
same virtual scale so tests stay deterministic.
"""

from __future__ import annotations

import threading
import time
from typing import Optional


class Clock:
    """Clock + condition factory interface used by the scheduler."""

    def now(self) -> float:
        """Current monotonic time in arbitrary, consistent seconds."""
        raise NotImplementedError

    def wall(self) -> float:
        """Wall-clock epoch seconds (for human-readable annotations)."""
        raise NotImplementedError

    def condition(self) -> "threading.Condition":
        """A condition bound to this clock's time base."""
        raise NotImplementedError

    def wake_all(self, condition: "threading.Condition") -> None:
        """Notify every waiter on ``condition`` without changing time."""
        raise NotImplementedError


class MonotonicClock(Clock):
    """Production clock: real monotonic time, real condition waits."""

    def __init__(self) -> None:
        self._wall_origin = time.time()
        self._mono_origin = time.monotonic()

    def now(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        # Keep the same zero point as now() so offsets line up.
        return self._wall_origin + (time.monotonic() - self._mono_origin)

    def condition(self) -> threading.Condition:
        return threading.Condition()

    def wake_all(self, condition: threading.Condition) -> None:
        with condition:
            condition.notify_all()


class _FakeCondition:
    """Condition stand-in registered with, and driven by, a FakeClock."""

    def __init__(self, clock: "FakeClock") -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._waiters: list[tuple[threading.Event, Optional[float]]] = []

    def __enter__(self) -> "_FakeCondition":
        self._lock.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self._lock.release()

    def wait(self, timeout: Optional[float] = None) -> bool:
        event = threading.Event()
        deadline = None if timeout is None else self._clock.now() + timeout
        self._waiters.append((event, deadline))
        if self._lock._is_owned():  # type: ignore[attr-defined]
            self._lock.release()
        try:
            # Virtual time drives wake-ups: FakeClock.advance releases every
            # waiter (predicates are re-checked by the caller), and the real
            # event timeout is only a deadlock watchdog measured in real
            # seconds, never the virtual interval itself.
            event.wait(timeout=30.0)
            return True
        finally:
            self._lock.acquire()

    def notify_all(self) -> None:
        waiters, self._waiters = self._waiters, []
        for event, _deadline in waiters:
            event.set()

    notify = notify_all


class FakeClock(Clock):
    """Deterministic virtual clock for tests.

    Time advances only through :meth:`advance`.  Waiters bound to this
    clock are released when their deadline passes during an ``advance``,
    or explicitly via :meth:`wake_all`.  The tiny real-time fallback in
    :meth:`advance` only lets released worker threads run; virtual pacing
    never depends on it.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self._now = float(start)
        self._wall = time.time()
        self._lock = threading.RLock()
        self._conditions: list[_FakeCondition] = []

    def now(self) -> float:
        return self._now

    def wall(self) -> float:
        return self._wall

    def condition(self) -> _FakeCondition:
        cond = _FakeCondition(self)
        self._conditions.append(cond)
        return cond

    def wake_all(self, condition) -> None:
        with condition:
            condition.notify_all()

    def advance(self, seconds: float, *, yield_real: float = 0.002) -> None:
        """Move virtual time by ``seconds`` and release due waiters.

        Time movement is itself a state change: every waiter is released
        and re-checks its predicate, exactly as a spurious wake-up on a
        real condition would allow.
        """
        with self._lock:
            self._now += seconds
            self._wall += seconds
            due = [c for c in self._conditions if c._waiters]
        for cond in due:
            with cond:
                cond.notify_all()
        if yield_real:
            time.sleep(yield_real)

    def yield_control(self, times: int = 1, *, yield_real: float = 0.002) -> None:
        """Wake all waiters without moving virtual time (event delivery)."""
        for _ in range(times):
            with self._lock:
                due = [c for c in self._conditions if c._waiters]
            for cond in due:
                with cond:
                    cond.notify_all()
            time.sleep(yield_real)

    def settle(self, *, timeout: float = 5.0, interval: float = 0.005) -> None:
        """Wait until the scheduler is parked or the batch has terminated.

        Test-only barrier: it returns once some condition bound to this
        clock has waiters (the scheduler is blocked and nothing more can
        happen without a clock advance or an external event such as a
        worker finishing), or the same "no waiter" state persists across
        two consecutive observations (the scheduler is moving no more and
        has either terminated or is about to park).
        """
        deadline = time.monotonic() + timeout
        time.sleep(interval)
        idle_streak = 0
        while time.monotonic() < deadline:
            with self._lock:
                waiting = any(c._waiters for c in self._conditions)
            if waiting:
                return
            # No waiter: transient scheduler movement, or run finished.
            self.yield_control(times=1, yield_real=interval)
            time.sleep(interval)
            idle_streak += 1
            if idle_streak >= 3:
                return
        raise TimeoutError("FakeClock.settle: scheduler never parked")
