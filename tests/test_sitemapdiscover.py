"""Regression tests for sitemapdiscover (offline, stdlib only).

Run with the repository root on sys.path, e.g.::

    python3 -m unittest tests.test_sitemapdiscover -v
"""

from __future__ import annotations

import gzip
import json
import os
import tempfile
import unittest
import zlib
from pathlib import Path

from sitemapdiscover import (
    Discovery,
    DownloadMapping,
    Limits,
    MapRejection,
    parse_map_file,
    parse_url,
)
from sitemapdiscover.errors import (ConfigurationError, MappingConflictError,
                                   URLRuleError)

NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


def urlset(entries, extra_ns=""):
    body = []
    for entry in entries:
        if isinstance(entry, str):
            body.append(f"<url><loc>{entry}</loc></url>")
        else:
            loc, lastmod = entry
            lm = f"<lastmod>{lastmod}</lastmod>" if lastmod else ""
            body.append(f"<url><loc>{loc}</loc>{lm}</url>")
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<urlset xmlns="{NS}"{extra_ns}>' + "".join(body) +
            "</urlset>").encode()


def sitemapindex(locs):
    body = "".join(
        f"<sitemap><loc>{loc}</loc>"
        + (f"<lastmod>{lm}</lastmod>" if lm else "")
        + "</sitemap>" for loc, lm in
        [(x, None) if isinstance(x, str) else x for x in locs])
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<sitemapindex xmlns="{NS}">' + body +
            "</sitemapindex>").encode()


class Workspace:
    def __init__(self, testcase):
        self.dir = Path(tempfile.mkdtemp(prefix="sd-test-"))
        testcase.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, data):
        if isinstance(data, str):
            data = data.encode()
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def gzip(self, name, data):
        return self.write(name, gzip.compress(data, mtime=0))

    def mapping(self, pairs):
        return DownloadMapping.from_pairs(pairs, base_dir=self.dir)

    def mapping_file(self, files):
        path = self.dir / "mapping.json"
        path.write_text(json.dumps({"files": files}), encoding="utf-8")
        return str(path)


class URLRuleTests(unittest.TestCase):
    def test_scheme_host_port_normalization(self):
        self.assertEqual(parse_url("HTTP://Example.COM:80/A?Q=1").identity,
                         "http://example.com/A?Q=1")
        self.assertEqual(parse_url("https://example.com:443").identity,
                         "https://example.com/")
        self.assertEqual(parse_url("https://example.com:8443/a").identity,
                         "https://example.com:8443/a")
        self.assertEqual(parse_url("http://[2001:0DB8::1]:8080/x").identity,
                         "http://[2001:db8::1]:8080/x")

    def test_idna_host(self):
        self.assertEqual(parse_url("https://BÜCHER.example/a").identity,
                         "https://xn--bcher-kva.example/a")

    def test_path_and_query_are_preserved(self):
        self.assertEqual(parse_url("https://e.test/a%2Fb").identity,
                         "https://e.test/a%2Fb")
        self.assertNotEqual(parse_url("https://e.test/a%2Fb").identity,
                            parse_url("https://e.test/a/b").identity)
        self.assertNotEqual(parse_url("https://e.test/%7E").identity,
                            parse_url("https://e.test/~").identity)
        self.assertEqual(parse_url("https://e.test/a//b/./c").identity,
                         "https://e.test/a//b/./c")
        self.assertEqual(parse_url("https://e.test/A?B=C").identity,
                         "https://e.test/A?B=C")

    def test_rejections(self):
        for bad in ("ftp://e.test/", "https://u:p@e.test/", "https:///x",
                    "e.test/x", "https://e.test/a b",
                    "https://e.test/a#frag", "https://e.test/a#",
                    "https://e.test:bad/", "https://e.test/%zz",
                    "https://e.test/a\tb"):
            with self.assertRaises(URLRuleError, msg=bad):
                parse_url(bad)


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(self)

    def test_relative_paths_resolve_against_mapping_dir(self):
        sub = self.ws.dir / "sub"
        sub.mkdir()
        (sub / "x.xml").write_bytes(b"x")
        path = self.ws.mapping_file([
            {"source_url": "https://e.test/x.xml", "path": "sub/x.xml"}])
        mapping = DownloadMapping.from_file(path)
        entry = mapping.get("https://e.test/x.xml")
        self.assertEqual(Path(entry.path), sub / "x.xml")

    def test_duplicate_normalized_source_is_conflict(self):
        with self.assertRaises(MappingConflictError) as ctx:
            DownloadMapping.from_file(self.ws.mapping_file([
                {"source_url": "https://e.test/a.xml", "path": "a"},
                {"source_url": "https://E.TEST:443/a.xml", "path": "b"}]))
        self.assertEqual(ctx.exception.first_index, 0)
        self.assertEqual(ctx.exception.second_index, 1)

    def test_path_and_query_are_not_merged(self):
        mapping = self.ws.mapping_file([
            {"source_url": "https://e.test/a%2Fb", "path": "a"},
            {"source_url": "https://e.test/a/b", "path": "b"},
            {"source_url": "https://e.test/a?x=1", "path": "c"}])
        self.assertEqual(len(DownloadMapping.from_file(mapping)), 3)

    def test_duplicate_json_key_rejected(self):
        path = self.ws.dir / "m.json"
        path.write_text('{"files": [], "files": []}', encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            DownloadMapping.from_file(str(path))


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(self)

    def parse(self, data, *, origin="https://e.test", gz=False, limits=None):
        if gz:
            path = self.ws.gzip("m.xml.gz", data)
        else:
            path = self.ws.write("m.xml", data)
        entries = []
        summary = parse_map_file(path, origin, limits or Limits(),
                                 entries.append)
        return summary, entries

    def reject(self, data, kind, **kwargs):
        with self.assertRaises(MapRejection) as ctx:
            self.parse(data, **kwargs)
        self.assertEqual(ctx.exception.kind, kind, ctx.exception.reason)
        return ctx.exception

    def test_urlset_basic_and_trimming(self):
        summary, entries = self.parse(urlset([
            (" https://e.test/a\n", " 2026-01-02 "),
            "https://e.test/b"]))
        self.assertEqual(summary.root_kind, "urlset")
        self.assertEqual(summary.entries, 2)
        self.assertEqual([e.loc for e in entries],
                         ["https://e.test/a", "https://e.test/b"])
        self.assertEqual(entries[0].lastmod, "2026-01-02")
        self.assertEqual([e.index for e in entries], [1, 2])

    def test_sitemapindex_basic(self):
        summary, entries = self.parse(sitemapindex([
            "https://e.test/a.xml", ("https://e.test/b.xml", "2026-01-01")]))
        self.assertEqual(summary.root_kind, "sitemapindex")
        self.assertEqual([e.kind for e in entries], ["sitemap", "sitemap"])
        self.assertEqual(entries[1].lastmod, "2026-01-01")

    def test_gzip_detected_by_magic_not_extension(self):
        data = urlset(["https://e.test/a"])
        summary, _ = self.parse(data, gz=True)
        self.assertEqual(summary.entries, 1)
        # gzip bytes with a .xml name are still decompressed
        path = self.ws.write("plain.xml", gzip.compress(data, mtime=0))
        entries = []
        parse_map_file(path, "https://e.test", Limits(), entries.append)
        self.assertEqual(len(entries), 1)

    def test_gzip_rejections(self):
        data = urlset(["https://e.test/a"])
        good = gzip.compress(data, mtime=0)
        bad_crc = good[:-8] + b"\x00\x00\x00\x00" + good[-4:]
        cases = {
            "gzip_concatenated": good + gzip.compress(data, mtime=0),
            "gzip_trailing": good + b"tail",
            "gzip_truncated": good[:-5],
            "gzip_crc": bad_crc,
            "gzip_invalid": b"\x1f\x8b\x00\x00",
        }
        for kind, payload in cases.items():
            with self.subTest(kind=kind):
                # gzip payloads are written with a .xml.gz name; detection
                # must still use the magic bytes only.
                path = self.ws.write("m.xml.gz", payload)
                with self.assertRaises(MapRejection) as ctx:
                    parse_map_file(path, "https://e.test", Limits(),
                                   lambda entry: None)
                self.assertEqual(ctx.exception.kind, kind,
                                 ctx.exception.reason)

    def test_xml_rejections(self):
        self.reject(b"<urlset", "invalid_xml")
        self.reject(b"<urlset/>", "bad_root")
        self.reject(b"<!DOCTYPE urlset><urlset xmlns='" + NS.encode() +
                    b"'/>", "doctype_forbidden")
        self.reject(b'<?xml version="1.0" encoding="ISO-8859-1"?>'
                    b'<urlset xmlns="' + NS.encode() + b'"/>',
                    "unsupported_encoding")
        self.reject(b'<urlset xmlns="' + NS.encode() + b'"><url><loc>'
                    b'https://e.test/\xff</loc></url></urlset>',
                    "invalid_utf8")

    def test_namespace_decoys_are_ignored_with_subtree(self):
        xml = (f'<urlset xmlns="{NS}" xmlns:e="https://ext/">'
               f'<url><e:loc>https://evil.test/x</e:loc>'
               f'<e:lastmod>bad</e:lastmod>'
               f'<loc>https://e.test/a</loc></url></urlset>').encode()
        summary, entries = self.parse(xml)
        self.assertEqual(summary.entries, 1)
        self.assertEqual(entries[0].loc, "https://e.test/a")

    def test_extension_only_loc_is_missing_loc(self):
        xml = (f'<urlset xmlns="{NS}" xmlns:e="https://ext/">'
               f'<url><e:loc>https://e.test/a</e:loc></url></urlset>').encode()
        self.reject(xml, "missing_loc")

    def test_duplicate_and_invalid_fields(self):
        base = f'<urlset xmlns="{NS}">'
        self.reject((base + "<url><loc>https://e.test/a</loc>"
                     "<loc>https://e.test/b</loc></url></urlset>").encode(),
                    "duplicate_loc")
        self.reject((base + "<url><loc>https://e.test/a</loc>"
                     "<lastmod>2026-01-01</lastmod>"
                     "<lastmod>2026-01-02</lastmod></url></urlset>").encode(),
                    "duplicate_lastmod")
        self.reject((base + "<url><lastmod>2026-01-01</lastmod></url>"
                     "</urlset>").encode(), "missing_loc")
        self.reject((base + "<url><loc>   </loc></url></urlset>").encode(),
                    "empty_loc")
        self.reject((base + "<url><loc>https://e.test/a</loc>"
                     "<lastmod>2026-13-01</lastmod></url></urlset>").encode(),
                    "invalid_lastmod")
        self.reject((base + "<url><loc>https://e.test/a</loc>"
                     "<lastmod>2026-01-01T00:00:00Z</lastmod></url>"
                     "</urlset>").encode(), "invalid_lastmod")

    def test_cross_origin_rejected_with_entry_number(self):
        exc = self.reject(urlset(["https://e.test/a",
                                  "https://other.test/b"]), "cross_origin")
        self.assertEqual(exc.detail.entry_index, 2)
        self.assertIn("other.test", exc.reason)

    def test_limits(self):
        self.reject(urlset([f"https://e.test/{i}" for i in range(3)]),
                    "limit_entries", limits=Limits(max_entries_per_map=2))
        self.reject(urlset(["https://e.test/" + "x" * 100]),
                    "limit_loc_length", limits=Limits(max_loc_length=20))
        self.reject(urlset(["https://e.test/a"]), "limit_decompressed",
                    limits=Limits(max_decompressed_bytes=30))
        self.reject(urlset(["https://e.test/a"]), "limit_file_bytes",
                    limits=Limits(max_file_bytes=30))

    def test_empty_urlset_is_valid(self):
        summary, entries = self.parse(urlset([]))
        self.assertEqual((summary.entries, entries), (0, []))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace(self)

    def run_discovery(self, roots, pairs, limits=None):
        mapping = self.ws.mapping(pairs)
        discovery = Discovery(roots, mapping, limits)
        self.addCleanup(discovery.close)
        discovery.run()
        return discovery

    def test_shared_subgraph_read_once_cycle_chain(self):
        a = self.ws.write("a.xml", sitemapindex(
            ["https://e.test/c.xml", "https://e.test/b.xml"]))
        b = self.ws.write("b.xml", sitemapindex(["https://e.test/c.xml"]))
        c = self.ws.write("c.xml", urlset(["https://e.test/p",
                                           ("https://e.test/p", "2026-05-05")]))
        d = self.run_discovery(["https://e.test/a.xml"], [
            ("https://e.test/a.xml", a), ("https://e.test/b.xml", b),
            ("https://e.test/c.xml", c)])
        nodes = {node.url: node for node in d.iter_maps()}
        self.assertEqual(nodes["https://e.test/c.xml"].arrival_count, 2)
        self.assertEqual(nodes["https://e.test/c.xml"].entry_count, 2)
        edges = list(d.iter_edges())
        # a -> c (read), a -> b (read), b -> c (revisit)
        self.assertEqual([e.resolution for e in edges],
                         ["read", "read", "revisit"])
        self.assertTrue(d.report.complete)

    def test_real_cycle_reports_chain_and_is_incomplete(self):
        a = self.ws.write("a.xml", sitemapindex(["https://e.test/b.xml"]))
        b = self.ws.write("b.xml", sitemapindex(["https://e.test/a.xml"]))
        d = self.run_discovery(["https://e.test/a.xml"], [
            ("https://e.test/a.xml", a), ("https://e.test/b.xml", b)])
        issues = list(d.iter_issues())
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].kind, "cycle")
        self.assertEqual(
            issues[0].chain,
            "https://e.test/a.xml -> https://e.test/b.xml -> "
            "https://e.test/a.xml")
        self.assertFalse(d.report.complete)

    def test_failed_map_does_not_leak_its_pages_or_children(self):
        good = self.ws.write("good.xml", urlset(["https://e.test/ok"]))
        broken = self.ws.write(
            "broken.xml",
            f'<urlset xmlns="{NS}"><url><loc>https://e.test/leak</loc>'
            f'</url><url><loc>https://e.test/leak2</loc>')
        root = self.ws.write("root.xml", sitemapindex(
            ["https://e.test/broken.xml", "https://e.test/good.xml"]))
        d = self.run_discovery(["https://e.test/root.xml"], [
            ("https://e.test/root.xml", root),
            ("https://e.test/broken.xml", broken),
            ("https://e.test/good.xml", good)])
        pages = [p.url for p in d.iter_pages()]
        self.assertEqual(pages, ["https://e.test/ok"])
        self.assertEqual(d.report.page_entries, 1)
        self.assertFalse(d.report.complete)

    def test_missing_mapping_is_a_located_failure(self):
        root = self.ws.write("root.xml", sitemapindex(
            ["https://e.test/unknown.xml"]))
        d = self.run_discovery(["https://e.test/root.xml"],
                               [("https://e.test/root.xml", root)])
        node = next(d.iter_failures())
        self.assertEqual(node.status, "missing_mapping")
        self.assertEqual(node.parent if hasattr(node, "parent") else
                         node.via_entry, 1)

    def test_depth_and_map_limits(self):
        d0 = self.ws.write("d0.xml", sitemapindex(["https://e.test/d1.xml"]))
        d1 = self.ws.write("d1.xml", sitemapindex(["https://e.test/d2.xml"]))
        d2 = self.ws.write("d2.xml", urlset(["https://e.test/p"]))
        pairs = [("https://e.test/d0.xml", d0), ("https://e.test/d1.xml", d1),
                 ("https://e.test/d2.xml", d2)]
        d = self.run_discovery(["https://e.test/d0.xml"], pairs,
                               Limits(max_depth=1))
        issues = list(d.iter_issues())
        self.assertEqual([(i.kind, i.child_url) for i in issues],
                         [("limit_depth", "https://e.test/d2.xml")])
        self.assertFalse(d.report.complete)

        d = self.run_discovery(["https://e.test/d0.xml"], pairs,
                               Limits(max_maps=2))
        self.assertEqual([i.kind for i in d.iter_issues()], ["limit_maps"])

    def test_duplicate_root_and_mutual_roots(self):
        a = self.ws.write("a.xml", sitemapindex(["https://e.test/b.xml"]))
        b = self.ws.write("b.xml", sitemapindex(["https://e.test/a.xml"]))
        d = self.run_discovery(
            ["https://e.test/a.xml", "https://e.test/a.xml",
             "https://e.test/b.xml"],
            [("https://e.test/a.xml", a), ("https://e.test/b.xml", b)])
        roots = list(d.iter_roots())
        self.assertEqual([r.status for r in roots],
                         ["processed", "duplicate_root", "revisit"])
        # a -> b -> a is a cycle, so the run is explicitly incomplete
        self.assertFalse(d.report.complete)

    def test_lastmod_aggregation_and_sources(self):
        m1 = self.ws.write("m1.xml", urlset([
            ("https://e.test/p", "2026-01-01"), "https://e.test/p"]))
        m2 = self.ws.write("m2.xml", urlset([("https://e.test/p",
                                             "2026-03-01")]))
        root = self.ws.write("root.xml", sitemapindex(
            ["https://e.test/m1.xml", "https://e.test/m2.xml"]))
        d = self.run_discovery(["https://e.test/root.xml"], [
            ("https://e.test/root.xml", root),
            ("https://e.test/m1.xml", m1), ("https://e.test/m2.xml", m2)])
        pages = list(d.iter_pages())
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].lastmod, "2026-03-01")
        self.assertEqual(len(pages[0].sources), 3)
        self.assertEqual([s.entry_index for s in pages[0].sources], [1, 2, 1])

    def test_index_lastmod_not_applied_to_pages(self):
        leaf = self.ws.write("leaf.xml", urlset(["https://e.test/p"]))
        root = self.ws.write("root.xml", sitemapindex(
            [("https://e.test/leaf.xml", "2026-09-09")]))
        d = self.run_discovery(["https://e.test/root.xml"], [
            ("https://e.test/root.xml", root),
            ("https://e.test/leaf.xml", leaf)])
        page = next(d.iter_pages())
        self.assertIsNone(page.lastmod)
        edge = next(d.iter_edges())
        self.assertEqual(edge.lastmod, "2026-09-09")

    def test_no_pages_only_empty_maps(self):
        empty = self.ws.write("empty.xml", urlset([]))
        root = self.ws.write("root.xml", sitemapindex(
            ["https://e.test/empty.xml"]))
        d = self.run_discovery(["https://e.test/root.xml"], [
            ("https://e.test/root.xml", root),
            ("https://e.test/empty.xml", empty)])
        self.assertEqual(d.report.pages, 0)
        self.assertTrue(d.report.complete)

    def test_missing_mapping_does_not_consume_max_maps(self):
        leaf = self.ws.write("leaf.xml", urlset(["https://e.test/ok"]))
        root = self.ws.write("root.xml", sitemapindex(
            ["https://e.test/absent.xml", "https://e.test/leaf.xml"]))
        d = self.run_discovery(
            ["https://e.test/root.xml"],
            [("https://e.test/root.xml", root),
             ("https://e.test/leaf.xml", leaf)],
            Limits(max_maps=2))
        # The missing file is itself a reported failure (complete=false),
        # but it must not consume the max_maps budget: the leaf is still
        # read and no limit_maps issue is raised.
        self.assertEqual([i.kind for i in d.iter_issues()], [])
        self.assertEqual([p.url for p in d.iter_pages()],
                         ["https://e.test/ok"])
        self.assertFalse(d.report.complete)

    def test_duplicate_child_edges_are_all_reported(self):
        leaf = self.ws.write("leaf.xml", urlset(["https://e.test/p"]))
        root = self.ws.write("root.xml", sitemapindex(
            ["https://e.test/leaf.xml", "https://e.test/leaf.xml"]))
        d = self.run_discovery(
            ["https://e.test/root.xml"],
            [("https://e.test/root.xml", root),
             ("https://e.test/leaf.xml", leaf)])
        edges = list(d.iter_edges())
        self.assertEqual([(e.entry_index, e.resolution) for e in edges],
                         [(1, "read"), (2, "revisit")])
        self.assertEqual(d.report.map_nodes, 2)
        self.assertEqual(d.report.sitemap_entries, 2)

    def test_mismatched_entry_kind_is_rejected(self):
        path = self.ws.write("mix.xml", (
            f'<urlset xmlns="{NS}"><sitemap><loc>https://e.test/x.xml'
            f'</loc></sitemap></urlset>').encode())
        with self.assertRaises(MapRejection) as ctx:
            parse_map_file(path, "https://e.test", Limits(), lambda e: None)
        self.assertEqual(ctx.exception.kind, "mismatched_entry")

    def test_scratch_directory_is_removed(self):
        scratch = self.ws.dir / "scratch"
        scratch.mkdir()
        mapping = self.ws.mapping([])
        discovery = Discovery(["https://e.test/x.xml"], mapping,
                              scratch_dir=str(scratch))
        discovery.run()
        self.assertTrue((scratch / "scratch.sqlite").exists())
        discovery.close()
        self.assertFalse((scratch / "scratch.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
