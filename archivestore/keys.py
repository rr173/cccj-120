"""External key_id -> key-file mapping.

The process is handed a mapping from key_id to the path of a key file that
lives OUTSIDE the storage directory.  A key file contains exactly 32 raw
bytes (one AES-256 KEK).  Key bytes are never logged and never appear in
error messages.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Mapping, Union

from .errors import KeyFormatError, KeyUnavailableError
from .layout import KEK_LEN

PathLike = Union[str, os.PathLike]


class KeyFileProvider:
    """Resolves key_id -> 32-byte KEK by reading the mapped key file."""

    def __init__(self, mapping: Mapping[str, PathLike]):
        self._map = {str(k): Path(v) for k, v in mapping.items()}

    def key_ids(self) -> list[str]:
        return sorted(self._map)

    def path_for(self, key_id: str) -> Path:
        try:
            return self._map[key_id]
        except KeyError:
            raise KeyUnavailableError(
                f"no key file registered for key_id {key_id!r}"
            ) from None

    def load(self, key_id: str) -> bytes:
        path = self.path_for(key_id)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise KeyUnavailableError(
                f"cannot read key file for key_id {key_id!r} at {path}: {exc.strerror or exc}"
            ) from None
        if len(data) != KEK_LEN:
            raise KeyFormatError(
                f"key file for key_id {key_id!r} at {path} has {len(data)} bytes, "
                f"expected exactly {KEK_LEN}"
            )
        return data


def generate_key_file(path: PathLike) -> None:
    """Create a fresh random 256-bit key file (mode 0600, must not exist)."""
    p = Path(path)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, os.urandom(KEK_LEN))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
