"""Sequential plaintext view over an encrypted archive.

Wraps ArchiveHandle.iter_blocks() so the WARC parser can read the archive
as a plain byte stream: ciphertext is decrypted and authenticated block by
block, never written to a permanent plaintext file, and at most one
plaintext block is held in memory at a time.  Long bodies simply span
multiple encrypted blocks.
"""

from __future__ import annotations


class ArchivePlaintextStream:
    """read(n)/tell() interface over an ArchiveHandle's plaintext."""

    def __init__(self, handle):
        self._handle = handle
        self._blocks = handle.iter_blocks()
        self._blk = b""
        self._blk_off = 0
        self._pos = 0
        self._len = handle.length
        self._done = self._len == 0

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self._len - self._pos
        if n == 0:
            return b""
        chunks = []
        need = n
        while need > 0 and not self._done:
            if self._blk_off == len(self._blk):
                try:
                    _, self._blk = next(self._blocks)
                except StopIteration:
                    self._done = True
                    break
                self._blk_off = 0
            take = min(need, len(self._blk) - self._blk_off)
            chunks.append(self._blk[self._blk_off:self._blk_off + take])
            self._blk_off += take
            need -= take
        data = b"".join(chunks)
        self._pos += len(data)
        return data

    def tell(self) -> int:
        return self._pos

    def close(self) -> None:
        self._blocks.close()
        self._blk = b""
        self._done = True

    def __enter__(self) -> "ArchivePlaintextStream":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
