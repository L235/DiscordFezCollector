"""Tests for send_webhook_with_backoff thread_id parameter."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import discord


class TestSendWebhookWithThreadId:
    """Test that send_webhook_with_backoff passes thread kwarg when thread_id is given."""

    @pytest.mark.asyncio
    @patch("src.discord_utils.discord_api_call_with_backoff")
    @patch("src.discord_utils.bot")
    async def test_sends_with_thread_object_when_thread_id_given(self, mock_bot, mock_backoff):
        from src.discord_utils import send_webhook_with_backoff, _WEBHOOK_CACHE
        _WEBHOOK_CACHE.clear()

        mock_webhook = MagicMock(spec=discord.Webhook)
        with patch("discord.Webhook.from_url", return_value=mock_webhook):
            await send_webhook_with_backoff(
                "https://discord.com/api/webhooks/123/abc",
                "test message",
                "test_key",
                thread_id=456
            )
        # Verify thread kwarg was passed to discord_api_call_with_backoff
        mock_backoff.assert_called_once()
        call_kwargs = mock_backoff.call_args[1]
        assert "thread" in call_kwargs
        assert call_kwargs["thread"].id == 456

    @pytest.mark.asyncio
    @patch("src.discord_utils.discord_api_call_with_backoff")
    @patch("src.discord_utils.bot")
    async def test_sends_without_thread_when_no_thread_id(self, mock_bot, mock_backoff):
        from src.discord_utils import send_webhook_with_backoff, _WEBHOOK_CACHE
        _WEBHOOK_CACHE.clear()

        mock_webhook = MagicMock(spec=discord.Webhook)
        with patch("discord.Webhook.from_url", return_value=mock_webhook):
            await send_webhook_with_backoff(
                "https://discord.com/api/webhooks/123/abc",
                "test message",
                "test_key"
            )
        mock_backoff.assert_called_once()
        call_kwargs = mock_backoff.call_args[1]
        assert "thread" not in call_kwargs
