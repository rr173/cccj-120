#!/usr/bin/env python3
"""Repeatable handover drill for batchfetch (real local HTTP, no network).

Run from the repository root:

    python3 -I demo/run_batchfetch_drill.py

The script starts the controllable two-port localhost site
(:mod:`batchfetch.testsite`, two ports == two origins with separate
budgets) and walks the whole handover story with **real** HTTP requests:

  1. Gate + per-origin cap: A fills its own origin cap while B's request
     is still accepted by the server; A's two actual request starts are
     at least the configured interval apart (checked on real monotonic
     timestamps, not estimates).
  2. Cross-origin redirect behind an existing B queue: the redirect
     neither exceeds a budget nor jumps B's first-come job.
  3. A<->B redirect ring, empty body and 500: each gets its documented
     terminal outcome and the slots it freed serve later jobs.
  4. Declared-length early close and oversized streaming body: both are
     incomplete/failed responses, and following jobs still fetch.
  5. Cancellation with one exchange held behind the gate: not-started
     addresses never reach the server; after the gate is opened every
     connection and worker winds down; a fresh batch in the same process
     then fetches successfully (no leaked slots or background requests).
  6. CLI round trip including the nonzero exit code of a batch that
     contains failures.

The expected job order and outcomes are hard-coded below independently of
the executor; the server's own receipt log is the evidence for what
actually reached the wire, never the executor's timeline alone.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from batchfetch import (  # noqa: E402
    CANCELLED,
    COMPLETE,
    FAILED,
    BatchConfig,
    BatchRunner,
    OriginBudget,
)
from batchfetch.config import parse_manifest  # noqa: E402
from batchfetch.testsite import DualPortSite  # noqa: E402

PY = sys.executable
CLI = [PY, "-I", "-c",
       "import sys; sys.path.insert(0, %r); "
       "from batchfetch.__main__ import main; "
       "sys.exit(main(sys.argv[1:]))" % str(REPO)]

failures: list[str] = []
checks = 0


def expect(label: str, condition: bool, detail: str = "") -> None:
    global checks
    checks += 1
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f"  实际={detail}" if not condition else ""))
    if not condition:
        failures.append(label)


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def make_jobs(*pairs) -> list:
    text = "\n".join(json.dumps({"job_id": jid, "url": url})
                     for jid, url in pairs)
    return parse_manifest(io.StringIO(text))


def worker_receipts(site: DualPortSite) -> list[dict]:
    return [r for r in site.receipts()
            if not r["path"].startswith("/__control")]


def wait_for_path(site: DualPortSite, prefix: str, *,
                  timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(r["path"].startswith(prefix) for r in worker_receipts(site)):
            return
        time.sleep(0.02)
    raise AssertionError(f"server never received a request for {prefix!r}")


def control(site: DualPortSite, which: str, action: str, **params) -> dict:
    url = site.control_url(which, action, **params)
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="batchfetch-drill-"))
    print(f"scratch workspace: {workspace}")

    with DualPortSite() as site:
        a, b = site.origin_a, site.origin_b
        print(f"origin A = {a}")
        print(f"origin B = {b}")

        # ------------------------------------------------------------
        section("1. 闸门占满 A 的名额，B 仍被接收；A 的两次开始满足间隔")
        # ------------------------------------------------------------
        site.site.reset()
        jobs = make_jobs(
            ("a-gated", a + "/gate?name=cap"),
            ("a-second", a + "/ok/second"),
            ("b-first", b + "/ok"),
        )
        # A serializes (cap 1) with a real 0.3s start interval; global cap 2.
        runner = BatchRunner(jobs, BatchConfig(
            global_inflight=2,
            default_origin=OriginBudget(1, 0.3),
            request_timeout_seconds=10.0,
            max_body_bytes=4096, max_redirects=3).validated())
        thread = threading.Thread(target=runner.run, daemon=True)
        thread.start()
        wait_for_path(site, "/gate")
        time.sleep(0.15)  # let b-first reach the server too
        received = {(r["port"], r["path"].split("?")[0])
                    for r in worker_receipts(site)}
        expect("A 的闸门请求已到达服务端",
               (site.port_a, "/gate") in received)
        expect("A 名额被占用期间 B 的请求仍被服务端接收",
               (site.port_b, "/ok") in received)
        expect("A 的第二个请求尚未开始（仍在等间隔/名额）",
               not any("/ok/second" in r["path"]
                       for r in worker_receipts(site)))
        # Release A's gate; a-second may only start one interval after the
        # *actual* first A start.
        first_a_start = min(e.mono for e in runner.timeline
                            if e.kind == "exchange_start"
                            and e.origin == a)
        control(site, "a", "open", name="cap")
        thread.join(timeout=15)
        expect("第 1 批次正常结束", not thread.is_alive())
        a_starts = sorted(e.mono for e in runner.timeline
                          if e.kind == "exchange_start" and e.origin == a)
        expect("A 两次实际开始间隔 >= 配置的 0.3s",
               len(a_starts) == 2 and a_starts[1] - a_starts[0] >= 0.28,
               detail=f"gap={a_starts[1] - a_starts[0]:.3f}")
        expect("时间线记录了 origin_interval 等待理由",
               any(e.kind == "wait"
                   and any(x["reason"] == "origin_interval"
                           for x in e.detail["blocked"])
                   for e in runner.timeline))
        summary = runner.summary()
        expect("三个作业全部完成", summary["complete"] == 3,
               detail=json.dumps(summary))

        # ------------------------------------------------------------
        section("2. A 返回到 B 的跳转，不能超过预算也不能插队")
        # ------------------------------------------------------------
        site.site.reset()
        jobs = make_jobs(
            ("redirect-job", a + f"/redirect?to={b}/landing&status=302"),
            ("b-queued", b + "/queued"),
        )
        runner = BatchRunner(jobs, BatchConfig(
            global_inflight=4,
            default_origin=OriginBudget(1, 0.0),
            request_timeout_seconds=5.0,
            max_body_bytes=4096, max_redirects=3).validated())
        runner.run()
        order = [r["path"] for r in worker_receipts(site)
                 if r["port"] == site.port_b]
        expect("B 站先到作业 /queued 先于跳转落点 /landing",
               order.index("/queued") < order.index("/landing"),
               detail=str(order))
        results = {r.job_id: r for r in runner.iter_results()}
        expect("跳转作业保留完整两跳链",
               results["redirect-job"].redirect_chain ==
               [a + f"/redirect?to={b}/landing&status=302",
                b + "/landing"])
        expect("B 站在途从未超过每站名额 1",
               all(e.detail["occupancy"]["origin_inflight"] <= 1
                   for e in runner.timeline
                   if e.kind == "exchange_start"))

        # ------------------------------------------------------------
        section("3. 相互跳转环、空体与 500 的逐项结果")
        # ------------------------------------------------------------
        site.site.reset()
        jobs = make_jobs(
            ("ring", a + "/loop-a"),
            ("empty", b + "/empty"),
            ("fail500", a + "/fail500"),
            ("after-ring", b + "/ok/after"),
        )
        runner = BatchRunner(jobs, BatchConfig(
            global_inflight=4,
            default_origin=OriginBudget(2, 0.02),
            request_timeout_seconds=5.0,
            max_body_bytes=4096, max_redirects=4).validated())
        runner.run()
        results = {r.job_id: r for r in runner.iter_results()}
        expected = {
            "ring": (FAILED, "redirect_loop"),
            "empty": (COMPLETE, None),
            "fail500": (COMPLETE, None),
            "after-ring": (COMPLETE, None),
        }
        for jid, (outcome, kind) in expected.items():
            expect(f"{jid} -> {outcome}"
                   + (f"/{kind}" if kind else ""),
                   results[jid].outcome == outcome
                   and results[jid].failure_kind == kind,
                   detail=json.dumps(results[jid].to_dict()))
        expect("500 作为取得的 HTTP 响应保留状态码",
               results["fail500"].status == 500)
        expect("空体有确定的 SHA256",
               results["empty"].chain[0].body_sha256 ==
               hashlib.sha256(b"").hexdigest())
        expect("环失败释放名额后后续作业仍完成",
               results["after-ring"].outcome == COMPLETE)

        # ------------------------------------------------------------
        section("4. 声明长度中途断开 与 超过上限的正文")
        # ------------------------------------------------------------
        site.site.reset()
        jobs = make_jobs(
            ("cut", a + "/truncated?len=128"),
            ("big", b + "/oversize?len=200000"),
            ("survivor", a + "/ok/survivor"),
        )
        runner = BatchRunner(jobs, BatchConfig(
            global_inflight=4,
            default_origin=OriginBudget(2, 0.0),
            request_timeout_seconds=5.0,
            max_body_bytes=8192, max_redirects=3).validated())
        runner.run()
        results = {r.job_id: r for r in runner.iter_results()}
        expect("声明 Content-Length 提前断流 -> truncated_body",
               results["cut"].outcome == FAILED
               and results["cut"].failure_kind == "truncated_body")
        expect("正文超过上限 -> body_too_large 且记录已读字节",
               results["big"].outcome == FAILED
               and results["big"].failure_kind == "body_too_large"
               and results["big"].chain[0].body_bytes >= 8192)
        expect("失败连接关闭后名额释放，后续作业成功",
               results["survivor"].outcome == COMPLETE)
        # The oversize request really was cut on the wire: server receipts
        # show it, but the client never retained the full body.
        expect("服务端确有 oversize 请求记录",
               any("/oversize" in r["path"] for r in worker_receipts(site)))

        # ------------------------------------------------------------
        section("5. 闸门挡住在途请求时取消，再开新批次")
        # ------------------------------------------------------------
        site.site.reset()
        jobs = make_jobs(
            ("gated", a + "/gate?name=cancel"),
            ("never-started", b + "/ok/never"),
        )
        runner = BatchRunner(jobs, BatchConfig(
            global_inflight=1,
            default_origin=OriginBudget(1, 0.0),
            request_timeout_seconds=10.0,
            max_body_bytes=4096, max_redirects=3).validated())
        thread = threading.Thread(target=runner.run, daemon=True)
        thread.start()
        wait_for_path(site, "/gate")
        runner.cancel()
        time.sleep(0.3)
        before_open = len(worker_receipts(site))
        control(site, "a", "open", name="cancel")
        thread.join(timeout=15)
        expect("取消后没有遗留派发线程", not thread.is_alive())
        results = {r.job_id: r for r in runner.iter_results()}
        expect("在途闸门作业允许在超时内结束并保留真实结果",
               results["gated"].outcome == COMPLETE
               and results["gated"].status == 200)
        expect("未开始作业明确标记 cancelled（不是超时/404）",
               results["never-started"].outcome == CANCELLED
               and results["never-started"].status is None)
        expect("未开始地址从未抵达服务端",
               len(worker_receipts(site)) == before_open
               and not any("/ok/never" in r["path"]
                           for r in worker_receipts(site)),
               detail=str([r["path"] for r in worker_receipts(site)]))

        # Fresh batch in the SAME process.
        site.site.reset()
        fresh_jobs = make_jobs(("fresh", b + "/ok/fresh"))
        runner2 = BatchRunner(fresh_jobs, BatchConfig(
            global_inflight=2,
            default_origin=OriginBudget(2, 0.0),
            request_timeout_seconds=5.0,
            max_body_bytes=4096, max_redirects=3).validated())
        runner2.run()
        fresh = runner2.iter_results()[0]
        expect("同进程新批次成功抓取（无泄漏名额/后台请求）",
               fresh.outcome == COMPLETE and fresh.status == 200)
        expect("新批次结束后全局在途归零", runner2.global_inflight == 0)
        expect("新批次的请求确实到达服务端",
               any("/ok/fresh" in r["path"]
                   for r in worker_receipts(site)))

        # ------------------------------------------------------------
        section("6. 命令行往返与非零退出码")
        # ------------------------------------------------------------
        manifest = workspace / "jobs.jsonl"
        report = workspace / "report.jsonl"
        with open(manifest, "w", encoding="utf-8") as fh:
            for jid, url in [
                    ("good", a + "/ok"),
                    ("dead", "http://127.0.0.1:1/no-such-port"),
                    ("redirect", a + f"/redirect?to={b}/empty&status=301")]:
                fh.write(json.dumps({"job_id": jid, "url": url}) + "\n")
        proc = subprocess.run(
            CLI + ["run", "--manifest", str(manifest),
                   "--report", str(report),
                   "--global-inflight", "3", "--origin-inflight", "2",
                   "--request-timeout", "2"],
            capture_output=True, text=True, timeout=30)
        expect("含失败的批次退出码为 3", proc.returncode == 3,
               detail=f"rc={proc.returncode}")
        lines = [json.loads(line) for line in
                 report.read_text(encoding="utf-8").splitlines()]
        result_lines = [line for line in lines if line["type"] == "result"]
        expect("每个 job_id 恰有一条终结果且按输入顺序",
               [r["job_id"] for r in result_lines]
               == ["good", "dead", "redirect"])
        by_id = {r["job_id"]: r for r in result_lines}
        expect("dead 为 failed，good/redirect 为 complete",
               by_id["dead"]["outcome"] == FAILED
               and by_id["good"]["outcome"] == COMPLETE
               and by_id["redirect"]["outcome"] == COMPLETE)
        expect("报告含独立时间线",
               any(line["type"] == "timeline" for line in lines))

    print("\n" + "=" * 72)
    print(f"检查项 {checks}，失败 {len(failures)}")
    if failures:
        print("失败项：")
        for label in failures:
            print(" -", label)
        return 1
    print("全部通过；预期顺序/状态见本脚本硬编码清单，服务端收据已逐项核对。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
