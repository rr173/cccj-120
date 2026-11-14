"""Exception hierarchy for warcdb (offline capture fact store).

Error messages may contain record ids, URIs, offsets, sizes and digest
strings.  They never contain key material (the fact store never sees KEKs
or DEKs itself; decryption happens inside archivestore).

Diagnostics are deliberately specific, per the format contract:

    WarcTruncatedError          truncated byte stream (unexpected EOF)
    WarcFormatError             malformed structure, illegal Content-Length
                                value, duplicate identity/boundary fields,
                                bad date, bad digest syntax, bad separator
    WarcLimitError              a configured size limit was exceeded
    UnsupportedRecordTypeError  WARC-Type other than response/revisit
    UnsupportedDigestError      digest algorithm other than sha256
    HttpFormatError             HTTP-level problems (bad status line,
                                unsupported version, chunked transfer,
                                content compression, length mismatch, ...)
    DigestMismatchError         declared WARC digest != computed digest
    ImportConflictError         same Record-ID with different bytes, or an
                                archive re-imported with different content
    RecordNotFoundError         unknown Record-ID in a query/export
    FetchNotResolvedError       export requested for a non-resolved fetch
    ExportVerificationError     exported bytes failed the length/digest check
"""


class WarcDbError(Exception):
    """Base class for all warcdb errors."""


class WarcTruncatedError(WarcDbError):
    """The byte stream ended before the current record was complete."""


class WarcFormatError(WarcDbError):
    """A WARC record is malformed (structure, fields, lengths, digests)."""


class WarcLimitError(WarcDbError):
    """A configured limit (header size, block size) was exceeded."""


class UnsupportedRecordTypeError(WarcDbError):
    """WARC-Type is neither 'response' nor 'revisit'."""


class UnsupportedDigestError(WarcDbError):
    """A WARC digest field uses an algorithm other than sha256."""


class HttpFormatError(WarcDbError):
    """The HTTP message inside a content block is invalid or unsupported."""


class DigestMismatchError(WarcDbError):
    """A declared WARC digest does not match the computed digest."""


class ImportConflictError(WarcDbError):
    """Import conflicts with facts already stored; nothing was changed."""


class RecordNotFoundError(WarcDbError):
    """The requested Record-ID is not present in the fact store."""


class FetchNotResolvedError(WarcDbError):
    """The fetch is pending or in error; its body cannot be exported."""


class ExportVerificationError(WarcDbError):
    """Bytes read back from the source archive failed verification."""
