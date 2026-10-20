"""Small synthetic SQLCipher fixtures; never read a real account."""

import hashlib
import hmac
import sqlite3
import struct

from Crypto.Cipher import AES

PAGE = 4096
RAW_KEY = b"synthetic-key-only-for-tests!!!!!"[:32].ljust(32, b"!")


def sqlite_bytes(sql="CREATE TABLE sample(value TEXT); INSERT INTO sample VALUES ('hello');"):
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA page_size=4096")
    connection.executescript(sql)
    raw = bytearray(connection.serialize())
    connection.close()
    raw[20] = 80
    for start in range(0, len(raw), PAGE):
        header = start + (100 if start == 0 else 0)
        assert raw[header] == 13, "Fixture only supports small leaf pages"
        cells = int.from_bytes(raw[header + 3:header + 5], "big")
        content = int.from_bytes(raw[header + 5:header + 7], "big")
        assert content > (header - start) + 8 + cells * 2 + 80
        payload = bytes(raw[start + content:start + PAGE])
        raw[start + content - 80:start + PAGE - 80] = payload
        raw[start + PAGE - 80:start + PAGE] = b"\0" * 80
        raw[header + 5:header + 7] = (content - 80).to_bytes(2, "big")
        for i in range(cells):
            offset = header + 8 + i * 2
            pointer = int.from_bytes(raw[offset:offset + 2], "big") - 80
            raw[offset:offset + 2] = pointer.to_bytes(2, "big")
    return bytes(raw)


def encrypt(raw, key=RAW_KEY, salt=b"fixture-salt-123"):
    cipher_key = hashlib.pbkdf2_hmac("sha512", key, salt, 256000, 32)
    mac_key = hashlib.pbkdf2_hmac("sha512", cipher_key, bytes(b ^ 0x3A for b in salt), 2, 32)
    output = bytearray()
    for start in range(0, len(raw), PAGE):
        number = start // PAGE + 1
        offset = 16 if number == 1 else 0
        iv = bytes([number % 255]) * 16
        ciphertext = AES.new(cipher_key, AES.MODE_CBC, iv).encrypt(raw[start + offset:start + PAGE - 80])
        page = (salt if number == 1 else b"") + ciphertext + iv
        digest = hmac.new(mac_key, page[offset:] + struct.pack("<I", number), hashlib.sha512).digest()
        output.extend(page + digest)
    return bytes(output)


def wal(frames):
    def checksum(data, state=(0, 0)):
        a, b = state
        for x, y in struct.iter_unpack("<II", data):
            a = (a + x + b) & 0xFFFFFFFF
            b = (b + y + a) & 0xFFFFFFFF
        return a, b
    salts = b"test-sal"
    header = struct.pack(">IIII", 0x377F0682, 3007000, PAGE, 0) + salts
    state = checksum(header)
    output = header + struct.pack(">II", *state)
    for number, size, page in frames:
        prefix = struct.pack(">II", number, size)
        state = checksum(prefix + page, state)
        output += prefix + salts + struct.pack(">II", *state) + page
    return output

