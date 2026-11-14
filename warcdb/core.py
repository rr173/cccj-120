"""FactStore: ties the encrypted archive store and the fact DB together.

  * import_archive(archive_id): stream-decrypt the archive block by block
    (no permanent plaintext file, no large body in memory), parse and
    validate every WARC record, then commit all facts in one transaction.
  * export_body(record_id, out): follow the confirmed source range of a
    resolved fetch through archivestore's read capability, verify the
    length and payload digest of the bytes read back, and atomically put
    the result in place.  A failed export leaves no complete result and
    never overwrites an existing file.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from archivestore.errors import TargetExistsError
from archivestore.store import ArchiveStore

from .db import FactDB, ImportResult, STATUS_RESOLVED
from .errors import (
    ExportVerificationError,
    FetchNotResolvedError,
    RecordNotFoundError,
)
from .stream import ArchivePlaintextStream
from .warc import Limits, WarcParser

PathLike = Union[str, os.PathLike]

_EXPORT_CHUNK = 1 << 20


@dataclass(frozen=True)
class ExportResult:
    record_id: str
    source_archive_id: str
    source_offset: int
    source_length: int
    payload_sha256: str
    bytes_written: int
    out: str

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


class FactStore:
    """Import from an ArchiveStore into a FactDB; export bodies back out."""

    def __init__(self, store: ArchiveStore, db: FactDB):
        self._store = store
        self._db = db

    @property
    def db(self) -> FactDB:
        return self._db

    def import_archive(self, archive_id: str, *, limits: Limits = Limits(),
                       pause_hook=None) -> ImportResult:
        """Validate every record of `archive_id`, then commit them together.

        Any parse/validation failure aborts the import before the
        transaction starts; a crash before the commit leaves no facts
        behind.  Re-importing the same archive is an idempotent
        confirmation.
        """
        with self._store.open(archive_id) as handle:
            with ArchivePlaintextStream(handle) as stream:
                parser = WarcParser(stream, limits=limits)
                records = list(parser.records())
        return self._db.import_records(archive_id, records, pause_hook=pause_hook)

    def export_body(self, record_id: str, out_path: PathLike) -> ExportResult:
        """Export the resolved body of one fetch to a new file.

        The bytes are read from the confirmed source range of the source
        archive (each touched ciphertext block is authenticated by
        archivestore), then checked against the stored body length and
        payload digest before the result is linked into place.  Any
        failure -- unresolved fetch, authentication error, length/digest
        mismatch -- removes the scratch file and leaves no result.
        """
        fetch = self._db.get(record_id)
        if fetch is None:
            raise RecordNotFoundError(f"no fetch with Record-ID {record_id!r}")
        if fetch.status != STATUS_RESOLVED:
            raise FetchNotResolvedError(
                f"fetch {record_id!r} is {fetch.status} "
                f"({fetch.status_detail or 'not resolved'}); refusing export"
            )
        out = Path(out_path)
        tmp = out.parent / f".export-{os.getpid()}-{secrets.token_hex(8)}.part"
        try:
            self._store.read_range(
                fetch.source_archive_id,
                fetch.source_offset,
                fetch.source_offset + fetch.source_length,
                tmp,
            )
            h = hashlib.sha256()
            total = 0
            with open(tmp, "rb") as f:
                while True:
                    chunk = f.read(_EXPORT_CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
                    total += len(chunk)
            if total != fetch.source_length:
                raise ExportVerificationError(
                    f"exported {total} byte(s), expected {fetch.source_length} "
                    f"for fetch {record_id!r}"
                )
            if h.hexdigest() != fetch.payload_digest:
                raise ExportVerificationError(
                    f"exported payload sha256 {h.hexdigest()} != stored "
                    f"digest {fetch.payload_digest} for fetch {record_id!r}"
                )
            try:
                os.link(tmp, out)       # atomic; fails if `out` exists
            except FileExistsError:
                raise TargetExistsError(
                    f"output target already exists: {out}"
                ) from None
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return ExportResult(
            record_id=record_id,
            source_archive_id=fetch.source_archive_id,
            source_offset=fetch.source_offset,
            source_length=fetch.source_length,
            payload_sha256=fetch.payload_digest,
            bytes_written=total,
            out=str(out),
        )
