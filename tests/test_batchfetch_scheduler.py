"""Scheduler unit tests with an injectable clock and scripted exchanges.

No real sockets and no real ``sleep``: a :class:`~batchfetch.FakeClock`
drives pacing, and a scripted exchange function decides each outcome from
the URL.  Real HTTP behavior is covered separately in
``test_batchfetch_http.py`` against the two-port localhost site.
"""

from __future__ import annotations

import threading
import unittest
from typing import Optional

from batchfetch import BatchConfig, BatchRunner, FakeClock, OriginBudget
from batchfetch.config import JobSpec
from batchfetch.exchange import ExchangeResult
from sitemapdiscover.urls import parse_url


def job(job_id: str, url: str, index: int = 0) -> JobSpec:
    parsed = parse_url(url)
    return JobSpec(job_id=job_id, index=index, raw_url=url, parsed=parsed)


class ScriptedExchange:
    """Deterministic exchange fake driven by the test, not the clock.

    Each call appends an :class:`threading.Event` to ``active`` and (unless
    ``auto``) blocks on it, so a test can hold exchanges in flight to
    inspect occupancy.  ``complete_one()`` / ``complete_all()`` release
    calls in start order.
    """

    def __init__(self, rules: Optional[dict] = None, *,
                 clock: Optional[FakeClock] = None, auto: bool = True):
        self.rules = rules or {}
        self.clock = clock
        self.auto = auto
        self.calls: list[dict] = []
        self.active: list[threading.Event] = []
        self._lock = threading.Lock()

    def __call__(self, url: str, origin: str, *, timeout: float,
                 max_body_bytes: int) -> ExchangeResult:
        done = threading.Event()
        with self._lock:
            started = self.clock.now() if self.clock else None
            self.calls.append({"url": url, "origin": origin,
                               "started_at": started})
            self.active.append(done)
        if self.auto:
            done.set()
        if not done.wait(timeout=10):
            raise AssertionError("scripted exchange never released")
        for needle, outcome in self.rules.items():
            if needle in url:
                return outcome(url, origin) if callable(outcome) else outcome
        return ExchangeResult(
            ok=True, url=url, origin=origin, status=200,
            body_bytes=1, body_sha256="x")

    def complete_one(self) -> None:
        with self._lock:
            if self.active:
                self.active.pop(0).set()

    def complete_all(self) -> None:
        with self._lock:
            active, self.active = self.active, []
        for event in active:
            event.set()


def base_config(**over) -> BatchConfig:
    values = dict(global_inflight=2,
                  default_origin=OriginBudget(2, 10.0),
                  request_timeout_seconds=5.0,
                  max_body_bytes=1024, max_redirects=3)
    values.update(over)
    return BatchConfig(**values).validated()


class _Driver:
    """Run a batch in a thread while the test drives the fake clock."""

    def __init__(self, runner: BatchRunner, clock: FakeClock):
        self.runner = runner
        self.clock = clock
        self.thread = threading.Thread(target=runner.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        self.clock.settle()
        return self

    def __exit__(self, *exc):
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise AssertionError("scheduler did not finish on virtual time")

    def settle(self):
        self.clock.settle()

    def advance(self, seconds: float):
        self.clock.advance(seconds)
        self.clock.settle()


class GlobalCapTests(unittest.TestCase):
    def test_global_inflight_cap_holds_across_origins(self):
        clock = FakeClock()
        fake = ScriptedExchange(clock=clock, auto=False)
        jobs = [job("a", "http://a.test/1", 0),
                job("b", "http://b.test/2", 1),
                job("c", "http://c.test/3", 2)]
        runner = BatchRunner(
            jobs, base_config(global_inflight=2), clock=clock,
            exchange_fn=fake)
        with _Driver(runner, clock):
            self.assertEqual(runner.global_inflight, 2)
            self.assertEqual(len(fake.calls), 2)
            # The third job must not start until one exchange finishes.
            fake.complete_one()
            clock.settle()
            self.assertEqual(len(fake.calls), 3)
            fake.complete_all()
        self.assertEqual(runner.summary()["complete"], 3)
        self.assertEqual(
            sorted(c["url"] for c in fake.calls),
            sorted(j.url for j in jobs))


class OriginCapTests(unittest.TestCase):
    def test_origin_cap_independent_of_global_cap(self):
        clock = FakeClock()
        fake = ScriptedExchange(clock=clock, auto=False)
        jobs = [job(f"a{i}", f"http://a.test/{i}", i) for i in range(3)]
        jobs.append(job("b0", "http://b.test/0", 3))
        cfg = BatchConfig(
            global_inflight=4,
            default_origin=OriginBudget(1, 0.0),
            request_timeout_seconds=5.0, max_body_bytes=1024,
            max_redirects=3).validated()
        runner = BatchRunner(jobs, cfg, clock=clock, exchange_fn=fake)
        with _Driver(runner, clock):
            # Origin A serializes (cap 1) but B starts in parallel.
            in_flight_urls = sorted(c["url"] for c in fake.calls)
            self.assertEqual(in_flight_urls,
                             ["http://a.test/0", "http://b.test/0"])
            fake.complete_all()
            clock.settle()
            fake.complete_all()
            clock.settle()
            fake.complete_all()
        self.assertEqual(len(fake.calls), 4)
        a_order = [c["url"] for c in fake.calls if "a.test" in c["url"]]
        self.assertEqual(a_order,
                         ["http://a.test/0", "http://a.test/1",
                          "http://a.test/2"])

    def test_per_origin_override_budget(self):
        clock = FakeClock()
        fake = ScriptedExchange(clock=clock, auto=False)
        jobs = [job(f"a{i}", f"http://a.test/{i}", i) for i in range(2)]
        cfg = BatchConfig(
            global_inflight=4,
            default_origin=OriginBudget(4, 0.0),
            origin_overrides={"http://a.test": OriginBudget(1, 0.0)},
            request_timeout_seconds=5.0, max_body_bytes=1024,
            max_redirects=3).validated()
        runner = BatchRunner(jobs, cfg, clock=clock, exchange_fn=fake)
        with _Driver(runner, clock):
            self.assertEqual(len(fake.calls), 1)
            fake.complete_one()
            clock.settle()
            self.assertEqual(len(fake.calls), 2)
            fake.complete_all()


class IntervalTests(unittest.TestCase):
    def test_interval_measured_from_actual_start(self):
        clock = FakeClock()
        fake = ScriptedExchange(clock=clock)
        jobs = [job(f"a{i}", f"http://a.test/{i}", i) for i in range(3)]
        cfg = BatchConfig(
            global_inflight=4,
            default_origin=OriginBudget(4, 10.0),
            request_timeout_seconds=5.0, max_body_bytes=1024,
            max_redirects=3).validated()
        runner = BatchRunner(jobs, cfg, clock=clock, exchange_fn=fake)
        with _Driver(runner, clock):
            self.assertEqual(len(fake.calls), 1)
            first_start = fake.calls[0]["started_at"]
            # Advancing less than the interval must not release the next.
            driver_advance(clock, 5.0)
            self.assertEqual(len(fake.calls), 1)
            driver_advance(clock, 5.5)  # now >= last_start + 10
            self.assertEqual(len(fake.calls), 2)
            self.assertGreaterEqual(
                fake.calls[1]["started_at"] - first_start, 10.0)
            driver_advance(clock, 10.5)
        self.assertEqual(len(fake.calls), 3)
        gaps = [fake.calls[i + 1]["started_at"]
                - fake.calls[i]["started_at"] for i in range(2)]
        self.assertTrue(all(g >= 10.0 for g in gaps), gaps)

    def test_head_blocked_origin_lets_other_origins_run(self):
        clock = FakeClock()
        fake = ScriptedExchange(clock=clock)
        jobs = [job("a0", "http://a.test/0", 0),
                job("a1", "http://a.test/1", 1),
                job("b0", "http://b.test/0", 2)]
        cfg = BatchConfig(
            global_inflight=4,
            default_origin=OriginBudget(4, 10.0),
            request_timeout_seconds=5.0, max_body_bytes=1024,
            max_redirects=3).validated()
        runner = BatchRunner(jobs, cfg, clock=clock, exchange_fn=fake)
        with _Driver(runner, clock):
            # a0 started; a1 must wait for the interval, but b0 runs now.
            running = sorted(c["url"] for c in fake.calls)
            self.assertEqual(running,
                             ["http://a.test/0", "http://b.test/0"])
            wait_events = [e for e in runner.timeline if e.kind == "wait"]
            self.assertTrue(wait_events)
            blocked = wait_events[0].detail["blocked"]
            self.assertEqual(
                {(b["origin"], b["reason"]) for b in blocked},
                {("http://a.test", "origin_interval")})
            driver_advance(clock, 20.0)
        self.assertEqual(len(fake.calls), 3)


class RoundRobinTests(unittest.TestCase):
    def test_origins_take_turns(self):
        clock = FakeClock()
        fake = ScriptedExchange(clock=clock)
        jobs = [job(f"a{i}", f"http://a.test/{i}", i) for i in range(2)]
        jobs += [job(f"b{i}", f"http://b.test/{i}", i + 2)
                 for i in range(2)]
        cfg = BatchConfig(
            global_inflight=1,
            default_origin=OriginBudget(1, 0.0),
            request_timeout_seconds=5.0, max_body_bytes=1024,
            max_redirects=3).validated()
        runner = BatchRunner(jobs, cfg, clock=clock, exchange_fn=fake)
        with _Driver(runner, clock):
            driver_advance(clock, 1.0)
            driver_advance(clock, 1.0)
            driver_advance(clock, 1.0)
        order = [c["origin"] for c in fake.calls]
        # Global cap 1: the two origins must alternate, not drain A first.
        self.assertEqual(order, ["http://a.test", "http://b.test",
                                 "http://a.test", "http://b.test"])


class NoEarlySlotTests(unittest.TestCase):
    def test_workers_do_not_hold_global_slot_while_pacing(self):
        clock = FakeClock()
        fake = ScriptedExchange(clock=clock)
        jobs = [job(f"a{i}", f"http://a.test/{i}", i) for i in range(3)]
        cfg = BatchConfig(
            global_inflight=3,
            default_origin=OriginBudget(3, 10.0),
            request_timeout_seconds=5.0, max_body_bytes=1024,
            max_redirects=3).validated()
        runner = BatchRunner(jobs, cfg, clock=clock, exchange_fn=fake)
        with _Driver(runner, clock):
            # While a1/a2 wait on A's interval, nothing is in flight and
            # the global occupancy recorded at the start is exactly 1.
            start_events = [e for e in runner.timeline
                            if e.kind == "exchange_start"]
            self.assertEqual(len(start_events), 1)
            self.assertEqual(
                start_events[0].detail["occupancy"]["global_inflight"], 1)
            self.assertEqual(runner.global_inflight, 0)
            # Two further 10s gaps must both be walked out on virtual time.
            driver_advance(clock, 12.0)
            driver_advance(clock, 12.0)
        self.assertEqual(len(fake.calls), 3)


def driver_advance(clock: FakeClock, seconds: float) -> None:
    clock.advance(seconds)
    clock.settle()


if __name__ == "__main__":
    unittest.main()
