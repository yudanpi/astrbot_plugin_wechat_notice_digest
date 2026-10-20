"""Authenticated SQLCipher snapshots (MIT adaptation; see third-party notices)."""

import hashlib
import hmac
import struct
from pathlib import Path

from ..vendor.wechat_decrypt.wal_snapshot import _metadata, merge_committed_pages


class SnapshotChangedError(RuntimeError):
    """A consistent file pair could not be captured."""


class SnapshotIntegrityError(RuntimeError):
    """The captured pages failed format or authentication checks."""


def read_snapshot(db: Path, attempts: int = 3) -> bytes:
    """Read committed pages without modifying either input file."""
    wal = Path(str(db) + "-wal")
    for _ in range(attempts):
        before = (_metadata(db), _metadata(wal))
        main = db.read_bytes()
        try:
            tail = wal.read_bytes()
        except FileNotFoundError:
            tail = b""
        after = (_metadata(db), _metadata(wal))
        if before != after:
            continue
        try:
            return merge_committed_pages(main, tail)
        except ValueError as error:
            raise SnapshotIntegrityError("Database/WAL snapshot rejected") from error
    raise SnapshotChangedError("Database changed during snapshot; retry later")


def decrypt_snapshot(data: bytes, raw_key: bytes) -> bytes:
    """Authenticate all pages before exposing any SQLite plaintext."""
    from Crypto.Cipher import AES

    if len(raw_key) != 32 or len(data) < 4096 or len(data) % 4096:
        raise SnapshotIntegrityError("Invalid encrypted database format")
    salt = data[:16]
    key = hashlib.pbkdf2_hmac("sha512", raw_key, salt, 256000, 32)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, bytes(b ^ 0x3A for b in salt), 2, 32)
    for start in range(0, len(data), 4096):
        page = data[start : start + 4096]
        offset = 16 if start == 0 else 0
        digest = hmac.new(
            mac_key, page[offset:-64] + struct.pack("<I", start // 4096 + 1), hashlib.sha512
        ).digest()
        if not hmac.compare_digest(digest, page[-64:]):
            raise SnapshotIntegrityError(
                "Database authentication failed; initialize the selected account"
            )
    output = bytearray()
    for start in range(0, len(data), 4096):
        page = data[start : start + 4096]
        offset = 16 if start == 0 else 0
        plain = AES.new(key, AES.MODE_CBC, page[4016:4032]).decrypt(page[offset:4016])
        if start == 0:
            if plain[:2] != b"\x10\x00" or plain[4:8] != b"\x50\x40\x20\x20":
                raise SnapshotIntegrityError("Unsupported SQLCipher database header")
            output.extend(b"SQLite format 3\0")
        output.extend(plain)
        output.extend(page[4016:])
    # WAL has already been merged; deserialize must not look for a disk sidecar.
    output[18:20] = b"\1\1"
    return bytes(output)
