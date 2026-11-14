"""warcdb: offline capture fact store on top of archivestore.

    from archivestore import ArchiveStore
    from warcdb import FactDB, FactStore, Limits

    store = ArchiveStore("storage", {"k1": "keys/k1.key"})
    db = FactDB("facts.db")
    facts = FactStore(store, db)

    result = facts.import_archive(archive_id)          # all-or-nothing
    info = db.chain("<urn:uuid:...>")                  # reference chain
    hit = db.lookup("http://example/", cutoff_us)      # latest <= instant
    facts.export_body(record_id, "body.bin")           # verified export

The fact store holds metadata and byte ranges only: no body bytes, and no
KEK/DEK material.  See WARCDB.md for the format contract and semantics.
"""

from .core import ExportResult, FactStore
from .db import FactDB, Fetch, ImportResult
from .errors import (
    DigestMismatchError,
    ExportVerificationError,
    FetchNotResolvedError,
    HttpFormatError,
    ImportConflictError,
    RecordNotFoundError,
    UnsupportedDigestError,
    UnsupportedRecordTypeError,
    WarcDbError,
    WarcFormatError,
    WarcLimitError,
    WarcTruncatedError,
)
from .warc import Limits, ParsedRecord, WarcParser

__version__ = "1.0.0"

__all__ = [
    "FactStore",
    "FactDB",
    "Fetch",
    "ImportResult",
    "ExportResult",
    "Limits",
    "WarcParser",
    "ParsedRecord",
    "WarcDbError",
    "WarcTruncatedError",
    "WarcFormatError",
    "WarcLimitError",
    "UnsupportedRecordTypeError",
    "UnsupportedDigestError",
    "HttpFormatError",
    "DigestMismatchError",
    "ImportConflictError",
    "RecordNotFoundError",
    "FetchNotResolvedError",
    "ExportVerificationError",
    "__version__",
]
