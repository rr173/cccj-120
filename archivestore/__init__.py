"""archivestore: local encrypted archive storage.

    from archivestore import ArchiveStore, KeyFileProvider, generate_key_file

    generate_key_file("keys/k1.key")            # done once, outside storage dir
    store = ArchiveStore("storage", {"k1": "keys/k1.key"})
    aid = store.seal("capture.bin", key_id="k1", block_size=1 << 20)
    store.read_range(aid, 0, 4096, "out.bin")
    store.verify(aid)
    store.rotate(old_key_id="k1", new_key_id="k2")
"""

from .errors import (
    ArchiveNotFoundError,
    ArchiveStoreError,
    AuthenticationError,
    BlobStateError,
    DescriptorError,
    KeyFormatError,
    KeyUnavailableError,
    RangeError,
    RotationError,
    SourceChangedError,
    TargetExistsError,
)
from .keys import KeyFileProvider, generate_key_file
from .store import ArchiveHandle, ArchiveStore, ReadStats, VerifyStats

__version__ = "1.0.0"

__all__ = [
    "ArchiveStore",
    "ArchiveHandle",
    "KeyFileProvider",
    "generate_key_file",
    "ReadStats",
    "VerifyStats",
    "ArchiveStoreError",
    "ArchiveNotFoundError",
    "AuthenticationError",
    "BlobStateError",
    "DescriptorError",
    "KeyFormatError",
    "KeyUnavailableError",
    "RangeError",
    "RotationError",
    "SourceChangedError",
    "TargetExistsError",
    "__version__",
]
