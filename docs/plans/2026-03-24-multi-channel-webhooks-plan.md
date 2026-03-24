# Multi-Channel + Parent-Channel Webhooks Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable multiple parent channels across servers and optional webhook delivery for threads under parent channels that have a configured webhook.

**Architecture:** Two independent changes: (1) `DISCORD_CHANNEL_ID` becomes `DISCORD_CHANNEL_IDS: set[int]`, parsed from comma-separated env var with backward-compat fallback; helpers change `==` to `in`. (2) `send_webhook_with_backoff` gains optional `thread_id` parameter; event_streams routing checks `WEBHOOKS` for parent channel key and prefers webhook delivery with fallback to direct send on error.

**Tech Stack:** Python 3, discord.py (Pycord), pytest, pytest-asyncio

---

### Task 1: Multi-Channel Config — Tests

**Files:**
- Modify: `src/config.py:18` (will change in Task 2)
- Create: `tests/test_multi_channel_config.py`

**Step 1: Write failing tests for channel ID parsing**

Create `tests/test_multi_channel_config.py`:

```python
"""Tests for multi-channel parent ID parsing."""

import os
import pytest


class TestParseChannelIds:
    """Test DISCORD_CHANNEL_IDS parsing from env vars."""

    def test_plural_env_var_comma_separated(self, monkeypatch):
        monkeypatch.setenv("FEZ_COLLECTOR_CHANNEL_IDS", "111,222,333")
        monkeypatch.delenv("FEZ_COLLECTOR_CHANNEL_ID", raising=False)
        from src.config import parse_channel_ids_env
        result = parse_channel_ids_env()
        assert result == {111, 222, 333}

    def test_plural_env_var_single_value(self, monkeypatch):
        monkeypatch.setenv("FEZ_COLLECTOR_CHANNEL_IDS", "111")
        monkeypatch.delenv("FEZ_COLLECTOR_CHANNEL_ID", raising=False)
        from src.config import parse_channel_ids_env
        result = parse_channel_ids_env()
        assert result == {111}

    def test_singular_env_var_fallback(self, monkeypatch):
        monkeypatch.delenv("FEZ_COLLECTOR_CHANNEL_IDS", raising=False)
        monkeypatch.setenv("FEZ_COLLECTOR_CHANNEL_ID", "999")
        from src.config import parse_channel_ids_env
        result = parse_channel_ids_env()
        assert result == {999}

    def test_plural_wins_over_singular(self, monkeypatch):
        monkeypatch.setenv("FEZ_COLLECTOR_CHANNEL_IDS", "111,222")
        monkeypatch.setenv("FEZ_COLLECTOR_CHANNEL_ID", "999")
        from src.config import parse_channel_ids_env
        result = parse_channel_ids_env()
        assert result == {111, 222}

    def test_whitespace_trimmed(self, monkeypatch):
        monkeypatch.setenv("FEZ_COLLECTOR_CHANNEL_IDS", " 111 , 222 ")
        monkeypatch.delenv("FEZ_COLLECTOR_CHANNEL_ID", raising=False)
        from src.config import parse_channel_ids_env
        result = parse_channel_ids_env()
        assert result == {111, 222}

    def test_neither_env_var_returns_zero_set(self, monkeypatch):
        monkeypatch.delenv("FEZ_COLLECTOR_CHANNEL_IDS", raising=False)
        monkeypatch.delenv("FEZ_COLLECTOR_CHANNEL_ID", raising=False)
        from src.config import parse_channel_ids_env
        result = parse_channel_ids_env()
        assert result == {0}
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/kevin/workspace/DiscordFezCollector && python -m pytest tests/test_multi_channel_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_channel_ids_env'`

**Step 3: Commit test file**

```bash
git add tests/test_multi_channel_config.py
git commit -m "test: add tests for multi-channel ID parsing (#18)"
```

---

### Task 2: Multi-Channel Config — Implementation

**Files:**
- Modify: `src/config.py:17-33`

**Step 1: Add `parse_channel_ids_env()` and replace `DISCORD_CHANNEL_ID`**

In `src/config.py`, replace lines 17-33:

```python
# Before:
DISCORD_TOKEN      = os.getenv("FEZ_COLLECTOR_DISCORD_TOKEN")
DISCORD_CHANNEL_ID = int(os.getenv("FEZ_COLLECTOR_CHANNEL_ID", "0"))
OWNER_ID           = int(os.getenv("FEZ_OWNER_ID", "0"))
STATE_FILE         = Path(os.getenv("FEZ_COLLECTOR_STATE", "./state/config.json"))
USER_AGENT         = os.getenv(
    "USER_AGENT",
    "DiscordFezCollector/1.0 (https://github.com/L235/DiscordFezCollector; User:L235)"
)

# Processing constants
STALENESS_SECS = 2 * 60 * 60  # discard events older than 2 hours

def validate_env() -> None:
    """Validate required environment variables. Call from main(), not at import time."""
    if not DISCORD_TOKEN or not DISCORD_CHANNEL_ID:
        logger.error("FEZ_COLLECTOR_DISCORD_TOKEN or FEZ_COLLECTOR_CHANNEL_ID missing")
        sys.exit("FEZ_COLLECTOR_DISCORD_TOKEN or FEZ_COLLECTOR_CHANNEL_ID missing")
```

```python
# After:
DISCORD_TOKEN      = os.getenv("FEZ_COLLECTOR_DISCORD_TOKEN")
OWNER_ID           = int(os.getenv("FEZ_OWNER_ID", "0"))
STATE_FILE         = Path(os.getenv("FEZ_COLLECTOR_STATE", "./state/config.json"))
USER_AGENT         = os.getenv(
    "USER_AGENT",
    "DiscordFezCollector/1.0 (https://github.com/L235/DiscordFezCollector; User:L235)"
)

# Processing constants
STALENESS_SECS = 2 * 60 * 60  # discard events older than 2 hours


def parse_channel_ids_env() -> set[int]:
    """Parse parent channel IDs from env vars.

    FEZ_COLLECTOR_CHANNEL_IDS (comma-separated) takes precedence.
    Falls back to FEZ_COLLECTOR_CHANNEL_ID (singular) for backward compat.
    """
    plural = os.getenv("FEZ_COLLECTOR_CHANNEL_IDS", "").strip()
    if plural:
        return {int(x.strip()) for x in plural.split(",") if x.strip()}
    return {int(os.getenv("FEZ_COLLECTOR_CHANNEL_ID", "0"))}


DISCORD_CHANNEL_IDS: set[int] = parse_channel_ids_env()

# Backward-compat alias — modules that only need a single ID (e.g. validate_env)
DISCORD_CHANNEL_ID = next(iter(DISCORD_CHANNEL_IDS))


def validate_env() -> None:
    """Validate required environment variables. Call from main(), not at import time."""
    if not DISCORD_TOKEN or DISCORD_CHANNEL_IDS == {0}:
        logger.error("FEZ_COLLECTOR_DISCORD_TOKEN or FEZ_COLLECTOR_CHANNEL_ID(S) missing")
        sys.exit("FEZ_COLLECTOR_DISCORD_TOKEN or FEZ_COLLECTOR_CHANNEL_ID(S) missing")
```

**Step 2: Run multi-channel config tests**

Run: `cd /home/kevin/workspace/DiscordFezCollector && python -m pytest tests/test_multi_channel_config.py -v`
Expected: All 6 tests PASS

**Step 3: Run full test suite to check nothing broke**

Run: `cd /home/kevin/workspace/DiscordFezCollector && python -m pytest -v`
Expected: All tests PASS (existing tests use `DISCORD_CHANNEL_ID` which still exists as alias)

**Step 4: Commit**

```bash
git add src/config.py
git commit -m "feat: parse multi-channel IDs from FEZ_COLLECTOR_CHANNEL_IDS env var (#18)"
```

---

### Task 3: Update Helpers for Multi-Channel — Tests

**Files:**
- Modify: `tests/test_command_helpers.py`

**Step 1: Write failing tests for multi-channel helpers**

Append to `tests/test_command_helpers.py`:

```python
class TestInParentChannel:
    def test_matches_when_channel_in_set(self, monkeypatch):
        import src.commands.helpers as helpers_mod
        monkeypatch.setattr(helpers_mod, "DISCORD_CHANNEL_IDS", {100, 200, 300})
        ctx = AsyncMock()
        ctx.channel.id = 200
        from src.commands.helpers import _in_parent_channel
        assert _in_parent_channel(ctx) is True

    def test_rejects_when_channel_not_in_set(self, monkeypatch):
        import src.commands.helpers as helpers_mod
        monkeypatch.setattr(helpers_mod, "DISCORD_CHANNEL_IDS", {100, 200, 300})
        ctx = AsyncMock()
        ctx.channel.id = 999
        from src.commands.helpers import _in_parent_channel
        assert _in_parent_channel(ctx) is False


class TestInCustomThread:
    def test_matches_thread_under_any_parent(self, monkeypatch):
        import src.commands.helpers as helpers_mod
        import discord
        monkeypatch.setattr(helpers_mod, "DISCORD_CHANNEL_IDS", {100, 200})
        ctx = AsyncMock()
        ctx.channel = AsyncMock(spec=discord.Thread)
        ctx.channel.parent_id = 200
        from src.commands.helpers import _in_custom_thread
        assert _in_custom_thread(ctx) is True

    def test_rejects_thread_under_unknown_parent(self, monkeypatch):
        import src.commands.helpers as helpers_mod
        import discord
        monkeypatch.setattr(helpers_mod, "DISCORD_CHANNEL_IDS", {100, 200})
        ctx = AsyncMock()
        ctx.channel = AsyncMock(spec=discord.Thread)
        ctx.channel.parent_id = 999
        from src.commands.helpers import _in_custom_thread
        assert _in_custom_thread(ctx) is False

    def test_rejects_non_thread_channel(self, monkeypatch):
        import src.commands.helpers as helpers_mod
        monkeypatch.setattr(helpers_mod, "DISCORD_CHANNEL_IDS", {100})
        ctx = AsyncMock()
        ctx.channel = AsyncMock(spec=discord.TextChannel)
        ctx.channel.parent_id = 100
        from src.commands.helpers import _in_custom_thread
        assert _in_custom_thread(ctx) is False
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/kevin/workspace/DiscordFezCollector && python -m pytest tests/test_command_helpers.py::TestInParentChannel -v`
Expected: FAIL — `DISCORD_CHANNEL_IDS` not found in helpers module (it still imports `DISCORD_CHANNEL_ID`)

**Step 3: Commit test additions**

```bash
git add tests/test_command_helpers.py
git commit -m "test: add multi-channel helper tests (#18)"
```

---

### Task 4: Update Helpers for Multi-Channel — Implementation

**Files:**
- Modify: `src/commands/helpers.py:12-43`

**Step 1: Update imports and helper functions**

In `src/commands/helpers.py`, change the import (line 15) and both helper functions (lines 33-43):

```python
# Before (line 15):
    DISCORD_CHANNEL_ID,

# After:
    DISCORD_CHANNEL_IDS,
```

```python
# Before (lines 33-43):
def _in_parent_channel(ctx: commands.Context) -> bool:
    """True when in the parent channel. TODO #18: change to `in` when DISCORD_CHANNEL_ID becomes a set."""
    return ctx.channel.id == DISCORD_CHANNEL_ID


def _in_custom_thread(ctx: commands.Context) -> bool:
    """True when inside any thread spawned from the parent channel."""
    return (
        isinstance(ctx.channel, discord.Thread)
        and ctx.channel.parent_id == DISCORD_CHANNEL_ID
    )

# After:
def _in_parent_channel(ctx: commands.Context) -> bool:
    """True when in any configured parent channel."""
    return ctx.channel.id in DISCORD_CHANNEL_IDS


def _in_custom_thread(ctx: commands.Context) -> bool:
    """True when inside any thread spawned from a parent channel."""
    return (
        isinstance(ctx.channel, discord.Thread)
        and ctx.channel.parent_id in DISCORD_CHANNEL_IDS
    )
```

**Step 2: Run helper tests**

Run: `cd /home/kevin/workspace/DiscordFezCollector && python -m pytest tests/test_command_helpers.py -v`
Expected: All tests PASS (old and new)

**Step 3: Run full test suite**

Run: `cd /home/kevin/workspace/DiscordFezCollector && python -m pytest -v`
Expected: All tests PASS

**Step 4: Commit**

```bash
git add src/commands/helpers.py
git commit -m "feat: support multiple parent channels in helpers (#18)"
```

---

### Task 5: Parent-Channel Webhook Delivery — Tests

**Files:**
- Create: `tests/test_webhook_thread_delivery.py`

**Step 1: Write failing tests for webhook thread delivery**

Create `tests/test_webhook_thread_delivery.py`:

```python
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
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/kevin/workspace/DiscordFezCollector && python -m pytest tests/test_webhook_thread_delivery.py -v`
Expected: FAIL — `send_webhook_with_backoff()` doesn't accept `thread_id` parameter

**Step 3: Commit test file**

```bash
git add tests/test_webhook_thread_delivery.py
git commit -m "test: add webhook thread delivery tests (#18)"
```

---

### Task 6: Parent-Channel Webhook Delivery — Implementation

**Files:**
- Modify: `src/discord_utils.py:86-133`

**Step 1: Add `thread_id` parameter to `send_webhook_with_backoff`**

In `src/discord_utils.py`, update the function signature and body (lines 86-132):

```python
# Before:
async def send_webhook_with_backoff(
    webhook_url: str,
    content: str,
    receiver_key: str
) -> bool:

# After:
async def send_webhook_with_backoff(
    webhook_url: str,
    content: str,
    receiver_key: str,
    *,
    thread_id: Optional[int] = None,
) -> bool:
```

Update the docstring Args section to include:
```
        thread_id: Optional Discord thread ID. When provided, sends the message
                   into that thread via the webhook.
```

Update the `discord_api_call_with_backoff` call (line 118):

```python
# Before:
        await discord_api_call_with_backoff(webhook.send, content)

# After:
        send_kwargs = {}
        if thread_id is not None:
            send_kwargs["thread"] = discord.Object(id=thread_id)
        await discord_api_call_with_backoff(webhook.send, content, **send_kwargs)
```

**Step 2: Run webhook tests**

Run: `cd /home/kevin/workspace/DiscordFezCollector && python -m pytest tests/test_webhook_thread_delivery.py -v`
Expected: All tests PASS

**Step 3: Run full test suite**

Run: `cd /home/kevin/workspace/DiscordFezCollector && python -m pytest -v`
Expected: All tests PASS (existing callers don't pass `thread_id`, so backward-compatible)

**Step 4: Commit**

```bash
git add src/discord_utils.py
git commit -m "feat: add thread_id parameter to send_webhook_with_backoff (#18)"
```

---

### Task 7: Event Streams Routing — Prefer Webhook for Parent Channels

**Files:**
- Modify: `src/event_streams.py:474-482`

**Step 1: Update thread message sending to prefer parent-channel webhook**

In `src/event_streams.py`, add `DISCORD_CHANNEL_IDS` to imports (line 32):

```python
# Before:
from src.config import (
    CONFIG,
    CONFIG_LOCK,
    WEBHOOKS,
    USER_AGENT,
    STALENESS_SECS,
    set_receiver_errored,
)

# After:
from src.config import (
    CONFIG,
    CONFIG_LOCK,
    DISCORD_CHANNEL_IDS,
    WEBHOOKS,
    USER_AGENT,
    STALENESS_SECS,
    set_receiver_errored,
)
```

Replace the thread sending block (lines 474-482):

```python
# Before:
        # Send to thread targets with rate limit handling
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Sending change to %d thread(s) and %d receiver(s)",
                len(targets),
                len(receiver_targets),
            )
        for tgt, style in targets:
            await send_message_with_backoff(tgt, _get_msg(style))

# After:
        # Send to thread targets — prefer parent-channel webhook if available
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Sending change to %d thread(s) and %d receiver(s)",
                len(targets),
                len(receiver_targets),
            )
        for tgt, style in targets:
            msg = _get_msg(style)
            parent_wh_url = WEBHOOKS.get(str(tgt.parent_id)) if hasattr(tgt, "parent_id") else None
            if parent_wh_url:
                try:
                    await send_webhook_with_backoff(parent_wh_url, msg, f"parent-{tgt.parent_id}", thread_id=tgt.id)
                    continue
                except WebhookError:
                    logger.warning(f"Parent-channel webhook failed for thread {tgt.id}; falling back to direct send")
            await send_message_with_backoff(tgt, msg)
```

**Step 2: Run full test suite**

Run: `cd /home/kevin/workspace/DiscordFezCollector && python -m pytest -v`
Expected: All tests PASS

**Step 3: Commit**

```bash
git add src/event_streams.py
git commit -m "feat: prefer parent-channel webhook for thread delivery (#18)"
```

---

### Task 8: Update conftest and validate_env

**Files:**
- Modify: `tests/conftest.py:3`

**Step 1: Update conftest to also set plural env var**

In `tests/conftest.py`, add after line 3:

```python
# Before:
os.environ.setdefault("FEZ_COLLECTOR_CHANNEL_ID", "123456789")

# After:
os.environ.setdefault("FEZ_COLLECTOR_CHANNEL_ID", "123456789")
os.environ.setdefault("FEZ_COLLECTOR_CHANNEL_IDS", "123456789")
```

**Step 2: Run full test suite**

Run: `cd /home/kevin/workspace/DiscordFezCollector && python -m pytest -v`
Expected: All tests PASS

**Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "chore: set FEZ_COLLECTOR_CHANNEL_IDS in test conftest (#18)"
```

---

### Task 9: Final Validation and Cleanup

**Step 1: Run full test suite one final time**

Run: `cd /home/kevin/workspace/DiscordFezCollector && python -m pytest -v`
Expected: All tests PASS

**Step 2: Verify no remaining TODO #18 references**

Run: `grep -rn "TODO #18" src/`
Expected: No output (the TODO in helpers.py was removed in Task 4)

**Step 3: Review all changes**

Run: `git log --oneline -10` to confirm commit history looks clean.

**Step 4: Squash or leave as-is per project convention, then push**
