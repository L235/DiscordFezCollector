# Architecture

DiscordFezCollector tails the Wikimedia EventStreams firehose and routes
matching wiki events to Discord: to **custom threads** under one parent
channel, and to **webhook receivers** (arbitrary channels, possibly on other
servers). A single owner configures everything through Discord commands.

## Module map

| Module | Responsibility |
| --- | --- |
| `bot.py` | Entry point shim; calls `src.main.main()`. |
| `src/main.py` | Lifecycle: env validation, signal handlers, `on_ready` startup (command sync, startup notice, background tasks). |
| `src/bot_instance.py` | The `discord.ext.commands.Bot` singleton. |
| `src/config.py` | Env vars, webhook-URL parsing, persistent JSON config (load/save + all mutation helpers, guarded by `CONFIG_LOCK`). |
| `src/constants.py` | Version and Discord message-limit constants. |
| `src/models.py` | `CustomFilter` (compiled include/exclude matcher) and retry/timeout settings classes. |
| `src/routing.py` | Pure routing decisions: global fast-reject, per-filter matching, message truncation. No I/O. |
| `src/event_streams.py` | The event pipeline (producer thread → queue → consumer loop), message formatting, health monitor, operator notices. |
| `src/discord_utils.py` | Discord send wrappers with 5xx retry; webhook error classification (permanent `WebhookError` vs `TransientWebhookError`). |
| `src/commands/` | Command handlers, split by domain (threads, receivers, config, filters, help) plus shared helpers. |
| `src/logging_setup.py` | Rotating file + stdout logging. |

## Data flow

```
Wikimedia EventStreams (SSE: recentchange, revision-create)
        │  blocking iterator, daemon thread ("eventstream-producer")
        ▼
asyncio.Queue (bounded → back-pressure)
        │  consumer: stream_worker() on the bot's event loop
        ▼
staleness check → skip if no user → fast_reject() over all active filters
        │
        ├─ match_filters(thread filters)   → fetch/caches discord.Thread → send
        └─ match_filters(receiver filters) → webhook send
                │
                └─ permanent webhook failure → receiver auto-disabled
                   + notice posted in the parent channel
```

Filters are rebuilt from config only when their config fingerprint changes
(`FILTER_CACHE` / `RECEIVER_FILTER_CACHE`); formatted messages are cached per
link style within one event.

## Persistence

A single JSON file (`FEZ_COLLECTOR_STATE`, default `./state/config.json`):

```json
{
  "version": "0.9",
  "threads":   { "<thread_id>": { "name", "active", "config": {…filter…} } },
  "receivers": { "<key>":       { "name", "active", "errored", "config": {…filter…} } }
}
```

The filter config keys (`pageIncludePatterns`, `userIncludeList`,
`summaryExcludePatterns`, `linkStyle`, `siteName`, …) are defined by
`DEFAULT_CUSTOM_CONFIG` in `src/config.py`. Writes are atomic
(tmp-file + rename) and failures are swallowed with an ERROR log: in-memory
config stays authoritative so a full disk cannot crash the pipeline.
Receiver webhook URLs are **not** persisted; they come from the
`FEZ_WEBHOOKS` env var (`key=url,key=url`), keyed to receiver entries.

## Threading model

Three execution contexts:

1. **The asyncio event loop** — Discord gateway, command handlers, the
   `stream_worker` consumer, the health monitor. Config access goes through
   `CONFIG_LOCK` (an `asyncio.Lock`).
2. **The producer daemon thread** — blocks on the SSE iterator, hands events
   into the loop with `asyncio.run_coroutine_threadsafe`. Talks to the loop
   only through the queue and checks `loop.is_closed()` before touching it
   (a closed loop during redeploy must not wedge the thread — #37).
3. **`last_event_time`** is the one value shared between them, guarded by a
   plain `threading.Lock`.

## Failure & recovery semantics

These encode operational incidents; preserve them when refactoring:

- **No `since=` on stream (re)connect** — a `since` timestamp forces a Kafka
  offset seek on stream.wikimedia.org that intermittently 500s (~20-25% of
  connects). Always reconnect at the live tail; staleness tolerance covers
  the gap. See `new_stream()` and
  [#46](https://github.com/L235/DiscordFezCollector/issues/46).
- **Transient vs permanent webhook failures** — 404/403/401/bad URL disable
  the receiver (with a notice); 5xx/network/timeout drop the one event but
  keep the receiver active. A passing hiccup must never permanently silence
  a receiver ([#48](https://github.com/L235/DiscordFezCollector/pull/48)).
- **Dead custom threads** — a send failure evicts the cached thread; the
  next matching event re-fetches, and a NotFound/Forbidden then deactivates
  the thread and posts a notice. No silent drops.
- **Restart-by-suicide** — a stalled stream (no events past
  `EVENTSTREAM_TIMEOUT_SECS`), a wedged queue, or persistent reconnect
  failure terminates the process (`SIGTERM`, then `sys.exit`); the external
  process manager (Procfile host) restarts it. Restarts announce themselves
  in the parent channel.
- **Discord 5xx retry** — discord.py retries 429s itself but not 5xx on the
  bot path; `discord_api_call_with_backoff` adds that with exponential
  backoff.

## Command surface

Prefix and slash commands (hybrid). Owner-only where destructive
(`authorised` check = `FEZ_OWNER_ID`). Groups: `/config` (per-thread),
`/globalconfig`, `/receiver`, thread lifecycle (`/activate`, `/deactivate`),
filter shortcuts (`/track`, `/ignore`, `/untrack`, `/unignore`), `/fezhelp`.
Filter mutations all funnel through `mutate_filter()` →
`mutate_entity_config_list()`.
