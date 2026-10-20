"""Verify that the selected account is actually open in the current user's WeChat."""

import os
import time
from pathlib import Path


class LiveAccountGuard:
    """Read-only process/file inspection, with a short positive-result cache."""

    def __init__(self, storage: Path):
        self.storage = storage.resolve()
        self.checked_at, self.active = 0, False

    def __call__(self, force=False):
        if not force and self.active and time.monotonic() - self.checked_at < 3:
            return True
        import psutil

        self.active = False
        try:
            current_user = psutil.Process(os.getpid()).username().casefold()
            for process in psutil.process_iter(["name", "username"]):
                if (process.info["name"] or "").casefold() != "weixin.exe":
                    continue
                if (process.info["username"] or "").casefold() != current_user:
                    continue
                try:
                    files = process.open_files()
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    continue
                for opened in files:
                    path = Path(opened.path).resolve()
                    if path.is_relative_to(self.storage) and path.suffix == ".db":
                        self.active = True
                        break
                if self.active:
                    break
        except (OSError, psutil.Error):
            self.active = False
        self.checked_at = time.monotonic()
        return self.active
