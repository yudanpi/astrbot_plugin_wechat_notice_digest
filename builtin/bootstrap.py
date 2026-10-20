"""Explicit, bounded local initialization; never terminate or relaunch WeChat."""

import csv
import io
import re
import subprocess
import threading
import time
from pathlib import Path

from .messages import LocalMessageReader
from .snapshot import decrypt_snapshot


def _main_pid():
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=3,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    processes = []
    for row in csv.reader(io.StringIO(result.stdout)):
        if len(row) >= 5 and row[0].lower() == "weixin.exe" and row[1].isdigit():
            size = re.sub(r"[^0-9]", "", row[4])
            if size and int(size) > 20 * 1024:
                processes.append((int(size), int(row[1])))
    return max(processes)[1] if processes else None


def capture_key(storage: Path, cancel: threading.Event, timeout: int) -> bytes:
    """Wait for a manually restarted process and verify candidates locally."""
    import frida

    database = storage / "message/message_0.db"
    with database.open("rb") as handle:
        first_page = handle.read(4096)
    device = frida.get_local_device()
    original = _main_pid()
    deadline = time.monotonic() + timeout
    target = None
    while not cancel.is_set() and time.monotonic() < deadline:
        pid = _main_pid()
        if pid and pid != original:
            target = pid
            break
        cancel.wait(0.05)
    if not target:
        raise TimeoutError("WeChat restart was not observed")
    session = device.attach(target)
    result = []
    seen = set()
    failed = threading.Event()

    def on_message(message, data):
        if message.get("type") == "error":
            failed.set()
        payload = message.get("payload", "")
        if not isinstance(payload, str):
            return
        if payload.startswith("ERR:"):
            failed.set()
        if payload.startswith("KEY:") and re.fullmatch(r"[0-9a-fA-F]{64}", payload[4:]):
            candidate = bytes.fromhex(payload[4:])
            if candidate in seen or len(seen) >= 64:
                return
            seen.add(candidate)
            try:
                decrypt_snapshot(first_page, candidate)
            except Exception:
                return
            result.append(candidate)

    script = None
    try:
        js = Path(__file__).resolve().parents[1] / "vendor/wechat_decrypt/capture.js"
        script = session.create_script(js.read_text(encoding="utf-8"))
        script.on("message", on_message)
        script.load()
        while (
            not cancel.is_set()
            and time.monotonic() < deadline
            and not result
            and not failed.is_set()
        ):
            cancel.wait(0.05)
        if cancel.is_set() or not result:
            raise TimeoutError("Initialization timed out or unsupported WeChat version")
        return result[0]
    finally:
        if script:
            try:
                script.unload()
            except Exception:
                pass
        try:
            session.detach()
        except Exception:
            pass
        result.clear()
        seen.clear()


class BootstrapController:
    """Only an explicit local request can start credential initialization."""

    def __init__(self, storage: Path, account: str, credentials, *, capture=capture_key):
        LocalMessageReader(account, storage)
        self.storage, self.account, self.credentials = storage, account, credentials
        self.capture = capture
        self.cancelled = threading.Event()
        self.thread = None
        self.state, self.error = "idle", ""

    def start(self, timeout: int = 120) -> None:
        if not 1 <= timeout <= 120:
            raise ValueError("Initialization timeout must be 1–120 seconds")
        if self.thread and self.thread.is_alive():
            return
        self.cancelled.clear()
        self.state, self.error = "waiting", ""
        self.thread = threading.Thread(target=self._run, args=(timeout,), daemon=True)
        self.thread.start()

    def _run(self, timeout):
        try:
            key = self.capture(self.storage, self.cancelled, timeout)
            if self.cancelled.is_set():
                self.state = "cancelled"
                return
            with (self.storage / "message/message_0.db").open("rb") as database:
                decrypt_snapshot(database.read(4096), key)
            if self.cancelled.is_set():
                self.state = "cancelled"
                return
            self.credentials.save(self.account, key)
            self.state = "ready"
        except Exception as error:
            self.state = "cancelled" if self.cancelled.is_set() else "error"
            self.error = (
                type(error).__name__
                + ": initialization failed; check login, permissions and supported version"
            )

    def cancel(self) -> None:
        self.cancelled.set()

    def wait(self, timeout=None):
        if self.thread:
            self.thread.join(timeout)

    def status(self) -> dict:
        return {"state": self.state, "error": self.error}
