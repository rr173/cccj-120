#!/usr/bin/env python3
"""Reproducible drill for sitemapdiscover (offline sitemap discovery).

Run from the repository root:

    python3 -I demo/run_sitemap_drill.py

The script is self-contained (standard library only), never accesses the
network and never parses page contents.  It:

  1. copies the checked-in fixtures into a fresh workspace, runs the two-
     root handover story (two indexes sharing one gzip map, one of them
     also pointing at another index, duplicate pages with different
     lastmods), and prints the discovery order, every page's sources and
     aggregated date.  All expected values are hard-coded here -- they
     were derived from the XML by hand, not by calling the traversal.
  2. runs the failure gallery: a real cycle, a branch whose local file is
     missing, a map broken at its tail, a gzip CRC failure, duplicate
     fields, a bad date, a cross-origin entry, an empty map, a gzip with
     a concatenated second member -- and checks that pages from rejected
     maps never appear while good sibling branches stay in the report.
  3. checks limits (depth, map count, entries, loc length, compressed and
     decompressed bytes) end-to-end through the real traversal.
  4. generates a large map of short URLs and runs a separate, memory-
     capped Linux process that discovers and consumes the result line by
     line, recording input size, page count and peak RSS.
  5. exercises the CLI (mapping -> report -> replay) and its exit codes.

Scratch directories are kept for inspection; inputs are never modified.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sitemapdiscover import (  # noqa: E402
    Discovery,
    DownloadMapping,
    Limits,
    MapRejection,
    SitemapDiscoverError,
)
from sitemapdiscover.reader import parse_map_file  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "sitemap_fixtures"
PY = sys.executable
# Console entry point that works under ``python -I`` (isolated mode skips
# both the cwd and PYTHONPATH, so the repo path is inserted explicitly).
CLI = [PY, "-I", "-c",
       "import sys; sys.path.insert(0, %r); "
       "from sitemapdiscover.__main__ import main; "
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
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def prepare(workspace: Path) -> None:
    for name in ("story-root-a.xml", "story-root-b.xml", "story-shared.xml.gz",
                 "story-sub.xml", "story-leaf-c.xml", "demo-mapping.json",
                 "demo-roots.txt"):
        shutil.copy(FIXTURES / name, workspace / name)


def story_expectations():
    """Hand-written expected result for the story (NOT generated)."""
    # Order: DFS by root order, then entry order.
    # root-a(1) -> shared.gz(2) -> pages widget, "/", widget(dup)
    #            -> sub-index(3) -> widget, gadget
    # root-b(4) -> shared.gz revisit
    #            -> leaf-c(5) -> widget, about, a%2Fb, a/b
    pages = [
        # url, aggregated lastmod, sources (map, entry, lastmod) in order
        ("https://shop.example.com/products/widget", "2026-05-20",
         [("https://shop.example.com/maps/shared.xml.gz", 1, "2026-01-05"),
          ("https://shop.example.com/maps/shared.xml.gz", 3, "2026-04-02"),
          ("https://shop.example.com/maps/sub-index.xml", 1, "2026-03-15"),
          ("https://shop.example.com/maps/leaf-c.xml", 1, "2026-05-20")]),
        ("https://shop.example.com/", None,
         [("https://shop.example.com/maps/shared.xml.gz", 2, None)]),
        ("https://shop.example.com/products/gadget", None,
         [("https://shop.example.com/maps/sub-index.xml", 2, None)]),
        ("https://shop.example.com/about?ref=sitemap", "2026-02-01",
         [("https://shop.example.com/maps/leaf-c.xml", 2, "2026-02-01")]),
        ("https://shop.example.com/files/a%2Fb", None,
         [("https://shop.example.com/maps/leaf-c.xml", 3, None)]),
        ("https://shop.example.com/files/a/b", "2026-01-31",
         [("https://shop.example.com/maps/leaf-c.xml", 4, "2026-01-31")]),
    ]
    maps = [
        ("https://shop.example.com/maps/root-a.xml", "complete", 0),
        ("https://shop.example.com/maps/shared.xml.gz", "complete", 1),
        ("https://shop.example.com/maps/sub-index.xml", "complete", 1),
        ("https://shop.example.com/maps/root-b.xml", "complete", 0),
        ("https://shop.example.com/maps/leaf-c.xml", "complete", 1),
    ]
    counts = {"map_nodes": 5, "maps_read": 5, "map_failures": 0,
              "issue_failures": 0, "edges": 4, "page_entries": 9,
              "sitemap_entries": 4, "pages": 6}
    return pages, maps, counts


def run_story(workspace: Path) -> None:
    section("场景 1：两条根索引共享一张 gzip 地图，另一条再指向一个子索引")
    mapping = DownloadMapping.from_file(workspace / "demo-mapping.json")
    roots = [line.strip() for line in
             (workspace / "demo-roots.txt").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.strip().startswith("#")]
    print("根本顺序:", roots)
    discovery = Discovery(roots, mapping)
    try:
        discovery.run()
        pages, map_expect, count_expect = story_expectations()

        print("\n-- 发现顺序（地图节点按实际处理顺序） --")
        for index, node in enumerate(discovery.iter_maps(), start=1):
            print(f"  {index}. map #{node.seq} depth={node.depth} "
                  f"status={node.status} arrivals={node.arrival_count} "
                  f"entries={node.entry_count} pages={node.page_count} "
                  f"{node.url}")
            expect(f"地图 #{index} == 预期 (url/status/depth)",
                   (node.url, node.status, node.depth) == map_expect[index - 1],
                   (node.url, node.status, node.depth))

        print("\n-- 引用边（含重复边） --")
        edges = list(discovery.iter_edges())
        for edge in edges:
            print(f"  edge {edge.parent_url} [{edge.entry_index}] -> "
                  f"{edge.child_url}  resolution={edge.resolution} "
                  f"lastmod={edge.lastmod}")
        expect("边数 == 4（根索引 2 条 + 共享地图重访 1 条 + 子索引 1 条）",
               len(edges) == 4, len(edges))
        expect("共享 gzip 地图被根 B 重访（resolution=revisit）",
               any(e.resolution == "revisit" and
                   e.child_url.endswith("shared.xml.gz") for e in edges))
        expect("索引里的 lastmod 只挂在下级地图边上，不上到网页",
               all(e.lastmod in (None, "2026-05-01", "2026-04-10",
                                 "2026-06-01") for e in edges))

        print("\n-- 网页、来源与汇总日期 --")
        for page in discovery.iter_pages():
            print(f"  page {page.page_seq}: {page.url}  lastmod={page.lastmod}")
            for source in page.sources:
                print(f"      source_url={source.source_url} "
                      f"entry={source.entry_index} loc={source.loc!r} "
                      f"lastmod={source.lastmod}")
        got_pages = [p.url for p in discovery.iter_pages()]
        expect("页面总数 == 6（9 条原始网页条目去重后）",
               discovery.report.pages == 6, discovery.report.pages)
        expect("原始网页条目数 == 9",
               discovery.report.page_entries == 9, discovery.report.page_entries)
        expect("原始 sitemap 条目数 == 4",
               discovery.report.sitemap_entries == 4,
               discovery.report.sitemap_entries)
        for expected, page in zip(pages, discovery.iter_pages()):
            expect(f"页面 {page.page_seq} url == {expected[0]}",
                   page.url == expected[0], page.url)
            expect(f"页面 {page.page_seq} 汇总 lastmod == {expected[1]}",
                   page.lastmod == expected[1], page.lastmod)
            got_sources = [(s.source_url, s.entry_index, s.lastmod)
                           for s in page.sources]
            expect(f"页面 {page.page_seq} 全部来源位置 == 预期",
                   got_sources == expected[2], got_sources)
        expect("按第一次有效出现顺序编号、无重复网页记录",
               got_pages == [p[0] for p in pages], got_pages)
        expect("索引条目里两条根索引各 2 条下级地图",
               discovery.report.sitemap_entries == 4,
               discovery.report.sitemap_entries)
        expect("发现完整：complete=true 且没有失败节点",
               discovery.report.complete is True,
               discovery.report.complete)
        expect("报告计数 == 预期",
               discovery.report.to_dict()["counts"] == count_expect,
               discovery.report.to_dict()["counts"])
    finally:
        discovery.close()


def run_failure_gallery(workspace: Path) -> None:
    section("场景 2：坏分支 / 环 / 缺来件 / 限额 —— 前面的条目不得混入")
    for name in sorted(p.name for p in FIXTURES.iterdir()
                       if p.name.startswith("full-") and
                       p.name not in ("full-roots.txt", "full-mapping.json")):
        shutil.copy(FIXTURES / name, workspace / name)
    shutil.copy(FIXTURES / "full-roots.txt", workspace / "full-roots.txt")
    shutil.copy(FIXTURES / "full-mapping.json", workspace / "full-mapping.json")

    mapping = DownloadMapping.from_file(workspace / "full-mapping.json")
    roots = [line.strip() for line in
             (workspace / "full-roots.txt").read_text(encoding="utf-8")
             .splitlines() if line.strip() and not line.startswith("#")]
    discovery = Discovery(roots, mapping, scratch_dir=str(workspace / "scratch"))
    try:
        discovery.run()
        print("-- 地图节点 --")
        for node in discovery.iter_maps():
            tail = ""
            if node.detail:
                tail = f"  failure={node.detail['kind']}: {node.detail['reason']}"
            print(f"  map #{node.seq} {node.status} depth={node.depth} "
                  f"entries={node.entry_count} {node.url}{tail}")
        print("-- 引用边 --")
        for edge in discovery.iter_edges():
            print(f"  edge {edge.parent_url} [{edge.entry_index}] -> "
                  f"{edge.child_url}  {edge.resolution}")
        print("-- 逐次失败（环/限额） --")
        for issue in discovery.iter_issues():
            print(f"  issue {issue.kind} entry={issue.entry_index} "
                  f"depth={issue.depth} {issue.child_url}")
            if issue.chain:
                print(f"        chain: {issue.chain}")
        print("-- 网页 --")
        pages = list(discovery.iter_pages())
        for page in pages:
            print(f"  page {page.page_seq}: {page.url} lastmod={page.lastmod}")

        statuses = {n.url.rsplit("/", 1)[-1]: n for n in discovery.iter_maps()}
        expect("末尾坏 XML 的地图状态为 error（invalid_xml）",
               statuses["broken-tail.xml"].status == "error" and
               statuses["broken-tail.xml"].detail["kind"] == "invalid_xml",
               statuses["broken-tail.xml"].detail)
        expect("gzip CRC 错的地图状态为 error（gzip_crc）",
               statuses["crc-bad.xml.gz"].detail["kind"] == "gzip_crc",
               statuses["crc-bad.xml.gz"].detail)
        expect("gzip 拼接第二个 member 被拒绝（gzip_concatenated）",
               statuses["concat.xml.gz"].detail["kind"] == "gzip_concatenated",
               statuses["concat.xml.gz"].detail)
        expect("缺来件分支为 missing_mapping，且带来源条目号",
               statuses["missing.xml"].status == "missing_mapping" and
               statuses["missing.xml"].via_entry == 2,
               statuses["missing.xml"].via_entry)
        expect("重复字段地图被拒绝（duplicate_loc）",
               statuses["dup-fields.xml"].detail["kind"] == "duplicate_loc",
               statuses["dup-fields.xml"].detail)
        expect("非法日期地图被拒绝（invalid_lastmod）",
               statuses["bad-date.xml"].detail["kind"] == "invalid_lastmod",
               statuses["bad-date.xml"].detail)
        expect("跨 origin 地图被拒绝，指出具体条目 2",
               statuses["cross-origin.xml"].detail["kind"] == "cross_origin" and
               statuses["cross-origin.xml"].detail["entry_index"] == 2,
               statuses["cross-origin.xml"].detail)
        expect("空地图有效（0 条，不产生网页）",
               statuses["empty.xml"].status == "complete" and
               statuses["empty.xml"].entry_count == 0)
        issues = list(discovery.iter_issues())
        expect("真环只报一次，链回到来源地图",
               [i.kind for i in issues] == ["cycle"] and
               issues[0].chain ==
               "https://fail.example.com/maps/cycle-a.xml -> "
               "https://fail.example.com/maps/cycle-b.xml -> "
               "https://fail.example.com/maps/cycle-a.xml",
               issues[0].chain if issues else None)
        expect("共享子图只解析一次（shared-leaf 只出现一次地图节点）",
               sum(1 for n in discovery.iter_maps()
                   if n.url.endswith("shared-leaf.xml")) == 1)

        page_urls = [p.url for p in pages]
        expect("末尾坏 XML 之前读到的网页没有混入清单",
               "https://fail.example.com/pages/leak-before-broken" not in page_urls
               and "https://fail.example.com/pages/leak-before-broken-2"
               not in page_urls, page_urls)
        expect("gzip CRC 错之前解出的网页没有混入清单",
               "https://fail.example.com/pages/leak-before-crc" not in page_urls,
               page_urls)
        expect("坏 map 的分支整体不派生（无 leak 前缀网页）",
               not any("/leak" in u for u in page_urls))
        expect("完好的兄弟分支仍在清单里（alpha/beta/gamma/shared/delta）",
               set(page_urls) == {
                   "https://fail.example.com/pages/alpha",
                   "https://fail.example.com/pages/beta",
                   "https://fail.example.com/pages/gamma",
                   "https://fail.example.com/pages/shared",
                   "https://fail.example.com/pages/shared-2",
                   "https://fail.example.com/pages/delta"}, page_urls)
        expect("alpha 汇总日期取两条地图来源的最大值 2026-05-05",
               next(p for p in pages
                    if p.url.endswith("/alpha")).lastmod == "2026-05-05")
        expect("整体显式未完成 complete=false",
               discovery.report.complete is False,
               discovery.report.complete)
        expect("报告计数：3 个失败地图文件 + 1 个环 + 1 个缺来件 = 8 个失败",
               discovery.report.map_failures == 7
               and discovery.report.issue_failures == 1,
               (discovery.report.map_failures, discovery.report.issue_failures))

        scratch_db = workspace / "scratch" / "scratch.sqlite"
        expect("运行期临时 SQLite 在发现期间存在（工作区自建）",
               scratch_db.exists())
    finally:
        discovery.close()
    expect("关闭后运行期临时 SQLite 被删除",
           not (workspace / "scratch" / "scratch.sqlite").exists())


def run_limit_checks(workspace: Path) -> None:
    section("场景 3：各类限额与压缩炸弹/大文件边界")
    NS = "http://www.sitemaps.org/schemas/sitemap/0.9"

    def write(name, data, gz=False):
        path = workspace / name
        path.write_bytes(gzip.compress(data, mtime=0) if gz else data)
        return str(path)

    def reject_case(label, data, kind, **kwargs):
        if kwargs.get("gz"):
            path = write(label + ".xml.gz", data, gz=True)
        else:
            path = write(label + ".xml", data)
        limits = kwargs.pop("limits", None)
        origin = kwargs.pop("origin", "https://lim.example.com")
        try:
            entries = []
            parse_map_file(path, origin, limits or Limits(), entries.append)
        except MapRejection as exc:
            expect(f"{label}: 拒绝类型 == {kind}",
                   exc.kind == kind, exc.kind)
            return
        expect(f"{label}: 应被拒绝（{kind}）", False, "accepted")

    # depth / map count / entries through the real traversal
    m0 = write("lim-d0.xml", f'<sitemapindex xmlns="{NS}">'
               f'<sitemap><loc>https://lim.example.com/lim-d1.xml</loc>'
               f'</sitemap></sitemapindex>'.encode())
    m1 = write("lim-d1.xml", f'<sitemapindex xmlns="{NS}">'
               f'<sitemap><loc>https://lim.example.com/lim-d2.xml</loc>'
               f'</sitemap></sitemapindex>'.encode())
    m2 = write("lim-d2.xml", f'<urlset xmlns="{NS}">'
               f'<url><loc>https://lim.example.com/lim-p</loc></url>'
               f'</urlset>'.encode())
    pairs = [("https://lim.example.com/lim-d0.xml", m0),
             ("https://lim.example.com/lim-d1.xml", m1),
             ("https://lim.example.com/lim-d2.xml", m2)]
    d = Discovery(["https://lim.example.com/lim-d0.xml"],
                  DownloadMapping.from_pairs(pairs, base_dir=str(workspace)),
                  Limits(max_depth=1))
    try:
        d.run()
        issues = list(d.iter_issues())
        expect("max_depth=1 时深度 2 的下级地图被定位拒绝",
               [i.kind for i in issues] == ["limit_depth"] and
               issues[0].child_url.endswith("lim-d2.xml"), issues)
        expect("深度限额 -> complete=false", d.report.complete is False)
    finally:
        d.close()

    d = Discovery(["https://lim.example.com/lim-d0.xml"],
                  DownloadMapping.from_pairs(pairs, base_dir=str(workspace)),
                  Limits(max_maps=2))
    try:
        d.run()
        expect("max_maps=2 时第 3 个不同地图不被读",
               [i.kind for i in d.iter_issues()] == ["limit_maps"],
               list(d.iter_issues()))
    finally:
        d.close()

    three = f'<urlset xmlns="{NS}">' + "".join(
        f'<url><loc>https://lim.example.com/p{i}</loc></url>'
        for i in range(3)) + "</urlset>"
    reject_case("entries", three.encode(), "limit_entries",
                limits=Limits(max_entries_per_map=2))
    reject_case("loclen", (f'<urlset xmlns="{NS}"><url><loc>'
                           f'https://lim.example.com/{"x" * 200}</loc></url>'
                           f'</urlset>').encode(), "limit_loc_length",
                limits=Limits(max_loc_length=50))
    big = f'<urlset xmlns="{NS}"><url><loc>https://lim.example.com/p</loc>'
    big += '<url><loc>https://lim.example.com/q</loc></url>' * 20000
    big += "</urlset>"
    reject_case("filebytes", big.encode(), "limit_file_bytes",
                limits=Limits(max_file_bytes=1000))
    reject_case("decompressed", big.encode(), "limit_decompressed",
                limits=Limits(max_decompressed_bytes=1000), gz=True)
    # a tiny gzip that expands to a lot: the decompressed cap must stop it
    bomb = f'<urlset xmlns="{NS}">' + "".join(
        f'<url><loc>https://lim.example.com/b{i}</loc></url>'
        for i in range(50000)) + "</urlset>"
    path = write("bomb.xml.gz", bomb.encode(), gz=True)
    try:
        parse_map_file(path, "https://lim.example.com",
                       Limits(max_decompressed_bytes=4096),
                       lambda entry: None)
    except MapRejection as exc:
        expect("压缩炸弹在解压字节上限处停止并定位", exc.kind == "limit_decompressed",
               exc.kind)
        expect("压缩炸弹失败不是内存错误",
               "MemoryError" not in exc.reason)
    else:
        expect("压缩炸弹应被拒绝", False)
    expect("炸弹 gzip 文件本身远小于其展开内容（压缩比）",
           os.path.getsize(path) * 5 < len(bomb.encode()),
           (os.path.getsize(path), len(bomb.encode())))


def run_cli_demo(workspace: Path) -> None:
    section("场景 4：命令行从映射文件到报告，失败退出码与逐条输出")
    report = workspace / "cli-report.jsonl"
    proc = subprocess.run(
        CLI + ["discover",
         "--roots", str(workspace / "demo-roots.txt"),
         "--mapping", str(workspace / "demo-mapping.json"),
         "--report", str(report)],
        cwd=str(REPO), capture_output=True, text=True)
    expect("成功发现退出码 0", proc.returncode == 0, proc.returncode)
    summary = json.loads(proc.stdout.strip().splitlines()[-1])
    expect("CLI 摘要 complete=true", summary["complete"] is True)
    replay = subprocess.run(
        CLI + ["pages", "--report", str(report)],
        cwd=str(REPO), capture_output=True, text=True)
    expect("pages 回放退出码 0", replay.returncode == 0, replay.returncode)
    lines = [json.loads(line) for line in replay.stdout.splitlines() if line]
    expect("pages 逐条回放 6 条网页记录", len(lines) == 6, len(lines))

    fail_report = workspace / "cli-fail.jsonl"
    proc = subprocess.run(
        CLI + ["discover",
         "--roots", str(workspace / "full-roots.txt"),
         "--mapping", str(workspace / "full-mapping.json"),
         "--report", str(fail_report)],
        cwd=str(REPO), capture_output=True, text=True)
    expect("未完成发现退出码非零（3）", proc.returncode == 3, proc.returncode)
    obj = json.loads(fail_report.read_text(encoding="utf-8").splitlines()[0])
    expect("报告首行显式 complete=false", obj["complete"] is False)

    bad = subprocess.run(
        CLI + ["discover",
         "--roots", str(workspace / "demo-roots.txt"),
         "--mapping", str(workspace / "demo-mapping.json"),
         "--report", str(workspace / "x.jsonl"),
         "--max-depth", "-1"],
        cwd=str(REPO), capture_output=True, text=True)
    expect("非法限额退出码 2", bad.returncode == 2, bad.returncode)
    err = json.loads(bad.stderr.strip().splitlines()[-1])
    expect("非法限额错误类型 LimitsError", err["error"] == "LimitsError",
           err)


MEMORY_WORKER = """
import json, resource, sys
sys.path.insert(0, {repo!r})
from sitemapdiscover import Discovery, DownloadMapping, Limits

roots = {roots!r}
mapping = DownloadMapping.from_file({mapping!r})
limits = Limits(max_entries_per_map={max_entries})
discovery = Discovery(roots, mapping, limits)
discovery.run()
seen = 0
sources = 0
for page in discovery.iter_pages():
    seen += 1
    sources += len(page.sources)
report = discovery.report
# Stream the report out (one JSON object per line) without building a
# list of pages first; this runs under the same memory cap.
discovery.write_report({report_path!r}, format="jsonl")
discovery.close()
page_lines = 0
with open({report_path!r}, "r", encoding="utf-8") as fh:
    for line in fh:
        if '"type": "page"' in line:
            page_lines += 1
print(json.dumps({{
    "pages": seen, "sources": sources, "page_lines": page_lines,
    "complete": report.complete,
    "maps_read": report.maps_read,
    "page_entries": report.page_entries,
    "max_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
}}))
"""


def _memory_worker(workspace: Path, count: int, max_entries: int) -> Path:
    NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
    name = f"big-map-{count}.xml.gz"
    entries = "".join(
        f"<url><loc>https://big.example.com/p{i}</loc>"
        f"<lastmod>2026-01-{1 + i % 28:02d}</lastmod></url>"
        for i in range(count))
    xml = (f'<urlset xmlns="{NS}">' + entries + "</urlset>").encode()
    big_path = workshop_write(workspace, name,
                              gzip.compress(xml, mtime=0, compresslevel=1))
    mapping_path = workspace / f"big-mapping-{count}.json"
    mapping_path.write_text(json.dumps({"files": [
        {"source_url": f"https://big.example.com/{name}",
         "path": name}]}), encoding="utf-8")
    worker = workspace / f"memory_worker_{count}.py"
    worker.write_text(textwrap.dedent(MEMORY_WORKER.format(
        repo=str(REPO),
        roots=[f"https://big.example.com/{name}"],
        mapping=str(mapping_path),
        max_entries=max_entries,
        report_path=str(workspace / f"report-{count}.jsonl"))),
        encoding="utf-8")
    return worker, big_path, len(xml)


def _run_capped(worker: Path) -> dict:
    # 96 MiB of address space: an implementation that keeps the whole
    # page list (or the whole XML) in Python would die here.
    proc = subprocess.run(
        [PY, "-I", str(worker)], capture_output=True, text=True, timeout=600,
        preexec_fn=lambda: resource.setrlimit(
            resource.RLIMIT_AS, (96 << 20, 96 << 20)))
    if proc.returncode != 0:
        print(proc.stdout, proc.stderr, file=sys.stderr)
        return {"error": proc.returncode, "stderr": proc.stderr[-2000:]}
    return json.loads(proc.stdout.strip().splitlines()[-1])


def run_memory_case(workspace: Path) -> None:
    section("场景 5：大量短 URL、受限内存在独立进程里发现并逐条消费")
    # First prove the default entry cap is a located failure, not a crash.
    worker, big_path, decompressed = _memory_worker(workspace, 120_000,
                                                    100_000)
    print(f"  输入：1 张 gzip urlset，120000 条 <url>，"
          f"解压后 {decompressed} 字节，压缩后 {os.path.getsize(big_path)} 字节")
    small = _run_capped(worker)
    if "error" in small:
        expect("默认每地图条目上限下的独立进程仍能结束", False, small)
        return
    expect("默认 max_entries_per_map=100000 时第 120000 条被定位拒绝",
           small["complete"] is False and small["pages"] == 0,
           small["complete"])
    # Now the real large run, with the entry cap raised explicitly.
    worker_big, big_path, decompressed = _memory_worker(
        workspace, 120_000, 200_000)
    worker_small, _, small_decompressed = _memory_worker(workspace, 1_000,
                                                         200_000)
    result = _run_capped(worker_big)
    if "error" in result:
        expect("受限内存进程成功完成发现", False, result)
        return
    baseline = _run_capped(worker_small)
    expect("受限内存进程成功完成发现", result["complete"] is True)
    expect("逐条消费网页数 == 120000", result["pages"] == 120_000,
           result["pages"])
    expect("来源条数与网页数一致（每网页 1 条来源）",
           result["sources"] == 120_000, result["sources"])
    expect("只读取了 1 张不同地图", result["maps_read"] == 1,
           result["maps_read"])
    expect("报告在受限内存里流式写出 120000 条网页",
           result["page_lines"] == 120_000, result["page_lines"])
    peak_mib = result["max_rss_kb"] / 1024
    base_mib = baseline["max_rss_kb"] / 1024
    print(f"  输入规模: 120000 条网页条目 / {decompressed} 解压字节 / "
          f"{os.path.getsize(big_path)} 压缩字节")
    print(f"  发现结果: pages={result['pages']} sources={result['sources']} "
          f"page_entries={result['page_entries']}")
    print(f"  子进程峰值 RSS: {peak_mib:.1f} MiB（120k 条）；"
          f"对照 1k 条 {base_mib:.1f} MiB；RLIMIT_AS=96 MiB")
    expect("峰值 RSS 明显低于内存上限（< 80 MiB）", peak_mib < 80, peak_mib)
    expect("条目数扩大 120 倍后峰值 RSS 增长 < 16 MiB（不随条目线性增长）",
           peak_mib - base_mib < 16, (base_mib, peak_mib))
    (workspace / "memory_result.json").write_text(
        json.dumps({"input_entries": 120_000,
                    "decompressed_bytes": decompressed,
                    "compressed_bytes": os.path.getsize(big_path),
                    "pages": result["pages"],
                    "peak_rss_kb": result["max_rss_kb"],
                    "baseline_1000_entries_rss_kb": baseline["max_rss_kb"]},
                   ensure_ascii=False, indent=2), encoding="utf-8")


def workshop_write(workspace: Path, name: str, data: bytes) -> Path:
    path = workspace / name
    path.write_bytes(data)
    return path


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="sitemap-drill-"))
    print(f"演练工作区: {workspace}")
    prepare(workspace)
    run_story(workspace)
    run_failure_gallery(workspace)
    run_limit_checks(workspace)
    run_cli_demo(workspace)
    run_memory_case(workspace)

    print("\n" + "=" * 78)
    if failures:
        print(f"FAILED: {len(failures)} / {checks} 项未通过")
        for item in failures:
            print("  -", item)
        return 1
    print(f"ALL PASS: {checks} 项检查全部通过")
    print(f"工作区保留在 {workspace}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
