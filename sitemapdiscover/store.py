"""Run-local SQLite scratch store.

The database lives in a temporary directory created by this process and
is deleted when the discovery run closes.  It is **not** a durable
business directory: nothing in it is meant to be reused across runs.

Tables (all bulk state, so Python never holds the whole graph):

``maps``     one row per **distinct map identity** ``(origin, url)`` that
             was attempted: read successfully (``complete``), rejected
             while parsing (``error``), or never opened because the
             mapping has no entry (``missing_mapping``).  ``arrival_count``
             counts how many references arrived at it.
``issues``   per-occurrence failures that have no map file of their own:
             reference cycles (with the active-stack chain), depth and
             map-count limit stops.
``edges``    every reference edge, duplicates included, with its
             resolution (``read``, ``revisit``, ``cycle``,
             ``limit_depth``, ``limit_maps``).
``roots``    one row per root position given by the caller.
``stage``    entries of the map currently being parsed; visible only
             after the whole file validated (``commit_success``).
``pending``  validated sitemap children of one map, in entry order,
             consumed one at a time by the DFS.
``pages``    one row per distinct page: discovery order, aggregated
             lastmod.
``sources``  every valid (map, entry) occurrence of a page.

Bulk data stays in SQLite.  Python memory is O(active DFS stack depth)
plus one parser buffer, one staged entry and one row of result data.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE maps (
    seq          INTEGER PRIMARY KEY,
    url          TEXT NOT NULL,
    origin       TEXT NOT NULL,
    depth        INTEGER NOT NULL,
    via_root     INTEGER,
    via_parent   INTEGER,
    via_entry    INTEGER,
    status       TEXT NOT NULL,          -- complete | error | missing_mapping
    file_path    TEXT,
    root_kind    TEXT,
    entry_count  INTEGER NOT NULL DEFAULT 0,
    page_count   INTEGER NOT NULL DEFAULT 0,
    sitemap_count INTEGER NOT NULL DEFAULT 0,
    arrival_count INTEGER NOT NULL DEFAULT 1,
    detail_kind  TEXT,
    detail_reason TEXT,
    detail_entry INTEGER,
    detail_line  INTEGER,
    detail_col   INTEGER
);
CREATE UNIQUE INDEX ux_maps_url ON maps (origin, url);

CREATE TABLE issues (
    issue_seq    INTEGER PRIMARY KEY,
    parent_map   INTEGER,
    parent_url   TEXT,
    entry_index  INTEGER,
    child_url    TEXT NOT NULL,
    child_origin TEXT NOT NULL,
    depth        INTEGER NOT NULL,
    via_root     INTEGER,
    kind         TEXT NOT NULL,
    reason       TEXT NOT NULL,
    chain        TEXT
);

CREATE TABLE edges (
    edge_seq     INTEGER PRIMARY KEY,
    parent_map   INTEGER NOT NULL,
    parent_url   TEXT NOT NULL,
    entry_index  INTEGER NOT NULL,
    child_url    TEXT NOT NULL,
    child_origin TEXT NOT NULL,
    via_root     INTEGER NOT NULL,
    depth        INTEGER NOT NULL,
    lastmod      TEXT,
    target_map   INTEGER,
    resolution   TEXT NOT NULL
);

CREATE TABLE roots (
    root_index   INTEGER PRIMARY KEY,
    url          TEXT NOT NULL,
    origin       TEXT NOT NULL,
    map_seq      INTEGER,
    status       TEXT NOT NULL,          -- read | revisit | duplicate_root
    first_root   INTEGER
);

CREATE TABLE stage (
    entry_index  INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    loc          TEXT NOT NULL,
    url          TEXT NOT NULL,
    origin       TEXT NOT NULL,
    lastmod      TEXT,
    line         INTEGER NOT NULL,
    PRIMARY KEY (entry_index, kind)
);

CREATE TABLE pending (
    map_seq      INTEGER NOT NULL,
    entry_index  INTEGER NOT NULL,
    child_url    TEXT NOT NULL,
    child_origin TEXT NOT NULL,
    PRIMARY KEY (map_seq, entry_index)
);

CREATE TABLE pages (
    page_seq     INTEGER PRIMARY KEY,
    url          TEXT NOT NULL UNIQUE,
    origin       TEXT NOT NULL,
    first_map    INTEGER NOT NULL,
    first_entry  INTEGER NOT NULL,
    lastmod      TEXT
);
CREATE INDEX ix_pages_origin ON pages (origin);

CREATE TABLE sources (
    source_seq   INTEGER PRIMARY KEY,
    page_seq     INTEGER NOT NULL,
    map_seq      INTEGER NOT NULL,
    entry_index  INTEGER NOT NULL,
    loc          TEXT NOT NULL,
    lastmod      TEXT
);
CREATE INDEX ix_sources_page ON sources (page_seq, source_seq);
"""


class ScratchStore:
    """Temporary, run-scoped SQLite store used by the discovery engine."""

    def __init__(self, directory: Optional[str] = None):
        self._own_dir = directory is None
        self._created_dir = directory is not None and             not os.path.isdir(directory)
        if directory is None:
            directory = tempfile.mkdtemp(prefix="sitemapdiscover-")
        self.directory = os.path.abspath(directory)
        os.makedirs(self.directory, exist_ok=True)
        self.db_path = os.path.join(self.directory, "scratch.sqlite")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.isolation_level = None  # explicit transactions
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA synchronous=OFF")
        # TEMP b-trees (sorting, UNIQUE lookups) go to disk, not RAM;
        # the page cache is capped at 2 MiB.
        self.conn.execute("PRAGMA temp_store=FILE")
        self.conn.execute("PRAGMA cache_size=-2048")
        self.conn.executescript(_SCHEMA)
        self._in_transaction = False
        self.closed = False

    # -- transactions around one map -----------------------------------
    def begin_map(self) -> None:
        self.conn.execute("BEGIN")
        self.conn.execute("DELETE FROM stage")
        self._in_transaction = True

    def stage_entry(self, record) -> None:
        self.conn.execute(
            "INSERT INTO stage (entry_index, kind, loc, url, origin, "
            "lastmod, line) VALUES (?,?,?,?,?,?,?)",
            (record.index, record.kind, record.loc, record.parsed.identity,
             record.parsed.origin, record.lastmod, record.line))

    def rollback_map(self) -> None:
        if self._in_transaction:
            self.conn.execute("ROLLBACK")
            self._in_transaction = False

    def commit_success(self, map_seq: int, *, root_kind: str,
                       entry_count: int, page_count: int,
                       sitemap_count: int) -> None:
        """Promote the staged map and COMMIT atomically.

        New pages are numbered in this map's entry order; every page
        occurrence becomes a source row; the page's aggregate lastmod is
        the maximum valid date seen so far (a source without a date
        never clears an existing date).  Sitemap entries become pending
        children for the DFS.  All of this is inside one transaction, so
        nothing from the map can be visible before its validation ends.
        """
        for row in _cursor_iter(self.conn.execute(
                "SELECT entry_index, kind, loc, url, origin, lastmod "
                "FROM stage ORDER BY entry_index")):
            if row["kind"] == "page":
                self.conn.execute(
                    "INSERT OR IGNORE INTO pages (url, origin, first_map, "
                    "first_entry, lastmod) VALUES (?,?,?,?,?)",
                    (row["url"], row["origin"], map_seq,
                     row["entry_index"], row["lastmod"]))
                page = self.conn.execute(
                    "SELECT page_seq, lastmod FROM pages WHERE url = ?",
                    (row["url"],)).fetchone()
                page_seq = page["page_seq"]
                self.conn.execute(
                    "INSERT INTO sources (page_seq, map_seq, entry_index, "
                    "loc, lastmod) VALUES (?,?,?,?,?)",
                    (page_seq, map_seq, row["entry_index"], row["loc"],
                     row["lastmod"]))
                if row["lastmod"] is not None and (
                        page["lastmod"] is None
                        or row["lastmod"] > page["lastmod"]):
                    self.conn.execute(
                        "UPDATE pages SET lastmod = ? WHERE page_seq = ?",
                        (row["lastmod"], page_seq))
            else:
                self.conn.execute(
                    "INSERT INTO pending (map_seq, entry_index, child_url, "
                    "child_origin) VALUES (?,?,?,?)",
                    (map_seq, row["entry_index"], row["url"], row["origin"]))
                self.conn.execute(
                    "INSERT INTO edges (parent_map, parent_url, "
                    "entry_index, child_url, child_origin, via_root, "
                    "depth, lastmod, target_map, resolution) "
                    "VALUES (?,?,?,?,?,?,?,?,NULL,'pending')",
                    (map_seq, self.map_url(map_seq), row["entry_index"],
                     row["url"], row["origin"], self.map_via_root(map_seq),
                     self.map_depth(map_seq) + 1, row["lastmod"]))
        self.conn.execute(
            "UPDATE maps SET status = 'complete', root_kind = ?, "
            "entry_count = ?, page_count = ?, sitemap_count = ?, "
            "detail_kind = NULL, detail_reason = NULL, detail_entry = NULL, "
            "detail_line = NULL, detail_col = NULL WHERE seq = ?",
            (root_kind, entry_count, page_count, sitemap_count, map_seq))
        self.conn.execute("DELETE FROM stage")
        self.conn.execute("COMMIT")
        self._in_transaction = False

    # -- map nodes -----------------------------------------------------
    def map_count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM maps").fetchone()[0])

    def next_map_seq(self) -> int:
        return int(self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM maps").fetchone()[0])

    def register_map(self, seq: int, *, url: str, origin: str, depth: int,
                     via_root: Optional[int], via_parent: Optional[int],
                     via_entry: Optional[int], status: str,
                     file_path: Optional[str] = None,
                     root_kind: Optional[str] = None,
                     entry_count: int = 0, page_count: int = 0,
                     sitemap_count: int = 0,
                     detail: Optional[Any] = None) -> None:
        d = detail or {}
        self.conn.execute(
            "INSERT INTO maps (seq, url, origin, depth, via_root, "
            "via_parent, via_entry, status, file_path, root_kind, "
            "entry_count, page_count, sitemap_count, detail_kind, "
            "detail_reason, detail_entry, detail_line, detail_col) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (seq, url, origin, depth, via_root, via_parent, via_entry,
             status, file_path, root_kind, entry_count, page_count,
             sitemap_count, d.get("kind"), d.get("reason"),
             d.get("entry_index"), d.get("line"), d.get("col")))

    def map_by_url(self, url: str, origin: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM maps WHERE url = ? AND origin = ?",
            (url, origin)).fetchone()
        return dict(row) if row is not None else None

    def bump_arrival(self, seq: int) -> None:
        self.conn.execute(
            "UPDATE maps SET arrival_count = arrival_count + 1 WHERE seq = ?",
            (seq,))

    def add_issue(self, *, parent_map: Optional[int],
                  parent_url: Optional[str], entry_index: Optional[int],
                  child_url: str, child_origin: str, depth: int,
                  via_root: Optional[int], kind: str, reason: str,
                  chain: Optional[str] = None) -> None:
        if parent_url is None and parent_map is not None:
            row = self.conn.execute(
                "SELECT url FROM maps WHERE seq = ?", (parent_map,)).fetchone()
            parent_url = row["url"] if row is not None else None
        self.conn.execute(
            "INSERT INTO issues (parent_map, parent_url, entry_index, "
            "child_url, child_origin, depth, via_root, kind, reason, "
            "chain) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (parent_map, parent_url, entry_index, child_url, child_origin,
             depth, via_root, kind, reason, chain))

    def map_row(self, map_seq: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM maps WHERE seq = ?", (map_seq,)).fetchone()
        return dict(row) if row is not None else None

    def map_url(self, map_seq: int) -> str:
        row = self.conn.execute(
            "SELECT url FROM maps WHERE seq = ?", (map_seq,)).fetchone()
        return row["url"] if row is not None else ""

    def map_via_root(self, map_seq: int) -> int:
        row = self.conn.execute(
            "SELECT via_root FROM maps WHERE seq = ?", (map_seq,)).fetchone()
        return int(row["via_root"]) if row is not None else -1

    def map_depth(self, map_seq: int) -> int:
        row = self.conn.execute(
            "SELECT depth FROM maps WHERE seq = ?", (map_seq,)).fetchone()
        return int(row["depth"]) if row is not None else 0

    def resolve_edge(self, parent_map: int, entry_index: int,
                     target_map: Optional[int], resolution: str) -> None:
        cur = self.conn.execute(
            "UPDATE edges SET target_map = ?, resolution = ? "
            "WHERE parent_map = ? AND entry_index = ?",
            (target_map, resolution, parent_map, entry_index))
        if cur.rowcount == 0:  # defensive: edge must have been created
            raise RuntimeError(
                f"edge ({parent_map},{entry_index}) was never staged")

    def register_root(self, root_index: int, *, url: str, origin: str,
                      map_seq: Optional[int], status: str,
                      first_root: Optional[int] = None) -> None:
        self.conn.execute(
            "INSERT INTO roots (root_index, url, origin, map_seq, status, "
            "first_root) VALUES (?,?,?,?,?,?)",
            (root_index, url, origin, map_seq, status, first_root))

    def mark_failure(self, seq: int, *, kind: str, reason: str,
                     entry_index: Optional[int] = None,
                     line: Optional[int] = None,
                     column: Optional[int] = None) -> None:
        self.conn.execute(
            "UPDATE maps SET status='error', detail_kind=?, "
            "detail_reason=?, detail_entry=?, detail_line=?, "
            "detail_col=? WHERE seq=?",
            (kind, reason, entry_index, line, column, seq))

    def pending_count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM pending").fetchone()[0])

    def next_pending(self, map_seq: int,
                     after_entry: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT entry_index, child_url, child_origin FROM pending "
            "WHERE map_seq = ? AND entry_index > ? "
            "ORDER BY entry_index LIMIT 1",
            (map_seq, after_entry)).fetchone()
        return dict(row) if row is not None else None

    # -- counters ------------------------------------------------------
    def maps_attempted(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM maps").fetchone()[0])

    def maps_read(self) -> int:
        """Distinct map files actually opened and fully validated."""
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM maps WHERE status = 'complete'"
        ).fetchone()[0])

    def maps_opened(self) -> int:
        """Distinct map identities whose local file was opened.

        ``complete`` and ``error`` nodes both had their bytes read; a
        ``missing_mapping`` node never opened a file and therefore does
        not consume the ``max_maps`` budget.
        """
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM maps WHERE status IN ('complete','error')"
        ).fetchone()[0])

    def failed_map_nodes(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM maps WHERE status != 'complete'"
        ).fetchone()[0])

    def issue_count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM issues").fetchone()[0])

    def page_count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM pages").fetchone()[0])

    def source_count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM sources").fetchone()[0])

    def edge_count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM edges").fetchone()[0])

    def status_counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for row in _cursor_iter(self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM maps GROUP BY status")):
            result[row["status"]] = int(row["n"])
        for row in _cursor_iter(self.conn.execute(
                "SELECT kind, COUNT(*) AS n FROM issues GROUP BY kind")):
            result[row["kind"]] = result.get(row["kind"], 0) + int(row["n"])
        return result

    # -- iteration (never materializes everything) ---------------------
    def iter_maps(self):
        yield from _cursor_iter(
            self.conn.execute("SELECT * FROM maps ORDER BY seq"))

    def iter_edges(self):
        yield from _cursor_iter(
            self.conn.execute("SELECT * FROM edges ORDER BY edge_seq"))

    def iter_issues(self):
        yield from _cursor_iter(
            self.conn.execute("SELECT * FROM issues ORDER BY issue_seq"))

    def iter_roots(self):
        yield from _cursor_iter(
            self.conn.execute("SELECT * FROM roots ORDER BY root_index"))

    def iter_pages(self):
        yield from _cursor_iter(
            self.conn.execute("SELECT * FROM pages ORDER BY page_seq"))

    def iter_pages_with_sources(self):
        """Yield (page_row, [source rows]) one page at a time.

        Only the current page's sources are held; the source cursor is
        ordered by page_seq and advances in lock-step.
        """
        data_cur = self.conn.execute(
            "SELECT s.page_seq, s.source_seq, s.map_seq, s.entry_index, "
            "s.loc, s.lastmod, m.url AS map_url, m.depth AS map_depth, "
            "m.file_path AS map_file "
            "FROM sources s JOIN maps m ON m.seq = s.map_seq "
            "ORDER BY s.page_seq, s.source_seq")
        data_iter = _cursor_iter(data_cur)
        current = next(data_iter, None)
        for page in _cursor_iter(
                self.conn.execute("SELECT * FROM pages ORDER BY page_seq")):
            bucket = []
            while current is not None and \
                    current["page_seq"] == page["page_seq"]:
                bucket.append(current)
                current = next(data_iter, None)
            yield page, bucket

    # -- lifecycle -----------------------------------------------------
    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self._in_transaction:
                self.conn.execute("ROLLBACK")
        except sqlite3.ProgramError:
            pass
        self.conn.close()
        if self._own_dir:
            shutil.rmtree(self.directory, ignore_errors=True)
        else:
            try:
                os.unlink(self.db_path)
            except OSError:
                pass
            if self._created_dir:
                try:
                    os.rmdir(self.directory)
                except OSError:
                    pass


def _cursor_iter(cursor: sqlite3.Cursor):
    """Yield one fresh dict per row (sqlite3.Row objects are reused)."""
    while True:
        row = cursor.fetchone()
        if row is None:
            return
        yield dict(row)
