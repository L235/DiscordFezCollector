"""Webhook send failure classification: transient vs permanent.

Regression tests for the bug where ANY ``discord.HTTPException`` (including
transient 5xx / network blips) raised ``WebhookError``, which the EventStreams
worker treats as a signal to permanently disable the receiver. A transient
Discord failure must NOT latch a receiver off; only a genuinely broken webhook
(404 deleted / 403 forbidden / 401 unauthorized) should.
"""
import types

import discord
import pytest

import src.discord_utils as du
from src.discord_utils import (
    send_webhook_with_backoff,
    WebhookError,
    TransientWebhookError,
)
from src.models import DiscordRetryConfig

WEBHOOK_URL = "https://discord.com/api/webhooks/1/token"


def _resp(status, reason="err"):
    """Minimal stand-in for an aiohttp response (discord exceptions read .status/.reason)."""
    return types.SimpleNamespace(status=status, reason=reason)


class _FakeWebhook:
    """Webhook whose .send always raises the configured exception."""

    def __init__(self, exc):
        self._exc = exc
        self.calls = 0

    async def send(self, content):
        self.calls += 1
        raise self._exc


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    """Make the 5xx retry loop instant and short so tests don't sleep."""
    monkeypatch.setattr(DiscordRetryConfig, "MAX_ATTEMPTS", 2)
    monkeypatch.setattr(DiscordRetryConfig, "INITIAL_BACKOFF_SECS", 0.0)
    monkeypatch.setattr(DiscordRetryConfig, "MAX_BACKOFF_SECS", 0.0)


def _install(monkeypatch, exc):
    """Seed the webhook cache so from_url is bypassed; return the fake webhook."""
    fake = _FakeWebhook(exc)
    monkeypatch.setitem(du._WEBHOOK_CACHE, WEBHOOK_URL, fake)
    return fake


# ── Permanent failures → WebhookError (caller disables the receiver) ────────

async def test_404_deleted_is_permanent(monkeypatch):
    _install(monkeypatch, discord.NotFound(_resp(404), "deleted"))
    with pytest.raises(WebhookError):
        await send_webhook_with_backoff(WEBHOOK_URL, "hi", "spi")


async def test_403_forbidden_is_permanent(monkeypatch):
    _install(monkeypatch, discord.Forbidden(_resp(403), "forbidden"))
    with pytest.raises(WebhookError):
        await send_webhook_with_backoff(WEBHOOK_URL, "hi", "spi")


async def test_401_unauthorized_is_permanent(monkeypatch):
    _install(monkeypatch, discord.HTTPException(_resp(401), "bad token"))
    with pytest.raises(WebhookError):
        await send_webhook_with_backoff(WEBHOOK_URL, "hi", "spi")


# ── Transient failures → TransientWebhookError (receiver stays active) ───────

async def test_5xx_is_transient_after_retries(monkeypatch):
    fake = _install(monkeypatch, discord.HTTPException(_resp(503), "unavailable"))
    with pytest.raises(TransientWebhookError):
        await send_webhook_with_backoff(WEBHOOK_URL, "hi", "spi")
    # It retried up to the limit rather than giving up / disabling immediately.
    assert fake.calls == DiscordRetryConfig.MAX_ATTEMPTS


async def test_network_error_is_transient(monkeypatch):
    _install(monkeypatch, OSError("connection reset by peer"))
    with pytest.raises(TransientWebhookError):
        await send_webhook_with_backoff(WEBHOOK_URL, "hi", "spi")


# ── The two error classes must be distinct so the caller can tell them apart ─

def test_transient_is_not_a_webhookerror():
    # event_streams.py disables receivers on `except WebhookError`; a transient
    # failure must not be caught by that clause.
    assert not issubclass(TransientWebhookError, WebhookError)
    assert not issubclass(WebhookError, TransientWebhookError)


# ── Operator notification when a receiver is auto-disabled ───────────────────

def test_disabled_notice_names_receiver_reason_and_recovery():
    from src.event_streams import format_receiver_disabled_notice

    note = format_receiver_disabled_notice("spi", "Webhook not found (404)")
    assert "spi" in note                       # which receiver
    assert "Webhook not found (404)" in note    # why
    assert "/receiver activate spi" in note     # how to recover
