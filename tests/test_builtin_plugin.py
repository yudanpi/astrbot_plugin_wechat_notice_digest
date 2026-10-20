import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_wechat_notice_digest.main import WeChatNoticeDigest


class BuiltinPluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_plugin_initialization_closes_created_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            source = SimpleNamespace(mode="builtin", initialize=AsyncMock(), close=AsyncMock())
            with patch(
                "astrbot_plugin_wechat_notice_digest.main.StarTools.get_data_dir",
                return_value=Path(directory),
            ):
                with patch(
                    "astrbot_plugin_wechat_notice_digest.main.BuiltinNoticeSource",
                    return_value=source,
                ):
                    plugin = WeChatNoticeDigest(
                        SimpleNamespace(),
                        {
                            "source_mode": "builtin",
                            "reader_initialize": True,
                            "pages_per_group": "invalid",
                        },
                    )
                    with self.assertRaises(ValueError):
                        await plugin.initialize()
                    source.close.assert_awaited_once()

    async def test_external_default_and_explicit_builtin_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "astrbot_plugin_wechat_notice_digest.main.StarTools.get_data_dir",
                return_value=Path(directory),
            ):
                plugin = WeChatNoticeDigest(SimpleNamespace(), {})
                await plugin.initialize()
                self.assertEqual(plugin.collector.source.mode, "external")
                await plugin.terminate()
                source = SimpleNamespace(
                    mode="builtin",
                    initialize=AsyncMock(),
                    close=AsyncMock(),
                    get=AsyncMock(side_effect=RuntimeError("not ready")),
                )
                config = {
                    "source_mode": "builtin",
                    "reader_initialize": True,
                    "account": "a",
                    "wechat_storage_dir": str(Path(directory) / "a/db_storage"),
                }
                with patch(
                    "astrbot_plugin_wechat_notice_digest.main.BuiltinNoticeSource",
                    return_value=source,
                ):
                    plugin = WeChatNoticeDigest(SimpleNamespace(), config)
                    await plugin.initialize()
                    source.initialize.assert_awaited_once()
                    self.assertFalse(config["reader_initialize"])
                    self.assertIs(plugin.collector.source, source)
                    await plugin.terminate()
                    source.close.assert_awaited_once()

    async def test_migration_keeps_settings_and_excludes_unmapped_history(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "astrbot_plugin_wechat_notice_digest.main.StarTools.get_data_dir",
                return_value=Path(directory),
            ):
                config = {
                    "source_mode": "builtin",
                    "account": "a",
                    "wechat_storage_dir": str(Path(directory) / "a/db_storage"),
                }
                plugin = WeChatNoticeDigest(SimpleNamespace(), config)
                await plugin.initialize()
                self.assertTrue(hasattr(plugin.collector, "minimum_timestamp"))
                self.assertFalse(config.get("reader_initialize", False))
                await plugin.terminate()
