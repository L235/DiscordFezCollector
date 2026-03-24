# Multi-Channel + Parent-Channel Webhooks (#18)

**Date:** 2026-03-24
**Issue:** #18

## Context

Issue #18 requests three capabilities:
1. Multiple parent channels across servers
2. Optional webhook URLs for parent channels (prefer webhook over direct send)
3. Receive-only webhook channels

Capability 3 shipped in PR #22 (receivers). This design addresses capabilities 1 and 2.

The commands refactor (PR #36) pre-shaped the code for multi-channel: `_in_parent_channel()` and `_in_custom_thread()` in `helpers.py` are the only places that check parent channel identity, with an explicit TODO comment for #18.

## Part 1: Multi-Channel Support

### Config

`DISCORD_CHANNEL_ID` (single `int`) becomes `DISCORD_CHANNEL_IDS` (`set[int]`), parsed from a comma-separated env var `FEZ_COLLECTOR_CHANNEL_IDS`.

Backward compatibility: if only `FEZ_COLLECTOR_CHANNEL_ID` (singular) is set, it becomes a set of one. If both are set, the plural form wins.

### Helpers (`src/commands/helpers.py`)

```python
# Before
def _in_parent_channel(ctx):
    return ctx.channel.id == DISCORD_CHANNEL_ID

def _in_custom_thread(ctx):
    return isinstance(ctx.channel, discord.Thread) and ctx.channel.parent_id == DISCORD_CHANNEL_ID

# After
def _in_parent_channel(ctx):
    return ctx.channel.id in DISCORD_CHANNEL_IDS

def _in_custom_thread(ctx):
    return isinstance(ctx.channel, discord.Thread) and ctx.channel.parent_id in DISCORD_CHANNEL_IDS
```

### Everything else

No changes. Thread creation, config commands, event routing, and receivers all work with arbitrary thread IDs independent of parent channel.

## Part 2: Parent-Channel Webhooks

### Delivery logic (`src/event_streams.py`)

When sending to a thread target, the routing checks if `WEBHOOKS` (from `FEZ_WEBHOOKS` env var) has an entry keyed by `str(thread.parent_id)`:
- **Key exists:** use `send_webhook_with_backoff()` with the thread's ID so the message lands in the thread.
- **No key:** fall back to `send_message_with_backoff()` (current behavior).

### Webhook call (`src/discord_utils.py`)

`send_webhook_with_backoff()` gains an optional `thread_id: int` parameter. When provided, passes `thread=discord.Object(id=thread_id)` to `webhook.send()`.

### Error handling

If the parent-channel webhook 404s or 403s, log a warning and fall back to direct send for that message. No auto-deactivation — unlike receivers, the thread itself is still valid; only the webhook is broken.

## Out of scope

- No new commands (no `/parentchannel` management)
- No per-thread webhook override
- No changes to receiver system
- No config schema changes
