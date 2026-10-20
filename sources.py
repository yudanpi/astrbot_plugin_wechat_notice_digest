"""Transport-only data sources shared by the existing collector."""

import asyncio
from urllib.parse import urlparse

import aiohttp


class HttpNoticeSource:
    """Preserve loopback-only HTTP behavior for existing installations."""

    mode = "external"

    def __init__(self, base_url):
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("数据接口必须是本机 HTTP 地址")
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("数据接口地址只能包含本机地址和端口")
        self.base_url, self.session = base_url.rstrip("/"), None

    async def get(self, path, **params):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20), trust_env=False
            )
        for attempt in range(2):
            try:
                async with self.session.get(
                    self.base_url + path, params=params, allow_redirects=False
                ) as response:
                    if response.status != 200:
                        raise RuntimeError(f"本地接口 HTTP {response.status}")
                    data = await response.json()
                if not isinstance(data, dict) or data.get("status") not in {"success", 0}:
                    raise RuntimeError("本地接口返回失败状态")
                return data
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError):
                if attempt:
                    raise
                await asyncio.sleep(0.3)

    async def close(self):
        if self.session:
            await self.session.close()
