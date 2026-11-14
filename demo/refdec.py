#!/usr/bin/env python3
"""Independent reference decryptor.

Deliberately does NOT import archivestore: the on-disk format is re-derived
here from the documented layout alone, so that this script cross-checks the
format specification rather than the library's own code.  It unwraps the DEK
with the given KEK file, authenticates the descriptor, decrypts every block
in order, checks every tag and the whole-archive SHA-256, and writes the
plaintext to --out.

Usage:
    python3 refdec.py --storage S --archive HEX --key-file K.key --out OUT
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import struct
import sys
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ---- documented layout constants (README "Binary layout") ------------------
DESC = struct.Struct(">4sH16sQIQ4s32s")   # 78-byte descriptor
TAG = 16
WRAP_NONCE = 12
DESC_INDEX = 0
FIRST_INDEX = 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--storage", required=True)
    ap.add_argument("--archive", required=True)
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    storage = Path(args.storage)
    aid = bytes.fromhex(args.archive)
    kek = Path(args.key_file).read_bytes()
    assert len(kek) == 32, "KEK file must be exactly 32 bytes"

    con = sqlite3.connect(storage / "archive.db")
    row = con.execute(
        "SELECT a.descriptor, a.desc_tag, a.blob_relpath, e.key_id, e.wrapped_dek"
        " FROM archives a JOIN envelopes e USING(archive_id) WHERE a.archive_id=?",
        (aid,),
    ).fetchone()
    con.close()
    assert row, "archive not found"
    desc_raw, desc_tag, blob_rel, key_id, wrapped = row

    magic, version, archive_id, orig_len, bsz, count, prefix, sha = DESC.unpack(desc_raw)
    assert magic == b"ARC1" and version == 1, "bad descriptor header"
    assert archive_id == aid, "descriptor archive_id mismatch"
    assert count == (0 if orig_len == 0 else -(-orig_len // bsz)), "bad block count"
    desc_digest = hashlib.sha256(desc_raw).digest()

    # Unwrap DEK: AAD = b"ARCW1" || archive_id || sha256(descriptor)
    #                    || len(key_id):2 || key_id
    kb = key_id.encode()
    aad = b"ARCW1" + aid + desc_digest + len(kb).to_bytes(2, "big") + kb
    dek = AESGCM(kek).decrypt(wrapped[:WRAP_NONCE], wrapped[WRAP_NONCE:], aad)
    assert len(dek) == 32
    aead = AESGCM(dek)

    # Descriptor authentication record: nonce prefix||0, empty plaintext,
    # AAD = the descriptor itself.
    aead.decrypt(prefix + DESC_INDEX.to_bytes(8, "big"), desc_tag, desc_raw)

    blob = storage / blob_rel
    expected = (count - 1) * (bsz + TAG) + (orig_len - (count - 1) * bsz) + TAG \
        if count else 0
    assert blob.stat().st_size == expected, "blob size mismatch"

    h = hashlib.sha256()
    with open(blob, "rb") as fin, open(args.out, "wb") as fout:
        for index in range(FIRST_INDEX, count + 1):
            plen = bsz if index < count else orig_len - (count - 1) * bsz
            ct = fin.read(plen + TAG)
            assert len(ct) == plen + TAG, "short block"
            block_aad = (b"ARCB1" + desc_digest + index.to_bytes(8, "big")
                         + plen.to_bytes(4, "big"))
            try:
                pt = aead.decrypt(prefix + index.to_bytes(8, "big"), ct, block_aad)
            except InvalidTag:
                raise SystemExit(f"block {index} authentication failed")
            h.update(pt)
            fout.write(pt)
    assert h.digest() == sha, "whole-archive SHA-256 mismatch"
    print(json.dumps({
        "archive_id": args.archive, "orig_len": orig_len, "block_size": bsz,
        "block_count": count, "key_id": key_id, "sha256": h.hexdigest(),
        "out": args.out, "ok": True,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
