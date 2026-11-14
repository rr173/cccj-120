"""Exception hierarchy for archivestore.

Error messages must never contain key material (KEK/DEK bytes).  They may
contain key_id strings, archive ids, paths and sizes.
"""


class ArchiveStoreError(Exception):
    """Base class for all archivestore errors."""


class ArchiveNotFoundError(ArchiveStoreError):
    """The requested archive_id is not registered in the catalogue."""


class DescriptorError(ArchiveStoreError):
    """A descriptor is malformed, inconsistent, or fails authentication."""


class AuthenticationError(ArchiveStoreError):
    """A GCM tag check failed (wrong key, tampered ciphertext/descriptor/
    envelope, swapped or reordered blocks, ...)."""


class KeyUnavailableError(ArchiveStoreError):
    """No usable key file is registered for the required key_id."""


class KeyFormatError(ArchiveStoreError):
    """A key file exists but does not contain exactly one 256-bit key."""


class RangeError(ArchiveStoreError, ValueError):
    """A requested [start, end) range is not valid for the archive."""


class RotationError(ArchiveStoreError):
    """A key rotation was rejected; the catalogue was left unchanged."""


class TargetExistsError(ArchiveStoreError, FileExistsError):
    """The requested output target already exists; nothing was written."""


class BlobStateError(ArchiveStoreError):
    """The ciphertext blob is missing, truncated, or has trailing garbage."""


class SourceChangedError(ArchiveStoreError):
    """The source file changed while it was being sealed."""
