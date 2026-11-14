"""Repeatable, no-network acceptance drill for robots_admission.

Run from the repository root:

    python3 -I demo/run_robots_drill.py

The script is fully self-contained (stdlib only): every robots fetch
result is fed into the cache from local bytes -- no network access, no
machine clock, no sleep.  Each judgment is printed with its real reason
and the matched rule line, then compared against an independent,
hard-coded expectation.  Re-running starts from a fresh cache every
time; the scratch directory is retained for inspection.

Scenarios (mirrors the task contract):
  1. User-agent group selection: two specific agents vs '*', case and
     substring token matching, '*' never mixed into a specific agent;
  2. same-length group merge and Allow winning a length tie;
  3. '$' and '*' rules against targets with query strings;
  4. percent/UTF-8 normalization: %7E == ~, %2F != /, %3F != ?, Chinese
     path == its UTF-8 percent-encoding, line diagnostics for bad rules;
  5. origin isolation: default port + host case share a policy, other
     scheme/ports do not, IPv6 literals, rejected URLs;
  6. 200 then a correct 304 refresh: window extended, rules unchanged;
     wrong-ETag 304, stale and equal-time updates rejected;
  7. expiry boundary, 429/5xx refresh failures (recorded, no extension,
     no deletion) and recovery by the next later valid result;
  8. 304 without a prior 200, invalid UTF-8, oversize body, naive
     instants and bad parameters: the old valid policy survives all;
     404/410 and empty-200 representations; never-fetched origins;
  9. the batch entry point checking one URL list as two crawler
     identities, and a fresh-process database reopen re-verifying one
     judgment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from robots_admission import (  # noqa: E402
    ALLOW,
    DISALLOW,
    NEEDS_REFRESH,
    AdmissionError,
    RobotsCache,
    render_decision,
)

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


failures: list[str] = []
checks = 0


def expect(label: str, condition: bool, detail: str = "") -> None:
    global checks
    checks += 1
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] 预期: {label}" + (f"  -- 实际: {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def section(title: str) -> None:
    print("\n" + "=" * 76)
    print(title)
    print("=" * 76)


def feed(cache: RobotsCache, url: str, t: datetime, status: int, ttl: int,
         body=None, etag=None, expect_accept: bool = True, note: str = ""):
    """Feed one fetch result and assert whether it was accepted."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    result = cache.update(url, fetched_at=t, status=status, ttl_seconds=ttl,
                          body=body, etag=etag)
    head = f"UPDATE {url} status={status} fetched_at={t.isoformat()} ttl={ttl}s"
    if etag is not None:
        head += f" etag={etag!r}"
    if note:
        head += f"   # {note}"
    print(head)
    print(f"  -> accepted={result.accepted}  {result.reason}")
    for diag in result.diagnostics:
        print(f"     diag: {diag}")
    expect(f"update accepted == {expect_accept}（{note or url}）",
           result.accepted is expect_accept, result.reason)
    return result


def judge(cache: RobotsCache, url: str, token: str, t: datetime,
          expected: str, note: str = "", line: int | None = None,
          selection: str | None = None):
    """Print one admission decision and assert the expected outcome."""
    decision = cache.check(url, token, t)
    if note:
        print(f"# {note}")
    for text_line in render_decision(decision).splitlines():
        print("  " + text_line)
    expect(f"outcome == {expected}（{note or url}）",
           decision.outcome == expected, decision.outcome)
    if line is not None:
        got = decision.rule.line if decision.rule is not None else None
        expect(f"命中行号 == {line}（{note or url}）", got == line, str(got))
    if selection is not None:
        expect(f"选择方式 == {selection}（{note or url}）",
               decision.selection == selection, decision.selection)
    print()
    return decision


def expect_error(label: str, fn, exc_type=AdmissionError):
    global checks
    try:
        fn()
    except exc_type as exc:
        checks += 1
        print(f"  [PASS] 预期: 抛出 {exc_type.__name__}（{label}）-- {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        checks += 1
        print(f"  [FAIL] 预期: 抛出 {exc_type.__name__}（{label}）-- 实际抛出 "
              f"{type(exc).__name__}: {exc}")
        failures.append(label)
        return
    checks += 1
    print(f"  [FAIL] 预期: 抛出 {exc_type.__name__}（{label}）-- 实际未抛出")
    failures.append(label)


# ----------------------------------------------------------------------
# robots fixtures (line numbers matter; keep in sync with expectations)
# ----------------------------------------------------------------------

AGENTS_ROBOTS = "\n".join([
    "# robots for agents.example",          # 1
    "User-agent: AlphaBot",                 # 2
    "Disallow: /private",                   # 3
    "Allow: /private/public",               # 4
    "",                                     # 5
    "User-agent: BetaBot",                  # 6
    "Disallow: /beta",                      # 7
    "",                                     # 8
    "User-agent: *",                        # 9
    "Disallow: /all",                       # 10
    "Allow:",                               # 11  empty -> diagnostic
    "Crawl-delay: 5",                       # 12  unknown field -> ignored
])

MERGE_ROBOTS = "\n".join([
    "User-agent: AbBot",                    # 1
    "Disallow: /merge",                     # 2
    "",                                     # 3
    "User-agent: CdBot",                    # 4
    "Allow: /merge",                        # 5
])

QUERY_ROBOTS = "\n".join([
    "User-agent: QueryBot",                 # 1
    "Disallow: /search?*q=secret",          # 2
    "Disallow: /exact$",                    # 3
    "Allow: /exact",                        # 4
    "Disallow: /tmp/$",                     # 5
])

ENC_ROBOTS = "\n".join([
    "User-agent: EncBot",                   # 1
    "Disallow: /~old",                      # 2
    "Allow: /~",                            # 3
    "Disallow: /files/%2Fsecret",           # 4
    "Disallow: /中文目录/",                  # 5
    "Disallow: /q%3Fz",                     # 6
    "Disallow: /bad%zzrule",                # 7  malformed % -> diagnostic
    "Disallow: no-slash",                   # 8  no leading '/' -> diagnostic
])

SHARED_ROBOTS = "\n".join([
    "User-agent: *",                        # 1
    "Disallow: /tracked",                   # 2
])

V6_ROBOTS = "\n".join([
    "User-agent: *",                        # 1
    "Disallow: /v6",                        # 2
])

REFRESH_ROBOTS = "\n".join([
    "User-agent: *",                        # 1
    "Disallow: /gone",                      # 2
])

FLAKY_V1 = "User-agent: *\nDisallow: /x\n"   # line 2
FLAKY_V2 = "User-agent: *\nDisallow: /y\n"   # line 2
STEADY_ROBOTS = "User-agent: *\nDisallow: /a\n"
GUARD_ROBOTS = "User-agent: *\nDisallow: /keep\n"


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="robots_drill_"))
    db = work / "cache.db"
    print(f"scratch directory: {work}")
    cache = RobotsCache(str(db))

    # ------------------------------------------------------------------
    section("场景 1：User-agent 组选择 —— 两个具体 agent 与 * 的分流")
    # ------------------------------------------------------------------
    feed(cache, "http://agents.example/robots.txt", at(0), 200, 3600,
         body=AGENTS_ROBOTS, note="含空 Allow（第 11 行诊断）与未知字段（第 12 行静默忽略）")

    judge(cache, "http://agents.example/private/x", "AlphaBot", at(30),
          DISALLOW, "AlphaBot 命中自己的 Disallow", line=3, selection="specific")
    judge(cache, "http://agents.example/private/public/a", "AlphaBot", at(30),
          ALLOW, "更长 Allow 胜过短 Disallow", line=4)
    judge(cache, "http://agents.example/all", "AlphaBot", at(30),
          ALLOW, "* 的限制不混入具体采集器规则")
    judge(cache, "http://agents.example/beta/x", "BetaBot", at(30),
          DISALLOW, "第二个具体 agent 命中自己的组", line=7, selection="specific")
    judge(cache, "http://agents.example/all", "BetaBot", at(30),
          ALLOW, "BetaBot 组无 /all 规则，* 不混入")
    judge(cache, "http://agents.example/all", "GammaBot", at(30),
          DISALLOW, "无具体匹配时退回 * 组", line=10, selection="wildcard")
    judge(cache, "http://agents.example/private/x", "GammaBot", at(30),
          ALLOW, "* 组没有 /private 规则")
    judge(cache, "http://agents.example/private/x", "alphabot", at(30),
          DISALLOW, "token 匹配不区分大小写", line=3)
    judge(cache, "http://agents.example/private/x", "MyAlphaBot2", at(30),
          DISALLOW, "组 token 以子串方式出现在采集器参数中", line=3)

    # ------------------------------------------------------------------
    section("场景 2：同长度多组合并 + 同长度平局 Allow 优先")
    # ------------------------------------------------------------------
    feed(cache, "http://merge.example/robots.txt", at(0), 200, 3600,
         body=MERGE_ROBOTS, note="AbBot 与 CdBot 两组")

    d = judge(cache, "http://merge.example/merge", "AbBotCdBot", at(30),
              ALLOW, "两个长度 5 的 token 同时命中 -> 合并两组，Allow/Disallow 同长度平局",
              line=5, selection="specific")
    expect("平局说明出现在理由中", "平局" in d.reason, d.reason)
    judge(cache, "http://merge.example/merge", "AbBot", at(30),
          DISALLOW, "单独 AbBot 只选中自己的组", line=2)

    # ------------------------------------------------------------------
    section("场景 3：带查询参数的 $ 与 *")
    # ------------------------------------------------------------------
    feed(cache, "http://query.example/robots.txt", at(0), 200, 3600,
         body=QUERY_ROBOTS)

    judge(cache, "http://query.example/search?q=secret&page=2", "QueryBot", at(30),
          DISALLOW, "* 跨越查询参数中间部分", line=2)
    judge(cache, "http://query.example/search?q=open", "QueryBot", at(30),
          ALLOW, "查询不含 secret，无规则命中")
    judge(cache, "http://query.example/exact", "QueryBot", at(30),
          ALLOW, "/exact$ 与 /exact 同长度平局，Allow 优先", line=4)
    judge(cache, "http://query.example/exact?x=1", "QueryBot", at(30),
          ALLOW, "带查询时 $ 锚定规则不再命中，前缀 Allow 生效", line=4)
    judge(cache, "http://query.example/tmp/", "QueryBot", at(30),
          DISALLOW, "$ 锚定精确匹配 /tmp/", line=5)
    judge(cache, "http://query.example/tmp/anything", "QueryBot", at(30),
          ALLOW, "$ 规则不覆盖更长的路径")

    # ------------------------------------------------------------------
    section("场景 4：百分号与 UTF-8 规范化（%7E==~，%2F!=/，中文路径等价）")
    # ------------------------------------------------------------------
    feed(cache, "http://enc.example/robots.txt", at(0), 200, 3600,
         body=ENC_ROBOTS, note="第 7、8 行规则非法，应给出行级诊断")

    judge(cache, "http://enc.example/~old", "EncBot", at(30),
          DISALLOW, "字面 ~ 命中", line=2)
    judge(cache, "http://enc.example/%7Eold", "EncBot", at(30),
          DISALLOW, "%7E 与 ~ 等价", line=2)
    judge(cache, "http://enc.example/~new", "EncBot", at(30),
          ALLOW, "短 Allow 覆盖未禁止的前缀", line=3)
    judge(cache, "http://enc.example/files/%2Fsecret", "EncBot", at(30),
          DISALLOW, "%2F 按三个字节参与匹配", line=4)
    judge(cache, "http://enc.example/files/%2fsecret", "EncBot", at(30),
          DISALLOW, "十六进制大小写统一", line=4)
    judge(cache, "http://enc.example/files//secret", "EncBot", at(30),
          ALLOW, "%2F 不会变成真正的路径分隔符 /")
    judge(cache, "http://enc.example/中文目录/a", "EncBot", at(30),
          DISALLOW, "中文路径按 UTF-8 逐字节编码后匹配", line=5)
    judge(cache, "http://enc.example/%E4%B8%AD%E6%96%87%E7%9B%AE%E5%BD%95/a",
          "EncBot", at(30),
          DISALLOW, "中文路径与其 UTF-8 百分号编码等价", line=5)
    judge(cache, "http://enc.example/q%3Fz", "EncBot", at(30),
          DISALLOW, "%3F 按三个字节匹配", line=6)
    judge(cache, "http://enc.example/q?z", "EncBot", at(30),
          ALLOW, "%3F 不会变成真正的查询分隔符 ?")
    d = cache.check("http://enc.example/~new", "EncBot", at(30))
    expect("诊断包含第 7 行（非法百分号规则）",
           any("第 7 行" in x for x in d.diagnostics), str(d.diagnostics))
    expect("诊断包含第 8 行（未以 / 开头的规则）",
           any("第 8 行" in x for x in d.diagnostics), str(d.diagnostics))
    print()
    expect_error("URL 含残缺百分号转义",
                 lambda: cache.check("http://enc.example/x%zz", "EncBot", at(30)))

    # ------------------------------------------------------------------
    section("场景 5：origin 隔离 —— 默认端口/大小写共享，跨 scheme/端口不共享，IPv6")
    # ------------------------------------------------------------------
    feed(cache, "http://SHARED.example:80/robots.txt", at(0), 200, 3600,
         body=SHARED_ROBOTS, note="大写主机 + 显式默认端口，应归一到 http://shared.example")

    judge(cache, "http://shared.example/tracked", "AnyBot", at(30),
          DISALLOW, "规范化 origin 命中缓存", line=2)
    judge(cache, "http://SHARED.EXAMPLE/tracked", "AnyBot", at(30),
          DISALLOW, "主机名大小写共享同一策略", line=2)
    judge(cache, "http://shared.example:80/tracked", "AnyBot", at(30),
          DISALLOW, "默认端口省略后共享同一策略", line=2)
    judge(cache, "https://shared.example/tracked", "AnyBot", at(30),
          NEEDS_REFRESH, "http 与 https 不共用缓存")
    judge(cache, "http://shared.example:8080/tracked", "AnyBot", at(30),
          NEEDS_REFRESH, "非默认端口不共用缓存")

    feed(cache, "http://[2001:DB8::1]:8080/robots.txt", at(0), 200, 3600,
         body=V6_ROBOTS, note="IPv6 字面地址")
    judge(cache, "http://[2001:db8::1]:8080/v6", "AnyBot", at(30),
          DISALLOW, "IPv6 大小写归一", line=2)
    judge(cache, "http://[2001:0db8:0000:0000:0000:0000:0000:1]:8080/v6",
          "AnyBot", at(30), DISALLOW, "IPv6 全写形式归一到同一地址", line=2)
    judge(cache, "http://[2001:db8::1]/v6", "AnyBot", at(30),
          NEEDS_REFRESH, "同一 IPv6 主机不同端口不共享")

    print("非法 URL 一律拒绝：")
    expect_error("其他 scheme", lambda: cache.check("ftp://shared.example/x", "A", at(30)))
    expect_error("含用户名密码", lambda: cache.check("http://user:pw@shared.example/x", "A", at(30)))
    expect_error("无主机", lambda: cache.check("http:///x", "A", at(30)))
    expect_error("非法端口", lambda: cache.check("http://shared.example:bad/x", "A", at(30)))
    expect_error("端口越界", lambda: cache.check("http://shared.example:99999/x", "A", at(30)))
    print()

    # ------------------------------------------------------------------
    section("场景 6：200 之后的正确 304 —— 有效期刷新且原规则不变")
    # ------------------------------------------------------------------
    feed(cache, "http://refresh.example/robots.txt", at(0), 200, 100,
         body=REFRESH_ROBOTS, etag="v1", note="ttl=100s，ETag=v1")
    judge(cache, "http://refresh.example/gone", "AnyBot", at(50),
          DISALLOW, "200 策略生效中", line=2)
    feed(cache, "http://refresh.example/robots.txt", at(60), 304, 100,
         etag="v1", note="ETag 一致的 304，窗口推到 [+60s, +160s)")
    d = judge(cache, "http://refresh.example/gone", "AnyBot", at(150),
              DISALLOW, "旧窗口 +100s 已过仍有效：304 延长了有效期且规则不变", line=2)
    expect("缓存表示状态为 304", d.cache is not None and d.cache.status == 304,
           str(d.cache))
    expect("valid_until == T0+160s",
           d.cache is not None and d.cache.valid_until == at(160).isoformat(),
           d.cache.valid_until if d.cache else "None")

    feed(cache, "http://refresh.example/robots.txt", at(70), 304, 100,
         etag="WRONG", expect_accept=False, note="ETag 不符的 304")
    feed(cache, "http://refresh.example/robots.txt", at(30), 200, 100,
         body="User-agent: *\nAllow: /\n", expect_accept=False,
         note="迟到的 200（fetched_at 早于上次已接受）")
    feed(cache, "http://refresh.example/robots.txt", at(60), 200, 100,
         body="User-agent: *\nAllow: /\n", expect_accept=False,
         note="fetched_at 与上次相等（非严格晚于）")
    d = judge(cache, "http://refresh.example/gone", "AnyBot", at(158),
              DISALLOW, "上述拒绝均未改变缓存", line=2)
    expect("fetched_at 仍是 T0+60s",
           d.cache is not None and d.cache.fetched_at == at(60).isoformat(),
           d.cache.fetched_at if d.cache else "None")

    feed(cache, "http://lonely.example/robots.txt", at(0), 304, 100,
         etag="x", expect_accept=False, note="没有前件的 304")
    judge(cache, "http://lonely.example/anything", "AnyBot", at(10),
          NEEDS_REFRESH, "304 不能凭空生成许可")

    # ------------------------------------------------------------------
    section("场景 7：到期点、429/5xx 刷新失败与下一次有效更新")
    # ------------------------------------------------------------------
    feed(cache, "http://flaky.example/robots.txt", at(0), 200, 50,
         body=FLAKY_V1, note="ttl=50s")
    judge(cache, "http://flaky.example/x", "AnyBot", at(49),
          DISALLOW, "到期前一刻仍按旧规则", line=2)
    d = judge(cache, "http://flaky.example/x", "AnyBot", at(50),
              NEEDS_REFRESH, "到期的那一刻必须 needs_refresh")
    expect("过期表示 fresh == False", d.cache is not None and not d.cache.fresh,
           str(d.cache))

    feed(cache, "http://flaky.example/robots.txt", at(60), 429, 50,
         expect_accept=False, note="429 只记录失败")
    judge(cache, "http://flaky.example/x", "AnyBot", at(60),
          NEEDS_REFRESH, "旧表示已过期，429 不延长有效期")
    feed(cache, "http://flaky.example/robots.txt", at(70), 500, 50,
         expect_accept=False, note="5xx 同样只记录失败")
    judge(cache, "http://flaky.example/x", "AnyBot", at(70),
          NEEDS_REFRESH, "500 之后仍无可用策略")

    feed(cache, "http://flaky.example/robots.txt", at(80), 200, 100,
         body=FLAKY_V2, note="更晚的有效 200 恢复服务（失败不推进单调时钟）")
    judge(cache, "http://flaky.example/x", "AnyBot", at(90),
          ALLOW, "新策略不再禁止 /x")
    judge(cache, "http://flaky.example/y", "AnyBot", at(90),
          DISALLOW, "新策略禁止 /y", line=2)
    recorded = cache.failures("http://flaky.example/")
    print(f"  flaky.example 的失败记录: {recorded}")
    expect("429 与 500 均被记录",
           [f["status"] for f in recorded] == [429, 500], str(recorded))
    print()

    feed(cache, "http://steady.example/robots.txt", at(0), 200, 100,
         body=STEADY_ROBOTS, note="ttl=100s")
    feed(cache, "http://steady.example/robots.txt", at(50), 503, 100,
         expect_accept=False, note="有效期内遇到 503")
    judge(cache, "http://steady.example/a", "AnyBot", at(55),
          DISALLOW, "旧表示仍在有效区间：按旧规则判断", line=2)
    judge(cache, "http://steady.example/a", "AnyBot", at(100),
          NEEDS_REFRESH, "失败不延长有效期：到期即 needs_refresh")

    # ------------------------------------------------------------------
    section("场景 8：无效更新与非法参数都不能替换现有有效策略")
    # ------------------------------------------------------------------
    feed(cache, "http://guard.example/robots.txt", at(0), 200, 1000,
         body=GUARD_ROBOTS, note="ttl=1000s 的长期策略")
    feed(cache, "http://guard.example/robots.txt", at(10), 200, 1000,
         body=b"\xff\xfe\x00 not-utf-8", expect_accept=False,
         note="无效 UTF-8")
    oversize = b"User-agent: *\nDisallow: /" + b"a" * (512 * 1024)
    feed(cache, "http://guard.example/robots.txt", at(20), 200, 1000,
         body=oversize, expect_accept=False,
         note=f"超过 512 KiB（{len(oversize)} 字节）")
    feed(cache, "http://guard.example/robots.txt", at(-5), 200, 1000,
         body="User-agent: *\nAllow: /\n", expect_accept=False,
         note="迟到更新（fetched_at 早于上次已接受）")
    d = judge(cache, "http://guard.example/keep", "AnyBot", at(30),
              DISALLOW, "坏编码、超限、迟到更新之后旧策略仍可用", line=2)
    expect("fetched_at 仍是 T0",
           d.cache is not None and d.cache.fetched_at == at(0).isoformat(),
           d.cache.fetched_at if d.cache else "None")

    print("非法时刻与不合类型的参数：")
    naive = datetime(2026, 1, 1, 0, 0, 0)
    expect_error("update 用无时区时刻",
                 lambda: cache.update("http://guard.example/", fetched_at=naive,
                                      status=200, ttl_seconds=10, body=b""))
    expect_error("负 ttl",
                 lambda: cache.update("http://guard.example/", fetched_at=at(40),
                                      status=200, ttl_seconds=-1, body=b""))
    expect_error("body 不是 bytes",
                 lambda: cache.update("http://guard.example/", fetched_at=at(40),
                                      status=200, ttl_seconds=10, body="str"),
                 exc_type=TypeError)
    expect_error("check 用无时区时刻",
                 lambda: cache.check("http://guard.example/keep", "AnyBot", naive))
    expect_error("空 product token",
                 lambda: cache.check("http://guard.example/keep", "", at(40)))
    expect_error("非 ASCII product token",
                 lambda: cache.check("http://guard.example/keep", "中文", at(40)))
    judge(cache, "http://guard.example/keep", "AnyBot", at(40),
          DISALLOW, "非法参数调用之后旧策略依旧可用", line=2)

    feed(cache, "http://gone.example/robots.txt", at(0), 404, 100,
         note="404 建立有到期时间的无规则表示")
    judge(cache, "http://gone.example/anything", "AnyBot", at(50),
          ALLOW, "无规则表示：不限制任何路径")
    judge(cache, "http://gone.example/anything", "AnyBot", at(100),
          NEEDS_REFRESH, "无规则表示同样会到期")
    feed(cache, "http://empty.example/robots.txt", at(0), 200, 100,
         body=b"", note="空 robots 也有效")
    judge(cache, "http://empty.example/foo", "AnyBot", at(50),
          ALLOW, "空文件表示没有路径限制")
    judge(cache, "http://never.example/x", "AnyBot", at(1),
          NEEDS_REFRESH, "从未抓取的 origin")

    # ------------------------------------------------------------------
    section("场景 9：批量检查入口（两个采集器身份）+ 新进程重开数据库核对")
    # ------------------------------------------------------------------
    urls_file = work / "urls.txt"
    urls_file.write_text("\n".join([
        "# 候选 URL 清单（演练用）",
        "http://agents.example/private/x",
        "http://agents.example/all",
        "http://enc.example/~old",
        "http://shared.example/tracked",
        "http://refresh.example/gone",
        "http://gone.example/anything",
        "http://never.example/x",
        "",
    ]), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "robots_admission", "--db", str(db),
         "batch", "--urls", str(urls_file),
         "--agent", "AlphaBot", "--agent", "GammaBot",
         "--now", at(90).isoformat()],
        cwd=str(REPO), capture_output=True, text=True)
    print("批量入口命令输出（JSON 已解析，关键结果如下）：")
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
    expect("批量入口退出码为 0", proc.returncode == 0, proc.stderr)
    batch = json.loads(proc.stdout)
    got = {item["url"]: {a: item["outcomes"][a]["outcome"] for a in ("AlphaBot", "GammaBot")}
           for item in batch["results"]}
    expected_batch = {
        "http://agents.example/private/x": (DISALLOW, ALLOW),
        "http://agents.example/all": (ALLOW, DISALLOW),
        "http://enc.example/~old": (ALLOW, ALLOW),   # EncBot 组不适用，无 * 组
        "http://shared.example/tracked": (DISALLOW, DISALLOW),
        "http://refresh.example/gone": (DISALLOW, DISALLOW),
        "http://gone.example/anything": (ALLOW, ALLOW),
        "http://never.example/x": (NEEDS_REFRESH, NEEDS_REFRESH),
    }
    for url, (want_alpha, want_gamma) in expected_batch.items():
        outcomes = got.get(url, {})
        alpha, gamma = outcomes.get("AlphaBot"), outcomes.get("GammaBot")
        print(f"  {url}\n    AlphaBot -> {alpha}    GammaBot -> {gamma}")
        expect(f"batch AlphaBot {url} == {want_alpha}", alpha == want_alpha, str(alpha))
        expect(f"batch GammaBot {url} == {want_gamma}", gamma == want_gamma, str(gamma))
    # 展示其中一条判断的完整理由（来自批量入口的真实输出）
    sample = next(i for i in batch["results"]
                  if i["url"] == "http://agents.example/private/x")
    print("  示例理由（AlphaBot, agents.example/private/x）：")
    print(f"    {sample['outcomes']['AlphaBot']['reason']}")
    print(f"    命中行：{sample['outcomes']['AlphaBot']['rule']}")
    print()

    # 新进程重开数据库，核对同一判断
    in_process = cache.check("http://refresh.example/gone", "AlphaBot", at(150))
    proc = subprocess.run(
        [sys.executable, "-m", "robots_admission", "--db", str(db),
         "check", "--url", "http://refresh.example/gone",
         "--agent", "AlphaBot", "--now", at(150).isoformat()],
        cwd=str(REPO), capture_output=True, text=True)
    expect("新进程 check 退出码为 0", proc.returncode == 0, proc.stderr)
    reopened = json.loads(proc.stdout)
    print("新进程重开数据库后的同一判断：")
    print(f"  outcome={reopened['outcome']}  rule={reopened['rule']}")
    print(f"  cache={reopened['cache']}")
    expect("新进程判断 outcome 一致", reopened["outcome"] == in_process.outcome,
           reopened["outcome"])
    expect("新进程命中行一致",
           reopened["rule"] is not None and in_process.rule is not None
           and reopened["rule"]["line"] == in_process.rule.line,
           str(reopened["rule"]))
    expect("新进程缓存依据一致（fetched_at / valid_until）",
           reopened["cache"] is not None and in_process.cache is not None
           and reopened["cache"]["fetched_at"] == in_process.cache.fetched_at
           and reopened["cache"]["valid_until"] == in_process.cache.valid_until,
           str(reopened["cache"]))

    # ------------------------------------------------------------------
    print("\n" + "=" * 76)
    print(f"演练结束：共 {checks} 项断言，失败 {len(failures)} 项")
    if failures:
        print("DRILL RESULT: FAILURES")
        for label in failures:
            print(f"  - {label}")
        print(f"scratch retained at: {work}")
        return 1
    print("DRILL RESULT: ALL SCENARIOS PASSED")
    print(f"scratch retained at: {work}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
