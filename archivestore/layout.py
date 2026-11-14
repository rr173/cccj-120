"""Binary layout constants and (de)serialisation for archivestore.

Everything is big-endian and fixed-length so the encoding is unambiguous.

Archive descriptor (exactly 78 bytes), stored in the catalogue and used as
AAD for the block-0 authentication record:

    offset  size  field
    0       4     magic  b"ARC1"
    4       2     format version (1)
    6       16    archive_id (random)
    22      8     original length in bytes (orig_len)
    30      4     block size B (plaintext bytes per content block)
    34      8     content block count N == ceil(orig_len / B)
    42      4     nonce prefix (random per archive)
    46      32    SHA-256 of the original plaintext

Content block i (1-based, i = 1..N) is stored in the blob file as
    ciphertext(B or fewer bytes) || 16-byte GCM tag
at offset (i-1) * (B + 16).  The GCM nonce is prefix || i (8-byte BE);
index 0 is reserved for the descriptor authentication record and is never
used for content.  The AAD of content block i is
    b"ARCB1" || SHA256(descriptor) || i (8-byte BE) || plen_i (4-byte BE)
which binds the block to the whole descriptor, its position, and its true
plaintext length (so a short tail block cannot be replaced by a full block
or vice versa).

The wrapped DEK record (exactly 60 bytes) is
    12-byte random nonce || AES-256-GCM(KEK, DEK) ciphertext || 16-byte tag
with AAD
    b"ARCW1" || archive_id || SHA256(descriptor) || len(key_id) (2-byte BE) || key_id
which binds the envelope to the archive, the exact descriptor, and the
key_id it was wrapped for.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

MAGIC = b"ARC1"
VERSION = 1

DESC_STRUCT = struct.Struct(">4sH16sQIQ4s32s")
DESCRIPTOR_LEN = DESC_STRUCT.size  # 78

TAG_LEN = 16
DEK_LEN = 32
KEK_LEN = 32
ARCHIVE_ID_LEN = 16
NONCE_PREFIX_LEN = 4
BLOCK_INDEX_LEN = 8
GCM_NONCE_LEN = NONCE_PREFIX_LEN + BLOCK_INDEX_LEN  # 12, as required by AES-GCM
WRAP_NONCE_LEN = 12
WRAPPED_DEK_LEN = WRAP_NONCE_LEN + DEK_LEN + TAG_LEN  # 60
SHA256_LEN = 32

DESC_AUTH_INDEX = 0       # nonce index reserved for the descriptor record
FIRST_CONTENT_INDEX = 1   # content blocks are numbered from 1

MIN_BLOCK_SIZE = 16
MAX_BLOCK_SIZE = 1 << 30  # plen must fit the 4-byte AAD field; keep far below
MAX_KEY_ID_BYTES = 0xFFFF

_AAD_CONTENT_DOMAIN = b"ARCB1"
_AAD_ENVELOPE_DOMAIN = b"ARCW1"


def block_count_for(orig_len: int, block_size: int) -> int:
    if orig_len < 0:
        raise ValueError("orig_len must be >= 0")
    if block_size <= 0:
        raise ValueError("block_size must be > 0")
    if orig_len == 0:
        return 0
    return (orig_len + block_size - 1) // block_size


@dataclass(frozen=True)
class Descriptor:
    archive_id: bytes
    orig_len: int
    block_size: int
    block_count: int
    nonce_prefix: bytes
    sha256: bytes
    raw: bytes  # canonical 78-byte encoding, as stored

    @property
    def digest(self) -> bytes:
        """SHA-256 of the canonical descriptor encoding."""
        return hashlib.sha256(self.raw).digest()

    @property
    def archive_id_hex(self) -> str:
        return self.archive_id.hex()

    def expected_blob_size(self) -> int:
        """Exact byte size the ciphertext blob must have."""
        if self.block_count == 0:
            return 0
        last = self.orig_len - (self.block_count - 1) * self.block_size
        return (self.block_count - 1) * (self.block_size + TAG_LEN) + last + TAG_LEN

    def block_plain_len(self, index: int) -> int:
        """Plaintext length of 1-based content block `index`."""
        if not 1 <= index <= self.block_count:
            raise ValueError(f"block index {index} out of range 1..{self.block_count}")
        if index < self.block_count:
            return self.block_size
        return self.orig_len - (self.block_count - 1) * self.block_size

    def block_span(self, index: int) -> tuple[int, int]:
        """(offset, stored length incl. tag) of 1-based block `index` in the blob."""
        plen = self.block_plain_len(index)
        offset = (index - 1) * (self.block_size + TAG_LEN)
        return offset, plen + TAG_LEN


def _validate_params(orig_len: int, block_size: int, block_count: int) -> None:
    from .errors import DescriptorError

    if not (MIN_BLOCK_SIZE <= block_size <= MAX_BLOCK_SIZE):
        raise DescriptorError(
            f"block size {block_size} outside [{MIN_BLOCK_SIZE}, {MAX_BLOCK_SIZE}]"
        )
    expected = block_count_for(orig_len, block_size)
    if block_count != expected:
        raise DescriptorError(
            f"block count {block_count} inconsistent with orig_len={orig_len} "
            f"and block_size={block_size} (expected {expected})"
        )


def encode_descriptor(
    *,
    archive_id: bytes,
    orig_len: int,
    block_size: int,
    block_count: int,
    nonce_prefix: bytes,
    sha256: bytes,
) -> bytes:
    if len(archive_id) != ARCHIVE_ID_LEN:
        raise ValueError("archive_id must be 16 bytes")
    if len(nonce_prefix) != NONCE_PREFIX_LEN:
        raise ValueError("nonce_prefix must be 4 bytes")
    if len(sha256) != SHA256_LEN:
        raise ValueError("sha256 must be 32 bytes")
    if orig_len < 0:
        raise ValueError("orig_len must be >= 0")
    _validate_params(orig_len, block_size, block_count)
    return DESC_STRUCT.pack(
        MAGIC, VERSION, archive_id, orig_len, block_size, block_count, nonce_prefix, sha256
    )


def decode_descriptor(buf: bytes) -> Descriptor:
    from .errors import DescriptorError

    if len(buf) != DESCRIPTOR_LEN:
        raise DescriptorError(
            f"descriptor is {len(buf)} bytes, expected exactly {DESCRIPTOR_LEN}"
        )
    magic, version, aid, olen, bsz, cnt, prefix, digest = DESC_STRUCT.unpack(buf)
    if magic != MAGIC:
        raise DescriptorError("bad descriptor magic")
    if version != VERSION:
        raise DescriptorError(f"unsupported descriptor version {version}")
    _validate_params(olen, bsz, cnt)
    return Descriptor(aid, olen, bsz, cnt, prefix, digest, bytes(buf))


def content_nonce(nonce_prefix: bytes, index: int) -> bytes:
    """12-byte GCM nonce: 4-byte archive prefix || 8-byte big-endian index."""
    if len(nonce_prefix) != NONCE_PREFIX_LEN:
        raise ValueError("nonce_prefix must be 4 bytes")
    if not 0 <= index < 1 << 64:
        raise ValueError("block index out of range")
    return nonce_prefix + index.to_bytes(BLOCK_INDEX_LEN, "big")


def content_aad(desc_digest: bytes, index: int, plain_len: int) -> bytes:
    """AAD binding a content block to the descriptor, its index and true length."""
    if len(desc_digest) != SHA256_LEN:
        raise ValueError("desc_digest must be 32 bytes")
    if not 0 <= plain_len <= MAX_BLOCK_SIZE:
        raise ValueError("plain_len out of range")
    return (
        _AAD_CONTENT_DOMAIN
        + desc_digest
        + index.to_bytes(BLOCK_INDEX_LEN, "big")
        + plain_len.to_bytes(4, "big")
    )


def envelope_aad(archive_id: bytes, desc_digest: bytes, key_id: str) -> bytes:
    """AAD binding a wrapped DEK to archive, descriptor digest and key_id."""
    if len(archive_id) != ARCHIVE_ID_LEN:
        raise ValueError("archive_id must be 16 bytes")
    if len(desc_digest) != SHA256_LEN:
        raise ValueError("desc_digest must be 32 bytes")
    kb = key_id.encode("utf-8")
    if len(kb) > MAX_KEY_ID_BYTES:
        raise ValueError("key_id too long")
    return _AAD_ENVELOPE_DOMAIN + archive_id + desc_digest + len(kb).to_bytes(2, "big") + kb
