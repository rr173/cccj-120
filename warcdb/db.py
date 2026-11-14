"""SQLite fact store for parsed captures.

One independent file holds, per fetch (WARC record): identity, URI,
instant, HTTP status and headers, the reference edge, the resolution
status, and -- for resolved fetches -- the body source archive_id plus the
exact byte range of the body inside the original WARC stream.  Body bytes
themselves are never copied here, and no KEK/DEK material is ever stored.

Import atomicity: all records of one archive are validated by the caller
first; this module then inserts them (and re-resolves affected fetches) in
a single transaction.  A crash before COMMIT leaves no trace of the
archive; re-importing the same archive afterwards is a clean no-op
confirmation.

Record-ID uniqueness: a record whose Record-ID and whole-record SHA-256
both match an existing row is a duplicate (fetch count does not grow);
the same Record-ID with different bytes is a conflict and rejects the
whole import.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from .errors import ImportConflictError, RecordNotFoundError
from .warc import HeaderField, ParsedRecord

PathLike = Union[str, "Path"]

STATUS_RESOLVED = "resolved"
STATUS_PENDING = "pending"
STATUS_ERROR = "error"

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    archive_id    TEXT PRIMARY KEY,      -- archive_id hex from archivestore
    imported_utc  TEXT NOT NULL,
    records_total INTEGER NOT NULL,      -- records seen in the archive
    new_fetches   INTEGER NOT NULL       -- rows actually added (rest: duplicates)
);
CREATE TABLE IF NOT EXISTS fetches (
    record_id         TEXT PRIMARY KEY,  -- WARC-Record-ID, verbatim
    archive_id        TEXT NOT NULL,     -- archive where first imported
    record_seq        INTEGER NOT NULL,  -- 0-based position in that archive
    record_offset     INTEGER NOT NULL,
    record_length     INTEGER NOT NULL,
    record_sha256     TEXT NOT NULL,     -- sha256 over the whole record bytes
    record_type       TEXT NOT NULL,     -- 'response' | 'revisit'
    target_uri        TEXT NOT NULL,     -- WARC-Target-URI, verbatim
    date_utc          TEXT NOT NULL,     -- normalised ISO-8601 UTC
    date_us           INTEGER NOT NULL,  -- the same instant, epoch microseconds
    http_version      TEXT NOT NULL,
    http_status       INTEGER NOT NULL,
    http_reason       TEXT NOT NULL,
    declared_body_len INTEGER NOT NULL,  -- HTTP Content-Length of this fetch
    block_digest      TEXT NOT NULL,     -- verified WARC-Block-Digest (hex)
    payload_digest    TEXT NOT NULL,     -- WARC-Payload-Digest (hex)
    refers_to         TEXT,              -- WARC-Refers-To (revisit only)
    body_archive_id   TEXT,              -- response: archive holding the body
    body_offset       INTEGER,           -- response: body range in its WARC
    body_length       INTEGER,
    status            TEXT NOT NULL,     -- 'resolved' | 'pending' | 'error'
    status_detail     TEXT NOT NULL DEFAULT '',
    endpoint_id       TEXT,              -- resolved terminal record_id
    source_archive_id TEXT,              -- resolved: where the body lives
    source_offset     INTEGER,           -- resolved: exact body byte range
    source_length     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fetches_uri_time
    ON fetches(target_uri, date_us);
CREATE TABLE IF NOT EXISTS fetch_headers (
    record_id TEXT NOT NULL REFERENCES fetches(record_id) ON DELETE CASCADE,
    seq       INTEGER NOT NULL,          -- header order within the response
    name      TEXT NOT NULL,             -- original case
    value     TEXT NOT NULL,             -- original value (OWS-trimmed)
    PRIMARY KEY (record_id, seq)
);
"""


@dataclass(frozen=True)
class Fetch:
    record_id: str
    archive_id: str
    record_seq: int
    record_offset: int
    record_length: int
    record_sha256: str
    record_type: str
    target_uri: str
    date_utc: str
    date_us: int
    http_version: str
    http_status: int
    http_reason: str
    declared_body_len: int
    block_digest: str
    payload_digest: str
    refers_to: Optional[str]
    body_archive_id: Optional[str]
    body_offset: Optional[int]
    body_length: Optional[int]
    status: str
    status_detail: str
    endpoint_id: Optional[str]
    source_archive_id: Optional[str]
    source_offset: Optional[int]
    source_length: Optional[int]
    headers: tuple = ()          # tuple[HeaderField, ...], loaded on demand

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__ if k != "headers"}
        d["http_headers"] = [{"name": h.name, "value": h.value} for h in self.headers]
        return d


@dataclass(frozen=True)
class ImportResult:
    archive_id: str
    records_total: int
    new_fetches: int
    duplicates: int
    already_imported: bool
    resolved: int                  # global status counts after the import
    pending: int
    error: int

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


_FETCH_COLUMNS = (
    "record_id, archive_id, record_seq, record_offset, record_length,"
    " record_sha256, record_type, target_uri, date_utc, date_us,"
    " http_version, http_status, http_reason, declared_body_len,"
    " block_digest, payload_digest, refers_to, body_archive_id,"
    " body_offset, body_length, status, status_detail, endpoint_id,"
    " source_archive_id, source_offset, source_length"
)


class FactDB:
    """One open view of a fact-store file.  Not thread-safe."""

    def __init__(self, path: PathLike):
        self._path = Path(path)
        self._con = sqlite3.connect(str(self._path), isolation_level=None)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=FULL")
        self._con.execute("PRAGMA foreign_keys=ON")
        self._con.execute("PRAGMA busy_timeout=10000")
        self._con.executescript(SCHEMA)

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> "FactDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ rows

    def _row_to_fetch(self, row) -> Fetch:
        return Fetch(*row)

    def _headers_for(self, record_id: str) -> tuple:
        return tuple(
            HeaderField(name, value)
            for name, value in self._con.execute(
                "SELECT name, value FROM fetch_headers"
                " WHERE record_id = ? ORDER BY seq",
                (record_id,),
            )
        )

    def get(self, record_id: str, *, with_headers: bool = True) -> Optional[Fetch]:
        row = self._con.execute(
            f"SELECT {_FETCH_COLUMNS} FROM fetches WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            return None
        fetch = self._row_to_fetch(row)
        if with_headers:
            fetch = Fetch(*row, self._headers_for(record_id))
        return fetch

    # ----------------------------------------------------------------- import

    def import_records(self, archive_id: str, records: list,
                       pause_hook=None) -> ImportResult:
        """Insert validated records of one archive in a single transaction.

        Duplicates (same Record-ID, same whole-record digest) are skipped;
        a Record-ID with different bytes rejects the whole import.  After
        inserting, every not-yet-resolved fetch is re-resolved, so late
        arrivals complete earlier pending chains while resolved rows are
        left untouched.
        """
        self._con.execute("BEGIN IMMEDIATE")
        try:
            prior = self._con.execute(
                "SELECT records_total FROM imports WHERE archive_id = ?",
                (archive_id,),
            ).fetchone()

            new_records = []
            duplicates = 0
            seen_in_batch: dict = {}
            for seq, rec in enumerate(records):
                prev = seen_in_batch.get(rec.record_id)
                if prev is not None:
                    if prev.record_sha256 == rec.record_sha256:
                        duplicates += 1
                        continue
                    raise ImportConflictError(
                        f"archive {archive_id} contains conflicting records "
                        f"with Record-ID {rec.record_id!r}"
                    )
                seen_in_batch[rec.record_id] = rec
                row = self._con.execute(
                    "SELECT record_sha256 FROM fetches WHERE record_id = ?",
                    (rec.record_id,),
                ).fetchone()
                if row is None:
                    new_records.append((seq, rec))
                elif row[0] == rec.record_sha256:
                    duplicates += 1
                else:
                    raise ImportConflictError(
                        f"record {rec.record_id!r} from archive {archive_id} "
                        f"conflicts with the different record already stored "
                        f"(whole-record sha256 {row[0]}); refusing the import"
                    )

            if prior is not None:
                # Re-import of a known archive is only a confirmation: the
                # content must be exactly what was imported before.
                if new_records or prior[0] != len(records):
                    raise ImportConflictError(
                        f"archive {archive_id} was already imported with "
                        f"{prior[0]} record(s); this import has "
                        f"{len(records)} record(s) including "
                        f"{len(new_records)} unseen one(s); refusing"
                    )
                self._con.execute("COMMIT")
                counts = self._status_counts()
                return ImportResult(
                    archive_id=archive_id, records_total=len(records),
                    new_fetches=0, duplicates=duplicates, already_imported=True,
                    **counts,
                )

            now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            for seq, rec in new_records:
                self._con.execute(
                    f"INSERT INTO fetches({_FETCH_COLUMNS})"
                    f" VALUES({','.join('?' * 26)})",
                    (
                        rec.record_id, archive_id, seq, rec.record_offset,
                        rec.record_length, rec.record_sha256, rec.record_type,
                        rec.target_uri, rec.date_utc, rec.date_us,
                        rec.http.version, rec.http.status, rec.http.reason,
                        rec.http.content_length, rec.block_digest,
                        rec.payload_digest, rec.refers_to,
                        archive_id if rec.record_type == "response" else None,
                        rec.body_offset, rec.body_length,
                        STATUS_PENDING, "", None, None, None, None,
                    ),
                )
                for hseq, h in enumerate(rec.http.headers):
                    self._con.execute(
                        "INSERT INTO fetch_headers(record_id, seq, name, value)"
                        " VALUES(?,?,?,?)",
                        (rec.record_id, hseq, h.name, h.value),
                    )
            self._con.execute(
                "INSERT INTO imports(archive_id, imported_utc, records_total,"
                " new_fetches) VALUES(?,?,?,?)",
                (archive_id, now, len(records), len(new_records)),
            )
            self._resolve_open_records()
            if pause_hook is not None:
                pause_hook("pre_commit")
            self._con.execute("COMMIT")
        except Exception:
            self._con.execute("ROLLBACK")
            raise
        counts = self._status_counts()
        return ImportResult(
            archive_id=archive_id, records_total=len(records),
            new_fetches=len(new_records), duplicates=duplicates,
            already_imported=False, **counts,
        )

    # ------------------------------------------------------------- resolution

    def _resolve_open_records(self) -> None:
        """Re-resolve every fetch that is not currently resolved.

        Resolved fetches never change: records are immutable and reference
        edges point at exact Record-IDs, so new imports cannot alter a
        completed chain.
        """
        open_ids = [
            r[0]
            for r in self._con.execute(
                "SELECT record_id FROM fetches WHERE status != ?"
                " ORDER BY record_id",
                (STATUS_RESOLVED,),
            )
        ]
        for rid in open_ids:
            status, detail, endpoint, src_aid, src_off, src_len = self._resolve(rid)
            self._con.execute(
                "UPDATE fetches SET status=?, status_detail=?, endpoint_id=?,"
                " source_archive_id=?, source_offset=?, source_length=?"
                " WHERE record_id=?",
                (status, detail, endpoint, src_aid, src_off, src_len, rid),
            )

    def _resolve(self, record_id: str):
        """Walk the reference chain of one fetch to its endpoint."""
        top = self.get(record_id, with_headers=False)
        seen = {record_id}
        chain = [record_id]
        cur = top
        while cur.record_type == "revisit":
            tgt_id = cur.refers_to
            if tgt_id in seen:
                cycle = " -> ".join(chain + [tgt_id])
                return (STATUS_ERROR,
                        f"reference cycle detected: {cycle}",
                        None, None, None, None)
            tgt = self.get(tgt_id, with_headers=False)
            if tgt is None:
                return (STATUS_PENDING,
                        f"refers-to target {tgt_id!r} is not in the fact store",
                        None, None, None, None)
            seen.add(tgt_id)
            chain.append(tgt_id)
            cur = tgt
        # cur is the terminal response record.
        if top.payload_digest != cur.payload_digest:
            return (STATUS_ERROR,
                    f"payload digest mismatch with endpoint "
                    f"{cur.record_id!r}: declared {top.payload_digest}, "
                    f"endpoint {cur.payload_digest}",
                    None, None, None, None)
        if top.declared_body_len != cur.body_length:
            return (STATUS_ERROR,
                    f"declared body length {top.declared_body_len} != "
                    f"endpoint body length {cur.body_length} "
                    f"(endpoint {cur.record_id!r})",
                    None, None, None, None)
        return (STATUS_RESOLVED, "", cur.record_id, cur.body_archive_id,
                cur.body_offset, cur.body_length)

    # ---------------------------------------------------------------- queries

    def chain(self, record_id: str) -> dict:
        """Full reference chain of one Record-ID, as stored today."""
        top = self.get(record_id)
        if top is None:
            raise RecordNotFoundError(f"no fetch with Record-ID {record_id!r}")
        hops = []
        seen = set()
        cur = top
        terminal = None
        while True:
            hops.append(cur.to_dict())
            if cur.record_type != "revisit":
                terminal = {"kind": "response", "record_id": cur.record_id}
                break
            tgt_id = cur.refers_to
            if tgt_id in seen:
                terminal = {"kind": "cycle", "record_id": tgt_id}
                break
            seen.add(cur.record_id)
            nxt = self.get(tgt_id)
            if nxt is None:
                terminal = {"kind": "missing", "record_id": tgt_id}
                break
            cur = nxt
        return {
            "record_id": record_id,
            "status": top.status,
            "status_detail": top.status_detail,
            "endpoint_id": top.endpoint_id,
            "hops": hops,
            "terminal": terminal,
            "source": self._source_dict(top),
        }

    @staticmethod
    def _source_dict(fetch: Fetch) -> Optional[dict]:
        if fetch.status != STATUS_RESOLVED:
            return None
        return {
            "archive_id": fetch.source_archive_id,
            "offset": fetch.source_offset,
            "length": fetch.source_length,
        }

    def lookup(self, target_uri: str, cutoff_us: int) -> dict:
        """Latest fetch of `target_uri` not later than `cutoff_us`.

        Selection rule (documented in WARCDB.md): highest date_us wins;
        ties are broken by the lexicographically greatest Record-ID
        (bytewise comparison of the raw string).  A pending or errored
        latest fetch is reported as-is; no earlier fetch is substituted.
        """
        row = self._con.execute(
            f"SELECT {_FETCH_COLUMNS} FROM fetches"
            f" WHERE target_uri = ? AND date_us <= ?"
            f" ORDER BY date_us DESC, record_id DESC LIMIT 1",
            (target_uri, cutoff_us),
        ).fetchone()
        if row is None:
            return {"found": False, "target_uri": target_uri}
        fetch = self._row_to_fetch(row)
        fetch = Fetch(*row, self._headers_for(fetch.record_id))
        return {
            "found": True,
            "target_uri": target_uri,
            "fetch": fetch.to_dict(),
            "source": self._source_dict(fetch),
        }

    def stats(self) -> dict:
        counts = self._status_counts()
        total = sum(counts.values())
        imports = self._con.execute(
            "SELECT COUNT(*), COALESCE(SUM(new_fetches), 0) FROM imports"
        ).fetchone()
        return {
            "fetches": total,
            "by_status": counts,
            "imports": imports[0],
            "imported_new_fetches": imports[1],
        }

    def _status_counts(self) -> dict:
        counts = {STATUS_RESOLVED: 0, STATUS_PENDING: 0, STATUS_ERROR: 0}
        for status, n in self._con.execute(
            "SELECT status, COUNT(*) FROM fetches GROUP BY status"
        ):
            counts[status] = n
        return counts

    def list_fetches(self, target_uri: Optional[str] = None) -> list:
        sql = f"SELECT {_FETCH_COLUMNS} FROM fetches"
        args = ()
        if target_uri is not None:
            sql += " WHERE target_uri = ?"
            args = (target_uri,)
        sql += " ORDER BY date_us, record_id"
        return [self._row_to_fetch(r).to_dict()
                for r in self._con.execute(sql, args)]

    def dump(self) -> dict:
        """Deterministic full-content dump (for cross-database comparison)."""
        fetches = [
            tuple(r)
            for r in self._con.execute(
                f"SELECT {_FETCH_COLUMNS} FROM fetches ORDER BY record_id"
            )
        ]
        headers = [
            tuple(r)
            for r in self._con.execute(
                "SELECT record_id, seq, name, value FROM fetch_headers"
                " ORDER BY record_id, seq"
            )
        ]
        imports = [
            (r[0], r[2], r[3])  # skip the wall-clock imported_utc
            for r in self._con.execute(
                "SELECT archive_id, imported_utc, records_total, new_fetches"
                " FROM imports ORDER BY archive_id"
            )
        ]
        return {"fetches": fetches, "fetch_headers": headers, "imports": imports}
