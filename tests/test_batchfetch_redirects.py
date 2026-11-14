"""Redirect routing and cancellation tests with the fake clock."""

from __future__ import annotations

import threading
import unittest

from batchfetch import CANCELLED, COMPLETE, FAILED, BatchConfig, BatchRunner
from batchfetch import FakeClock, OriginBudget
from batchfetch.exchange import ExchangeResult

from tests.test_batchfetch_scheduler import ScriptedExchange, _Driver, job


def redirect(url: str, origin: str, *, status: int = 302,
             location: str) -> ExchangeResult:
    return ExchangeResult(ok=True, url=url, origin=origin, status=status,
                          location=location, body_bytes=0,
                          body_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")


def ok(url: str, origin: str, *, status: int = 200) -> ExchangeResult:
    return ExchangeResult(ok=True, url=url, origin=origin, status=status,
                          body_bytes=3, body_sha256="abc")


def failed(url: str, origin: str, *, kind: str = "connection_error",
           reason: str = "boom") -> ExchangeResult:
    return ExchangeResult(ok=False, url=url, origin=origin,
                          failure_kind=kind, failure_reason=reason)


def cfg(**over) -> BatchConfig:
    values = dict(global_inflight=3,
                  default_origin=OriginBudget(3, 0.0),
                  request_timeout_seconds=5.0,
                  max_body_bytes=1024, max_redirects=3)
    values.update(over)
    return BatchConfig(**values).validated()


class RedirectTests(unittest.TestCase):
    def test_relative_location_resolved_and_chain_recorded(self):
        clock = FakeClock()
        fake = ScriptedExchange(clock=clock, rules={
            "/start": lambda u, o: redirect(u, o, location="/next?x=1"),
            "/next": ok,
        })
        runner = BatchRunner([job("j", "http://a.test/start")],
                             cfg(), clock=clock, exchange_fn=fake)
        with _Driver(runner, clock):
            pass
        result = runner.iter_results()[0]
        self.assertEqual(result.outcome, COMPLETE)
        self.assertEqual(result.final_url, "http://a.test/next?x=1")
        self.assertEqual(result.redirect_chain,
                         ["http://a.test/start", "http://a.test/next?x=1"])
        self.assertEqual([h.status for h in result.chain], [302, 200])
        self.assertEqual(result.chain[0].location, "/next?x=1")

    def test_cross_origin_redirect_requeues_at_target_origin(self):
        clock = FakeClock()
        fake = ScriptedExchange(clock=clock, auto=False, rules={
            "/start": lambda u, o: redirect(
                u, o, location="http://b.test/landing"),
            "/landing": ok,
        })
        runner = BatchRunner([job("j", "http://a.test/start")],
                             cfg(global_inflight=3),
                             clock=clock, exchange_fn=fake)
        with _Driver(runner, clock):
            # Only the A-origin request is in flight.
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(fake.calls[0]["origin"], "http://a.test")
            fake.complete_one()  # release the redirect response
            clock.settle()
            # The hop is now queued at B and is the only thing running;
            # A holds no slot anymore.
            self.assertEqual(len(fake.calls), 2)
            self.assertEqual(fake.calls[1]["origin"], "http://b.test")
            self.assertEqual(runner._origins["http://a.test"].inflight, 0)
            fake.complete_all()
        result = runner.iter_results()[0]
        self.assertEqual(result.outcome, COMPLETE)
        self.assertEqual(result.final_url, "http://b.test/landing")

    def test_redirect_respects_target_origin_fifo(self):
        clock = FakeClock()
        # j1 is a queued job at origin B, received before j0's redirect
        # can possibly arrive there.
        jobs = [job("j0", "http://a.test/start", 0),
                job("j1", "http://b.test/waiting", 1)]
        fake = ScriptedExchange(clock=clock, auto=False, rules={
            "/start": lambda u, o: redirect(
                u, o, location="http://b.test/landing"),
        })
        # Origin B serializes everything so the FIFO is observable.
        config = cfg(default_origin=OriginBudget(1, 0.0),
                     global_inflight=4)
        runner = BatchRunner(jobs, config, clock=clock, exchange_fn=fake)
        with _Driver(runner, clock):
            running = sorted(c["url"] for c in fake.calls)
            self.assertEqual(running,
                             ["http://a.test/start",
                              "http://b.test/waiting"])
            # Let A answer the redirect first.  Its hop lands behind j1 in
            # B's FIFO; B's own exchange is still in flight.
            fake.complete_one()
            clock.settle()
            self.assertEqual(
                [c["url"] for c in fake.calls
                 if c["origin"] == "http://b.test"],
                ["http://b.test/waiting"])
            self.assertEqual(
                runner._origins["http://b.test"].queue[0].url,
                "http://b.test/landing")
            # Now release j1.  The redirected hop is the next B start.
            fake.complete_one()
            clock.settle()
            b_order = [c["url"] for c in fake.calls
                       if c["origin"] == "http://b.test"]
            self.assertEqual(b_order,
                             ["http://b.test/waiting",
                              "http://b.test/landing"])
            fake.complete_all()
        results = {r.job_id: r for r in runner.iter_results()}
        self.assertEqual(results["j0"].outcome, COMPLETE)
        self.assertEqual(results["j1"].outcome, COMPLETE)

    def test_redirect_loop_back_to_requested_url_fails(self):
        clock = FakeClock()
        fake = ScriptedExchange(clock=clock, rules={
            "/start": lambda u, o: redirect(
                u, o, location="http://b.test/mid"),
            "/mid": lambda u, o: redirect(
                u, o, location="http://a.test/start"),
        })
        runner = BatchRunner([job("j", "http://a.test/start")],
                             cfg(), clock=clock, exchange_fn=fake)
        with _Driver(runner, clock):
            pass
        result = runner.iter_results()[0]
        self.assertEqual(result.outcome, FAILED)
        self.assertEqual(result.failure_kind, "redirect_loop")
        self.assertEqual(len(result.chain), 2)

    def test_max_redirects_enforced(self):
        clock = FakeClock()
        fake = ScriptedExchange(clock=clock, rules={
            "/r": lambda u, o: redirect(u, o, location=u + "x"),
        })
        runner = BatchRunner(
            [job("j", "http://a.test/r")], cfg(max_redirects=2),
            clock=clock, exchange_fn=fake)
        with _Driver(runner, clock):
            pass
        result = runner.iter_results()[0]
        self.assertEqual(result.outcome, FAILED)
        self.assertEqual(result.failure_kind, "redirect_limit")
        # hop0 + 2 redirect hops = 3 requests; the 4th never starts.
        self.assertEqual(len(result.chain), 3)

    def test_missing_or_illegal_location_fails(self):
        clock = FakeClock()
        missing = ExchangeResult(
            ok=True, url="http://a.test/x", origin="http://a.test",
            status=302, location=None, body_bytes=0, body_sha256="z")
        fake = ScriptedExchange(clock=clock, rules={"/x": missing})
        runner = BatchRunner([job("j", "http://a.test/x")],
                             cfg(), clock=clock, exchange_fn=fake)
        with _Driver(runner, clock):
            pass
        self.assertEqual(runner.iter_results()[0].failure_kind,
                         "redirect_error")

        clock2 = FakeClock()
        illegal = ScriptedExchange(clock=clock2, rules={
            "/y": lambda u, o: redirect(u, o, location="ftp://b.test/z")})
        runner2 = BatchRunner([job("j", "http://a.test/y")],
                              cfg(), clock=clock2, exchange_fn=illegal)
        with _Driver(runner2, clock2):
            pass
        self.assertEqual(runner2.iter_results()[0].failure_kind,
                         "redirect_error")

    def test_non_redirect_statuses_are_final(self):
        clock = FakeClock()
        fake = ScriptedExchange(clock=clock, rules={
            "/noredir": lambda u, o: ok(u, o, status=204),
        })
        runner = BatchRunner([job("j", "http://a.test/noredir")],
                             cfg(), clock=clock, exchange_fn=fake)
        with _Driver(runner, clock):
            pass
        result = runner.iter_results()[0]
        self.assertEqual(result.outcome, COMPLETE)
        self.assertEqual(result.status, 204)
        self.assertEqual(len(result.chain), 1)


class TransportFailureTests(unittest.TestCase):
    def test_connection_failure_marks_job_failed_others_continue(self):
        clock = FakeClock()
        fake = ScriptedExchange(clock=clock, rules={
            "/bad": failed,
            "/good": ok,
        })
        jobs = [job("bad", "http://a.test/bad", 0),
                job("good", "http://a.test/good", 1)]
        runner = BatchRunner(jobs, cfg(), clock=clock, exchange_fn=fake)
        with _Driver(runner, clock):
            pass
        results = {r.job_id: r for r in runner.iter_results()}
        self.assertEqual(results["bad"].outcome, FAILED)
        self.assertEqual(results["bad"].failure_kind, "connection_error")
        self.assertEqual(results["good"].outcome, COMPLETE)


class CancellationTests(unittest.TestCase):
    def test_cancel_before_start_marks_queued_jobs_cancelled(self):
        clock = FakeClock()
        fake = ScriptedExchange(clock=clock, auto=False)
        # Global cap 1: the second job is guaranteed still queued.
        jobs = [job("j0", "http://a.test/0", 0),
                job("j1", "http://a.test/1", 1),
                job("j2", "http://b.test/2", 2)]
        runner = BatchRunner(jobs, cfg(global_inflight=1),
                             clock=clock, exchange_fn=fake)
        with _Driver(runner, clock):
            self.assertEqual(len(fake.calls), 1)
            runner.cancel()
            clock.settle()
            fake.complete_all()
        results = {r.job_id: r for r in runner.iter_results()}
        self.assertEqual(results["j0"].outcome, COMPLETE)
        self.assertEqual(results["j1"].outcome, CANCELLED)
        self.assertEqual(results["j2"].outcome, CANCELLED)
        # Cancellation is never disguised as a timeout or a 404.
        for jid in ("j1", "j2"):
            self.assertIsNone(results[jid].status)
            self.assertEqual(results[jid].failure_kind, "cancelled")
        # Only the one in-flight exchange ever reached the exchange fn.
        self.assertEqual(len(fake.calls), 1)
        summary = runner.summary()
        self.assertEqual(summary["cancelled"], 2)
        self.assertFalse(summary["success"])
        self.assertTrue(any(e.kind == "cancelled" for e in runner.timeline))

    def test_cancel_blocks_new_redirect_but_keeps_completed_results(self):
        clock = FakeClock()
        fake = ScriptedExchange(clock=clock, auto=False, rules={
            "/start": lambda u, o: redirect(
                u, o, location="http://b.test/landing"),
        })
        runner = BatchRunner([job("j0", "http://a.test/start", 0),
                              job("j1", "http://b.test/other", 1)],
                             cfg(global_inflight=2),
                             clock=clock, exchange_fn=fake)
        with _Driver(runner, clock):
            self.assertEqual(len(fake.calls), 2)
            runner.cancel()
            clock.settle()
            fake.complete_all()  # both in-flight exchanges end
        results = {r.job_id: r for r in runner.iter_results()}
        # j1 got a real response; j0's redirect must not be followed.
        self.assertEqual(results["j1"].outcome, COMPLETE)
        self.assertEqual(results["j0"].outcome, CANCELLED)
        self.assertEqual(len(results["j0"].chain), 1)
        self.assertEqual(results["j0"].chain[0].status, 302)
        self.assertEqual(len(fake.calls), 2)  # no third request started

    def test_cancel_races_a_dispatched_item_without_double_release(self):
        clock = FakeClock()
        gate = threading.Event()
        release = threading.Event()

        def racing_exchange(url, origin, *, timeout, max_body_bytes):
            gate.set()
            release.wait(timeout=5)
            return ExchangeResult(
                ok=True, url=url, origin=origin, status=200,
                body_bytes=1, body_sha256="x")

        # global cap 1: the second job can only be dispatched once the first
        # exchange finishes; cancel lands while it may already be handed to a
        # worker, exercising the worker-side cancel branch.
        jobs = [job("j0", "http://a.test/0", 0),
                job("j1", "http://a.test/1", 1)]
        runner = BatchRunner(
            jobs, cfg(global_inflight=1,
                      default_origin=OriginBudget(1, 0.0)),
            clock=clock, exchange_fn=racing_exchange)
        thread = threading.Thread(target=runner.run, daemon=True)
        thread.start()
        self.assertTrue(gate.wait(timeout=5))
        runner.cancel()
        release.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        results = {r.job_id: r for r in runner.iter_results()}
        self.assertEqual(results["j0"].outcome, COMPLETE)
        self.assertEqual(results["j1"].outcome, CANCELLED)
        # Slots were released exactly once each: counters back at zero and
        # no exception leaked through the worker.
        self.assertEqual(runner.global_inflight, 0)
        for state in runner._origins.values():
            self.assertEqual(state.inflight, 0)
            self.assertEqual(len(state.queue), 0)

    def test_no_leftover_threads_after_batch(self):
        clock = FakeClock()
        fake = ScriptedExchange(clock=clock)
        before = threading.active_count()
        for batch in range(2):
            runner = BatchRunner(
                [job(f"j{batch}", "http://a.test/ok")],
                cfg(global_inflight=2),
                clock=clock, exchange_fn=fake)
            runner.run()
        self.assertLessEqual(threading.active_count(), before)


if __name__ == "__main__":
    unittest.main()
