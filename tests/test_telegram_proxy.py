"""Offline regression tests for direct and proxied Telegram clients."""

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


class TelegramProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = SimpleNamespace(
            BOT_TOKEN="123:test-only",
            TELEGRAM_API=123,
            TELEGRAM_HASH="test-only",
            TG_PROXY={},
            USER_SESSION_STRING="test-only",
        )
        self.client = SimpleNamespace(
            start=AsyncMock(),
            me=SimpleNamespace(username="test_bot", is_premium=False),
        )
        self.client_factory = Mock(return_value=self.client)
        self.logger = Mock()

        bot = ModuleType("bot")
        bot.LOGGER = self.logger
        config = ModuleType("bot.core.config_manager")
        config.Config = self.config
        pyrogram = ModuleType("pyrogram")
        pyrogram.Client = self.client_factory
        pyrogram.enums = SimpleNamespace(ParseMode=SimpleNamespace(HTML="html"))
        types = ModuleType("pyrogram.types")
        types.LinkPreviewOptions = Mock()
        path = Path(__file__).resolve().parents[1] / "bot/core/telegram_manager.py"
        spec = importlib.util.spec_from_file_location("bot.core.telegram_manager", path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(
            sys.modules,
            {
                "bot": bot,
                "bot.core.config_manager": config,
                "pyrogram": pyrogram,
                "pyrogram.types": types,
            },
        ):
            spec.loader.exec_module(module)
        self.manager = module.TgClient

    async def _check_proxy(self, value, expected):
        self.config.TG_PROXY = value
        for method in ("start_bot", "start_user"):
            with self.subTest(client=method):
                self.client_factory.reset_mock()
                self.client.start.reset_mock()
                self.logger.reset_mock()
                await getattr(self.manager, method)()
                self.client_factory.assert_called_once()
                self.assertIs(self.client_factory.call_args.kwargs["proxy"], expected)
                self.client.start.assert_awaited_once()
                self.logger.error.assert_not_called()

    async def test_empty_proxy_uses_direct_connection(self):
        await self._check_proxy({}, None)

    async def test_none_proxy_uses_direct_connection(self):
        await self._check_proxy(None, None)

    async def test_configured_proxy_is_preserved(self):
        proxy = {"scheme": "socks5", "hostname": "127.0.0.1", "port": 1080}
        await self._check_proxy(proxy, proxy)


if __name__ == "__main__":
    unittest.main()
