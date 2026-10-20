"""Bounded asynchronous requests to one plugin-owned local reader process."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

MAX_LINE = 8 * 1024 * 1024


class ReaderNotReady(RuntimeError):
    """Initialization is pending; its worker must remain alive."""


class BuiltinNoticeSource:
    """Keep blocking reads and optional native dependencies out of AstrBot."""

    mode = "builtin"

    def __init__(self, data_dir: Path, storage: Path, account: str):
        self.data_dir, self.storage, self.account = data_dir, storage, account
        self.process = None
        self.lock = asyncio.Lock()
        self.sequence, self.failures = 0, 0
        self.retry_at, self.backoff = 0, 0
        self.last_status = {"state": "idle", "error": ""}
        self.configuration_error = ""
        if os.name != "nt" or sys.maxsize < 2**32 or sys.version_info[:2] != (3, 12):
            self.configuration_error = "内置读取需要 Windows x64 和 Python3.12"
        elif not account or storage.name != "db_storage" or storage.parent.name != account:
            self.configuration_error = "请在本机插件配置填写账号目录名和对应 db_storage 完整路径"

    async def _start(self):
        if self.configuration_error:
            raise ValueError(self.configuration_error)
        if self.process and self.process.returncode is None:
            return
        package = Path(__file__).resolve().parents[1]
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            "-m",
            package.name + ".builtin.worker",
            cwd=package.parent,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=MAX_LINE,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        config = {
            "data_dir": str(self.data_dir),
            "storage": str(self.storage),
            "account": self.account,
        }
        self.process.stdin.write((json.dumps(config) + "\n").encode())
        await self.process.stdin.drain()

    async def _request(self, method, **params):
        async with self.lock:
            try:
                await self._start()
                self.sequence += 1
                request_id = self.sequence
                packet = json.dumps({"id": request_id, "method": method, "params": params})
                if len(packet.encode()) >= MAX_LINE:
                    raise RuntimeError("Reader request exceeds limit")
                async with asyncio.timeout(30):
                    self.process.stdin.write((packet + "\n").encode())
                    await self.process.stdin.drain()
                    raw = await self.process.stdout.readline()
                    if not raw or len(raw) >= MAX_LINE:
                        raise RuntimeError("Reader stopped or response exceeds limit")
                    response = json.loads(raw)
                    if not isinstance(response, dict) or response.get("id") != request_id:
                        raise RuntimeError("Reader protocol mismatch")
                    if response.get("error"):
                        raise RuntimeError(str(response["error"])[:200])
                    if not isinstance(response.get("result"), dict):
                        raise RuntimeError("Invalid reader response")
                    return response["result"]
            except BaseException:
                await asyncio.shield(self.close())
                raise

    async def get(self, path, **params):
        if time.monotonic() < self.retry_at:
            raise RuntimeError("内置读取暂停重试；用 /通知状态 查看初始化状态")
        try:
            self.last_status = await self._request("status")
            if self.last_status.get("state") != "ready":
                raise ReaderNotReady("请在本机初始化内置读取，等待手动重启微信并完成登录")
            response = await self._request("get", path=path, **params)
            self.failures, self.retry_at, self.backoff = 0, 0, 0
            return response
        except ReaderNotReady:
            raise
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception:
            self.failures += 1
            self.backoff = (5, 15, 30, 60, 120, 300)[min(self.failures - 1, 5)]
            self.retry_at = time.monotonic() + self.backoff
            await self.close()
            raise RuntimeError("内置读取不可用，请检查本机初始化、登录及微信版本") from None

    async def initialize(self):
        if self.configuration_error:
            raise ValueError(self.configuration_error)
        self.retry_at = 0
        return await self._request("initialize")

    async def status(self):
        if self.configuration_error:
            return {"state": "error", "error": self.configuration_error}
        try:
            self.last_status = await self._request("status")
        except Exception:
            self.last_status = {"state": "error", "error": "读取进程不可用，请检查依赖或重载插件"}
        return dict(self.last_status)

    async def close(self):
        process, self.process = self.process, None
        if process:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            else:
                await process.wait()
