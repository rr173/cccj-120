"""Discovery engine: depth-first walk from root maps to pages.

Traversal contract
------------------
* Roots are processed in the order given.  A root's whole subtree is
  finished before the next root starts (depth-first).
* Children of one map are visited in entry order.
* An index may reference another index; depth is ``root = 0``.
* A map node is identified by ``(origin, canonical URL)``.  It is parsed
  **at most once per run**: a shared subgraph reached again is recorded
  as a ``revisit`` edge to the first node instead of being read twice.
* A reference cycle is detected only along the *active* DFS stack, and
  the chain back to the ancestor is reported.  "A and B both point to C"
  is a shared subgraph, not a cycle.
* Every reference edge is recorded, duplicates included; missing map
  files, depth and map-count limit stops are located issues/failure
  nodes, never silently skipped.

Atomic maps
-----------
Each map is staged into SQLite inside one transaction.  Pages, pending
children and the map's success state are committed only after gzip
CRC/trailer, the XML tail and every entry validated.  A rejected map
rolls its stage back completely: no page from it becomes visible and no
child is derived from it.  Independent branches already scheduled
continue.

Memory
------
Python memory is O(active DFS frame depth): one small frame per active
map (map seq, current child entry), plus one parser buffer, one staged
entry and one row of result data.  Maps, pages, sources and edges live
in the run-local SQLite file; ``iter_pages()`` streams them one by one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Sequence

from .errors import (ConfigurationError, MapRejection, SitemapDiscoverError,
                     URLRuleError)
from .mapping import DownloadMapping, MappingEntry
from .profile import Limits
from .reader import MapSummary, parse_map_file
from .store import ScratchStore
from .urls import ParsedURL, parse_url


@dataclass
class PageSource:
    """One valid occurrence of a page inside a map."""

    source_url: str      # canonical map URL
    map_depth: int
    entry_index: int     # 1-based entry number inside that map
    loc: str             # original loc text
    lastmod: Optional[str]

    def to_dict(self) -> dict:
        return {
            "source_url": self.source_url,
            "map_depth": self.map_depth,
            "entry_index": self.entry_index,
            "loc": self.loc,
            "lastmod": self.lastmod,
        }


@dataclass
class PageResult:
    """A distinct page with every valid source occurrence."""

    page_seq: int
    url: str
    origin: str
    lastmod: Optional[str]
    sources: List[PageSource] = field(default_factory=list)

    def to_dict(self, include_sources: bool = True) -> dict:
        return {
            "page_seq": self.page_seq,
            "url": self.url,
            "origin": self.origin,
            "lastmod": self.lastmod,
            "source_count": len(self.sources),
            "sources": [s.to_dict() for s in self.sources]
            if include_sources else [],
        }


@dataclass
class MapEdge:
    parent_map: int
    parent_url: str
    entry_index: int
    child_url: str
    via_root: int
    depth: int
    lastmod: Optional[str]
    target_map: Optional[int]
    resolution: str

    def to_dict(self) -> dict:
        return {
            "parent_map": self.parent_map,
            "parent_url": self.parent_url,
            "entry_index": self.entry_index,
            "child_url": self.child_url,
            "via_root": self.via_root,
            "depth": self.depth,
            # index-level lastmod describes the child map, never its pages
            "lastmod": self.lastmod,
            "target_map": self.target_map,
            "resolution": self.resolution,
        }


@dataclass
class MapNode:
    seq: int
    url: str
    origin: str
    depth: int
    via_root: Optional[int]
    via_parent: Optional[int]
    via_entry: Optional[int]
    status: str
    arrival_count: int
    file_path: Optional[str] = None
    root_kind: Optional[str] = None
    entry_count: int = 0
    page_count: int = 0
    sitemap_count: int = 0
    detail: Optional[dict] = None

    @property
    def is_failure(self) -> bool:
        return self.status != "complete"

    def to_dict(self) -> dict:
        d = {
            "seq": self.seq,
            "url": self.url,
            "origin": self.origin,
            "depth": self.depth,
            "via_root": self.via_root,
            "via_parent": self.via_parent,
            "via_entry": self.via_entry,
            "status": self.status,
            "arrival_count": self.arrival_count,
            "file_path": self.file_path,
            "root_kind": self.root_kind,
            "entries": self.entry_count,
            "pages": self.page_count,
            "sitemaps": self.sitemap_count,
        }
        if self.detail:
            d["failure"] = self.detail
        return d


@dataclass
class Issue:
    """A located failure with no map file of its own (cycle/limit stop)."""

    issue_seq: int
    parent_map: Optional[int]
    parent_url: Optional[str]
    entry_index: Optional[int]
    child_url: str
    depth: int
    kind: str
    reason: str
    chain: Optional[str]

    def to_dict(self) -> dict:
        return {
            "issue_seq": self.issue_seq,
            "parent_map": self.parent_map,
            "parent_url": self.parent_url,
            "entry_index": self.entry_index,
            "child_url": self.child_url,
            "depth": self.depth,
            "kind": self.kind,
            "reason": self.reason,
            "chain": self.chain,
        }


@dataclass
class RootRecord:
    root_index: int
    url: str
    origin: str
    map_seq: Optional[int]
    status: str
    first_root: Optional[int]

    def to_dict(self) -> dict:
        return {
            "root_index": self.root_index,
            "url": self.url,
            "origin": self.origin,
            "map_seq": self.map_seq,
            "status": self.status,
            "first_root": self.first_root,
        }


@dataclass
class DiscoveryReport:
    """Result summary of one run; ``complete`` is explicit."""

    complete: bool
    roots: List[str]
    origins: List[str]
    limits: Limits
    map_nodes: int
    maps_read: int
    map_failures: int
    issue_failures: int
    edges: int
    page_entries: int
    sitemap_entries: int
    pages: int
    status_counts: dict

    def to_dict(self) -> dict:
        return {
            "complete": self.complete,
            "roots": list(self.roots),
            "origins": list(self.origins),
            "limits": self.limits.to_dict(),
            "counts": {
                # distinct map identities attempted (read + failed)
                "map_nodes": self.map_nodes,
                # distinct map files whose bytes were read and validated
                "maps_read": self.maps_read,
                "map_failures": self.map_failures,
                "issue_failures": self.issue_failures,
                # every reference edge, duplicates included
                "edges": self.edges,
                # every valid page entry (raw), before dedup
                "page_entries": self.page_entries,
                # every valid sitemap entry (raw)
                "sitemap_entries": self.sitemap_entries,
                # distinct pages after URL-identity dedup
                "pages": self.pages,
            },
            "map_status_counts": dict(self.status_counts),
        }


class Discovery:
    """One discovery run.  Use as a context manager or call ``close()``.

    ``Discovery(roots, mapping, limits)`` validates everything and creates
    the temporary scratch database.  Call :meth:`run` (once), then iterate
    results or write the report.  ``close()`` removes the scratch data.
    """

    def __init__(self, roots: Sequence[str], mapping: DownloadMapping,
                 limits: Optional[Limits] = None,
                 scratch_dir: Optional[str] = None):
        if not isinstance(mapping, DownloadMapping):
            raise TypeError("mapping must be a DownloadMapping")
        if isinstance(roots, (str, bytes)) or not roots:
            raise ConfigurationError(
                "roots must be a non-empty sequence of URL strings")
        self.limits = (limits or Limits()).validated()
        self.mapping = mapping
        self.root_urls: List[str] = []
        self.root_parsed: List[ParsedURL] = []
        for index, raw in enumerate(roots):
            if not isinstance(raw, str):
                raise ConfigurationError(
                    f"root #{index} is not a string: {raw!r}")
            try:
                parsed = parse_url(raw)
            except URLRuleError as exc:
                raise ConfigurationError(
                    f"root #{index} rejected: {exc}") from exc
            self.root_parsed.append(parsed)
            self.root_urls.append(parsed.identity)
        self.store = ScratchStore(scratch_dir)
        self._ran = False
        self._finished = False
        self._active: List["_Frame"] = []
        self._root_seen: dict = {}
        self._report: Optional[DiscoveryReport] = None

    # -- context manager ------------------------------------------------
    def __enter__(self) -> "Discovery":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.store.close()

    # -- traversal ------------------------------------------------------
    def run(self) -> "Discovery":
        if self._ran:
            raise SitemapDiscoverError("run() may only be called once")
        self._ran = True
        try:
            for root_index, parsed in enumerate(self.root_parsed):
                self._run_root(root_index, parsed)
        finally:
            self._finished = True
            self._report = self._build_report()
        return self

    def _run_root(self, root_index: int, parsed: ParsedURL) -> None:
        key = (parsed.origin, parsed.identity)
        existing = self._root_seen.get(key)
        if existing is not None:
            # The identical root given twice: reported, not parsed again.
            node = self.store.map_by_url(parsed.identity, parsed.origin)
            self.store.register_root(
                root_index, url=parsed.identity, origin=parsed.origin,
                map_seq=node["seq"] if node is not None else None,
                status="duplicate_root", first_root=existing)
            return
        self._root_seen[key] = root_index
        self._active = []
        root = _Frame(parsed=parsed, depth=0, via_root=root_index,
                      via_parent=None, via_entry=None)
        self._active.append(root)
        self._visit_node(root)
        # Root status: "processed" when this position read the map,
        # "revisit" when the map was already handled earlier in the run
        # (complete), "failed" when the root's own map attempt failed.
        if root.parsed_ok:
            root_status = "processed"
        else:
            row = (self.store.map_row(root.map_seq)
                   if root.map_seq is not None else None)
            root_status = ("revisit" if row is not None and
                           row["status"] == "complete" else "failed")
        self.store.register_root(
            root_index, url=parsed.identity, origin=parsed.origin,
            map_seq=root.map_seq, status=root_status)
        self._walk()

    def _walk(self) -> None:
        """Iterative depth-first descent over the active frame stack."""
        while self._active:
            top = self._active[-1]
            if not top.parsed_ok or top.map_seq is None:
                self._active.pop()
                continue
            row = self.store.next_pending(top.map_seq, top.last_entry)
            if row is None:
                self._active.pop()
                continue
            top.last_entry = int(row["entry_index"])
            child = _Frame(parsed=parse_url(row["child_url"]),
                           depth=top.depth + 1,
                           via_root=top.via_root,
                           via_parent=top.map_seq,
                           via_entry=top.last_entry)
            self._active.append(child)
            self._visit_node(child)

    # -- one node -------------------------------------------------------
    def _visit_node(self, frame: "_Frame") -> None:
        """Classify the node already pushed as the top active frame."""
        store = self.store
        parsed = frame.parsed

        # 1. Cycle?  Only ancestor nodes on the active stack count.
        ancestors = self._active[:-1]
        for stack_index, active_frame in enumerate(ancestors):
            if (active_frame.parsed.origin == parsed.origin
                    and active_frame.parsed.identity == parsed.identity):
                chain = " -> ".join(
                    f.parsed.identity for f in ancestors[stack_index:])
                chain += f" -> {parsed.identity}"
                frame.map_seq = active_frame.map_seq
                self._record_edge(frame, active_frame.map_seq, "cycle")
                store.add_issue(
                    parent_map=frame.via_parent, parent_url=None,
                    entry_index=frame.via_entry,
                    child_url=parsed.identity, child_origin=parsed.origin,
                    depth=frame.depth, via_root=frame.via_root,
                    kind="cycle",
                    reason=f"reference cycle: {parsed.identity!r} is "
                           f"already on the active traversal stack",
                    chain=chain)
                return

        # 2. Attempted earlier in this run?  Edge only, never re-read.
        prior = store.map_by_url(parsed.identity, parsed.origin)
        if prior is not None:
            store.bump_arrival(prior["seq"])
            frame.map_seq = prior["seq"]
            self._record_edge(frame, prior["seq"], "revisit")
            return

        # 3. Depth limit (roots are depth 0).
        if frame.depth > self.limits.max_depth:
            self._record_edge(frame, None, "limit_depth")
            store.add_issue(
                parent_map=frame.via_parent, parent_url=None,
                entry_index=frame.via_entry, child_url=parsed.identity,
                child_origin=parsed.origin, depth=frame.depth,
                via_root=frame.via_root, kind="limit_depth",
                reason=f"depth {frame.depth} exceeds max_depth="
                       f"{self.limits.max_depth}; map was not read")
            return

        # 4. Local mapping entry (looked up before the map-count limit:
        #    a branch with no local file was never read and must not
        #    consume the budget of maps actually opened).
        entry = self.mapping.get(parsed.identity)
        if entry is None:
            seq = store.next_map_seq()
            store.register_map(
                seq, url=parsed.identity, origin=parsed.origin,
                depth=frame.depth, via_root=frame.via_root,
                via_parent=frame.via_parent, via_entry=frame.via_entry,
                status="missing_mapping",
                detail={"kind": "missing_mapping",
                        "reason": "no local mapping entry for this URL; "
                                  "the map was not read"})
            frame.map_seq = seq
            self._record_edge(frame, seq, "missing_mapping")
            return

        # 5. Distinct-map limit (only files actually opened count).
        if store.maps_opened() >= self.limits.max_maps:
            self._record_edge(frame, None, "limit_maps")
            store.add_issue(
                parent_map=frame.via_parent, parent_url=None,
                entry_index=frame.via_entry, child_url=parsed.identity,
                child_origin=parsed.origin, depth=frame.depth,
                via_root=frame.via_root, kind="limit_maps",
                reason=f"max_maps={self.limits.max_maps} distinct map files "
                       f"already opened in this run; map was not read")
            return

        self._read_map(frame, entry)

    def _read_map(self, frame: "_Frame", entry: MappingEntry) -> None:
        store = self.store
        parsed = frame.parsed
        seq = store.next_map_seq()
        store.register_map(
            seq, url=parsed.identity, origin=parsed.origin,
            depth=frame.depth, via_root=frame.via_root,
            via_parent=frame.via_parent, via_entry=frame.via_entry,
            status="error", file_path=entry.path,
            detail={"kind": "incomplete",
                    "reason": "map processing did not finish"})
        frame.map_seq = seq
        self._record_edge(frame, seq, "read")
        store.begin_map()
        try:
            summary: MapSummary = parse_map_file(
                entry.path, parsed.origin, self.limits, store.stage_entry)
            store.commit_success(
                seq, root_kind=summary.root_kind,
                entry_count=summary.entries,
                page_count=summary.pages,
                sitemap_count=summary.sitemaps)
            frame.parsed_ok = True
        except MapRejection as exc:
            store.rollback_map()
            d = exc.detail
            store.mark_failure(seq, kind=d.kind, reason=d.reason,
                               entry_index=d.entry_index, line=d.line,
                               column=d.column)
        except Exception as exc:  # pragma: no cover - defensive
            store.rollback_map()
            store.mark_failure(seq, kind="internal_error", reason=str(exc))

    def _record_edge(self, frame: "_Frame", target_map: Optional[int],
                     resolution: str) -> None:
        if frame.via_parent is None:
            return  # roots have no incoming reference edge
        self.store.resolve_edge(frame.via_parent, frame.via_entry,
                                target_map, resolution)

    # -- results --------------------------------------------------------
    def _require_finished(self) -> None:
        if not self._finished:
            raise SitemapDiscoverError(
                "run the discovery before reading results")

    def iter_pages(self) -> Iterator[PageResult]:
        """Stream distinct pages in discovery order, sources included."""
        self._require_finished()
        for page, rows in self.store.iter_pages_with_sources():
            sources = [PageSource(
                source_url=row["map_url"], map_depth=row["map_depth"],
                entry_index=row["entry_index"], loc=row["loc"],
                lastmod=row["lastmod"]) for row in rows]
            yield PageResult(page_seq=page["page_seq"], url=page["url"],
                             origin=page["origin"], lastmod=page["lastmod"],
                             sources=sources)

    def iter_edges(self) -> Iterator[MapEdge]:
        self._require_finished()
        for row in self.store.iter_edges():
            yield MapEdge(
                parent_map=row["parent_map"], parent_url=row["parent_url"],
                entry_index=row["entry_index"], child_url=row["child_url"],
                via_root=row["via_root"], depth=row["depth"],
                lastmod=row["lastmod"], target_map=row["target_map"],
                resolution=row["resolution"])

    def iter_maps(self) -> Iterator[MapNode]:
        self._require_finished()
        for row in self.store.iter_maps():
            yield _map_node(row)

    def iter_failures(self) -> Iterator[MapNode]:
        self._require_finished()
        for node in self.iter_maps():
            if node.is_failure:
                yield node

    def iter_issues(self) -> Iterator[Issue]:
        self._require_finished()
        for row in self.store.iter_issues():
            yield Issue(issue_seq=row["issue_seq"],
                        parent_map=row["parent_map"],
                        parent_url=row["parent_url"],
                        entry_index=row["entry_index"],
                        child_url=row["child_url"],
                        depth=row["depth"], kind=row["kind"],
                        reason=row["reason"], chain=row["chain"])

    def iter_roots(self) -> Iterator[RootRecord]:
        self._require_finished()
        for row in self.store.iter_roots():
            yield RootRecord(
                root_index=row["root_index"], url=row["url"],
                origin=row["origin"], map_seq=row["map_seq"],
                status=row["status"], first_root=row["first_root"])

    def _build_report(self) -> DiscoveryReport:
        store = self.store
        failures = store.failed_map_nodes()
        issues = store.issue_count()
        return DiscoveryReport(
            complete=(failures == 0 and issues == 0),
            roots=list(self.root_urls),
            origins=sorted({p.origin for p in self.root_parsed}),
            limits=self.limits,
            map_nodes=store.map_count(),
            maps_read=store.maps_read(),
            map_failures=failures,
            issue_failures=issues,
            edges=store.edge_count(),
            page_entries=store.source_count(),
            sitemap_entries=store.pending_count(),
            pages=store.page_count(),
            status_counts=store.status_counts(),
        )

    @property
    def report(self) -> DiscoveryReport:
        self._require_finished()
        assert self._report is not None
        return self._report

    # -- report writing -------------------------------------------------
    def write_report(self, out, *, format: str = "jsonl",
                     include_sources: bool = True) -> dict:
        """Stream the full report to ``out`` (path or writable text file).

        ``jsonl`` writes one JSON object per line; ``json`` writes one
        JSON document whose arrays are produced from cursors, never from
        a Python list of all pages.
        """
        self._require_finished()
        if hasattr(out, "write"):
            fh, close = out, False
        else:
            fh, close = open(out, "w", encoding="utf-8"), True
        try:
            report = self.report.to_dict()
            if format == "jsonl":
                self._write_jsonl(fh, report, include_sources)
            elif format == "json":
                self._write_json(fh, report, include_sources)
            else:
                raise ValueError(
                    f"unknown report format {format!r} (jsonl|json)")
        finally:
            if close:
                fh.close()
        return report

    def _write_jsonl(self, fh, report: dict, include_sources: bool) -> None:
        def emit(obj) -> None:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        emit({"type": "report", **report})
        for root in self.iter_roots():
            emit({"type": "root", **root.to_dict()})
        for node in self.iter_maps():
            emit({"type": "map", **node.to_dict()})
        for edge in self.iter_edges():
            emit({"type": "edge", **edge.to_dict()})
        for issue in self.iter_issues():
            emit({"type": "issue", **issue.to_dict()})
        for page in self.iter_pages():
            emit({"type": "page", **page.to_dict(include_sources)})

    def _write_json(self, fh, report: dict, include_sources: bool) -> None:
        fh.write('{"report": ')
        fh.write(json.dumps(report, ensure_ascii=False))
        for key, iterator in (
                ("roots", self.iter_roots()),
                ("maps", self.iter_maps()),
                ("edges", self.iter_edges()),
                ("issues", self.iter_issues())):
            fh.write(f', "{key}": [')
            first = True
            for record in iterator:
                if not first:
                    fh.write(", ")
                first = False
                fh.write(json.dumps(record.to_dict(), ensure_ascii=False))
            fh.write("]")
        fh.write(', "pages": [')
        first = True
        for page in self.iter_pages():
            if not first:
                fh.write(", ")
            first = False
            fh.write(json.dumps(page.to_dict(include_sources),
                                ensure_ascii=False))
        fh.write("]}")


class _Frame:
    """One node on the active DFS stack."""

    __slots__ = ("parsed", "depth", "via_root", "via_parent", "via_entry",
                 "map_seq", "parsed_ok", "last_entry")

    def __init__(self, parsed: ParsedURL, depth: int, via_root: int,
                 via_parent: Optional[int], via_entry: Optional[int]):
        self.parsed = parsed
        self.depth = depth
        self.via_root = via_root
        self.via_parent = via_parent
        self.via_entry = via_entry
        self.map_seq: Optional[int] = None
        self.parsed_ok = False
        self.last_entry = 0


def _map_node(row: dict) -> MapNode:
    detail = None
    if row["detail_kind"] or row["detail_reason"]:
        detail = {
            "kind": row["detail_kind"],
            "reason": row["detail_reason"],
            "entry_index": row["detail_entry"],
            "line": row["detail_line"],
            "column": row["detail_col"],
        }
    return MapNode(
        seq=row["seq"], url=row["url"], origin=row["origin"],
        depth=row["depth"], via_root=row["via_root"],
        via_parent=row["via_parent"], via_entry=row["via_entry"],
        status=row["status"], arrival_count=row["arrival_count"],
        file_path=row["file_path"], root_kind=row["root_kind"],
        entry_count=row["entry_count"], page_count=row["page_count"],
        sitemap_count=row["sitemap_count"], detail=detail)
