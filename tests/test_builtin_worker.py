import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_wechat_notice_digest.builtin.source import BuiltinNoticeSource


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_polling_during_initialization_preserves_capture_process(self):
        source = BuiltinNoticeSource(Path("unused"), Path("a/db_storage"), "a")
        with patch.object(source, "_request", new=AsyncMock(return_value={"state": "waiting"})):
            with patch.object(source, "close", new=AsyncMock()) as close:
                with self.assertRaises(RuntimeError):
                    await source.get("/api/chat/accounts")
                close.assert_not_awaited()

    async def test_no_process_before_first_request_and_no_implicit_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            source = BuiltinNoticeSource(Path(directory), Path(directory) / "a/db_storage", "a")
            self.assertIsNone(source.process)
            state = await source.status()
            self.assertEqual(state["state"], "idle")
            with self.assertRaises(RuntimeError):
                await source.get("/api/chat/accounts")
            await source.close()
            self.assertIsNone(source.process)

    async def test_protocol_errors_cancel_worker_and_enter_bounded_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            source = BuiltinNoticeSource(Path(directory), Path(directory) / "a/db_storage", "a")

            class Fake:
                returncode = None
                stdin = None
                stdout = None

            async def bad(*args, **kwargs):
                raise RuntimeError("Malformed protocol")

            with patch.object(source, "_request", side_effect=bad):
                with self.assertRaises(RuntimeError):
                    await source.get("/api/chat/accounts")
            self.assertGreater(source.retry_at, 0)
            self.assertLessEqual(source.backoff, 300)
            await source.close()
