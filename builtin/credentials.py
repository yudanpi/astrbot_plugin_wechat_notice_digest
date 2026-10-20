"""Protect account credentials with current-user Windows DPAPI and ACLs."""

import base64
import csv
import ctypes
import io
import json
import os
import subprocess
import uuid
from ctypes import wintypes
from pathlib import Path


def secure_directory(directory: Path) -> None:
    """Restrict this plugin-owned directory before placing private files in it."""
    if os.name != "nt":
        raise RuntimeError("Built-in reader requires Windows")
    directory.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    sid = next(csv.reader(io.StringIO(result.stdout)))[1]
    if not sid.startswith("S-1-"):
        raise RuntimeError("Cannot resolve current Windows user")
    subprocess.run(
        ["icacls", str(directory), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F", "/q"],
        check=True,
        capture_output=True,
        timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Windows credential protection unavailable")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, output = Blob(len(data), buffer), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise RuntimeError("Windows credential operation failed")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def _protect(data):
    return _dpapi(data, decrypt=False)


class CredentialStore:
    """Bind one protected record to one explicitly selected account."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "credentials.dpapi"

    def save(self, account: str, raw_key: bytes) -> None:
        if not account or len(raw_key) != 32:
            raise ValueError("Invalid account credential")
        temporary = None
        try:
            blob = _protect(json.dumps({"account": account, "key": raw_key.hex()}).encode())
            secure_directory(self.directory)
            temporary = self.directory / (uuid.uuid4().hex + ".tmp")
            with temporary.open("x", encoding="utf-8") as output:
                json.dump({"blob": base64.b64encode(blob).decode()}, output)
            temporary.replace(self.path)
        except Exception:
            raise RuntimeError("Credential protection failed") from None
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    def load(self, account: str) -> bytes:
        try:
            blob = base64.b64decode(
                json.loads(self.path.read_text(encoding="utf-8"))["blob"], validate=True
            )
            record = json.loads(_dpapi(blob, decrypt=True))
            key = bytes.fromhex(record["key"])
            if record["account"] != account or len(key) != 32:
                raise ValueError
            return key
        except Exception:
            raise RuntimeError(
                "Credential unavailable for selected account; initialize locally"
            ) from None
