"""The event pipeline: Wikimedia EventStreams → filters → Discord targets.

Structure (see docs/architecture.md):

- producer_loop()   — daemon thread; tails the blocking SSE iterator, owns the
                      reconnect/backoff policy, feeds the asyncio queue.
- stream_worker()   — async consumer; one _process_event() call per event.
- _process_event()  — staleness gate → pure routing (src/routing.py) →
                      _dispatch().
- _dispatch()       — formats once per link style and sends to matched
                      threads/webhooks, handling eviction and auto-disable.
- eventstream_health_monitor() — restarts the process when the stream stalls.
"""

import asyncio
import concurrent.futures
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import UTC, datetime
from typing import NoReturn

import discord
import requests
from pywikibot import config as pwb_config
from pywikibot.comms.eventstreams import EventStreams

from src.bot_instance import bot
from src.config import (
    CONFIG,
    CONFIG_LOCK,
    STALENESS_SECS,
    USER_AGENT,
    WEBHOOKS,
    set_custom_thread_active,
    set_receiver_errored,
)
from src.discord_utils import (
    TransientWebhookError,
    WebhookError,
    discord_api_call_with_backoff,
    send_message_with_backoff,
    send_webhook_with_backoff,
)
from src.formatting import (
    format_change,
    format_eventstream_timeout_notice,
    format_receiver_disabled_notice,
    format_thread_disabled_notice,
)
from src.logging_setup import logger
from src.models import CustomFilter
from src.routing import fast_reject, match_filters, truncate_for_discord
from src.settings import EventStreamConfig, RetryConfig

# --------------------------------------------------------------------------- #
# ── MediaWiki EventStreams setup                                             #
# --------------------------------------------------------------------------- #

pwb_config.user_agent_description = USER_AGENT

STREAM_NAMES = ["recentchange", "revision-create"]


def new_stream() -> EventStreams:
    """Construct a fresh EventStreams connection at the live tail.

    Constructed lazily (never at import time): pywikibot performs network
    calls during construction, so creating the stream on import would block
    module import — and the whole test suite — in any offline environment.

    NOTE: We deliberately do *not* pass `since=`. Supplying a `since` timestamp
    forces stream.wikimedia.org to perform a Kafka offset seek, which
    intermittently returns HTTP 500 (~20-25% of connections, reproduced
    directly). Since we always reconnected with `since=now` anyway, the seek
    bought nothing over the live tail while causing the 500s. Omitting `since`
    connects at the live tail, which never 500s. Brief reconnect gaps (a few
    seconds, a few times a day) are well within the STALENESS_SECS tolerance.
    Full analysis: https://github.com/L235/DiscordFezCollector/issues/46
    """
    return EventStreams(streams=STREAM_NAMES, headers={"user-agent": USER_AGENT})


def terminate_process(reason: str) -> NoReturn:
    """Restart-by-suicide: the external process manager restarts us.

    Sends SIGTERM (which main()'s handler turns into a graceful exit), waits
    out a grace period, then force-exits if the process is somehow still
    alive. Blocking — for thread contexts (the producer); async callers must
    use the inline os.kill + `await asyncio.sleep` sequence instead so the
    event loop keeps draining during the grace period.
    """
    logger.error(f"Terminating process: {reason}")
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(RetryConfig.GRACE_PERIOD_SECS)
    sys.exit(1)


# --------------------------------------------------------------------------- #
# ── Caches & runtime state                                                   #
# --------------------------------------------------------------------------- #

#  Filter-target key: thread id (int) or receiver key (str)
TargetKey = int | str


def _fingerprint(cfg: dict) -> str:
    """Deterministic (& cheap) fingerprint for a config dict."""
    return json.dumps(cfg, sort_keys=True)


class FilterRegistry:
    """Compiled-filter cache keyed by target, rebuilt only on config change.

    Compiling a CustomFilter means building regexes, so we fingerprint the
    raw config dict and reuse the compiled filter until the config differs.

    A config that fails to compile (e.g. an invalid regex smuggled in via
    setraw, bypassing command validation) is cached as None — logged once,
    and the target is skipped until its config changes. It must never crash
    the pipeline into a restart loop, because the bad config is persisted
    and would crash every restart too.
    """

    def __init__(self) -> None:
        self._cache: dict[TargetKey, tuple[str, CustomFilter | None]] = {}

    def get(self, key: TargetKey, cfg: dict) -> CustomFilter | None:
        fp = _fingerprint(cfg)
        cached = self._cache.get(key)
        if cached and cached[0] == fp:
            return cached[1]
        try:
            filt = CustomFilter(cfg)
        except Exception as e:
            logger.error(f"Invalid filter config for {key!r}; skipping this target until its config changes: {e}")
            filt = None
        self._cache[key] = (fp, filt)
        return filt

    def evict(self, key: TargetKey) -> None:
        self._cache.pop(key, None)

    def clear(self) -> None:
        self._cache.clear()


THREAD_FILTERS = FilterRegistry()
RECEIVER_FILTERS = FilterRegistry()

#  thread_id → discord.Thread
THREAD_CACHE: dict[int, discord.Thread] = {}

#  Track last event received time (for health monitoring)
_last_event_lock = threading.Lock()
last_event_time: float | None = None

#  Warn only once about events that carry no usable timestamp
_warned_missing_ts = False

#  Warn only once per receiver that is active but missing from FEZ_WEBHOOKS
_warned_webhookless_receivers: set[str] = set()


def _event_ts_to_epoch(change: dict) -> float | None:
    """
    Best-effort extraction of an event timestamp (UTC seconds).

    MediaWiki recentchange events *usually* include an integer `timestamp`
    (Unix epoch). Some other stream payloads (or schema bumps / edge cases)
    may omit it; in that case fall back to `meta.dt` (ISO 8601).

    Returns float seconds, or None if unavailable/unparseable.
    """
    ts = change.get("timestamp")
    if isinstance(ts, (int, float)):
        return float(ts)
    # Occasionally comes as str
    if isinstance(ts, str) and ts.isdigit():
        return float(ts)
    meta = change.get("meta") or {}
    dt_s = meta.get("dt")
    if dt_s:
        try:
            return datetime.fromisoformat(dt_s.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
    return None


def _is_stale(change: dict) -> bool:
    """True when the event is older than STALENESS_SECS. Events without a
    usable timestamp are treated as fresh (warned once, not per event)."""
    global _warned_missing_ts
    ts = _event_ts_to_epoch(change)
    if ts is None:
        if not _warned_missing_ts:
            logger.warning("Event without timestamp encountered; treating as fresh. Further warnings suppressed.")
            _warned_missing_ts = True
        return False
    return (datetime.now(UTC).timestamp() - ts) > STALENESS_SECS


# --------------------------------------------------------------------------- #
# ── Active-filter snapshots                                                  #
# --------------------------------------------------------------------------- #

async def active_custom_filters() -> list[tuple[int, CustomFilter]]:
    """(thread_id, CustomFilter) for **active** custom threads."""
    async with CONFIG_LOCK:
        snapshot = list(CONFIG["threads"].items())

    out: list[tuple[int, CustomFilter]] = []
    for tid_str, entry in snapshot:
        tid = int(tid_str)
        if not entry.get("active", False):
            THREAD_FILTERS.evict(tid)
            continue
        filt = THREAD_FILTERS.get(tid, entry["config"])
        if filt is not None:
            out.append((tid, filt))

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("Found %d active custom filters", len(out))
    return out


async def active_receiver_filters() -> list[tuple[str, CustomFilter]]:
    """(receiver_key, CustomFilter) for **active**, non-errored receivers
    that have a webhook URL configured."""
    async with CONFIG_LOCK:
        snapshot = list(CONFIG.get("receivers", {}).items())

    out: list[tuple[str, CustomFilter]] = []
    for key, entry in snapshot:
        if not entry.get("active", False) or entry.get("errored", False):
            RECEIVER_FILTERS.evict(key)
            continue
        if key not in WEBHOOKS:
            # Warn once per key, not once per firehose event.
            if key not in _warned_webhookless_receivers:
                _warned_webhookless_receivers.add(key)
                logger.warning(f"Receiver '{key}' active but no webhook URL in env; further warnings suppressed")
            continue
        filt = RECEIVER_FILTERS.get(key, entry["config"])
        if filt is not None:
            out.append((key, filt))

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("Found %d active receiver filters", len(out))
    return out


# --------------------------------------------------------------------------- #
# ── Operator notices & dead-thread handling                                  #
# --------------------------------------------------------------------------- #

async def _announce(channel: discord.TextChannel | None, text: str) -> None:
    """Best-effort post of an operator notice to the main channel; no-op when no
    channel is available. (send_message_with_backoff swallows its own errors.)"""
    if channel is not None:
        await send_message_with_backoff(channel, text)


async def _deactivate_dead_thread(
    tid: int, exc: Exception, channel: discord.TextChannel | None
) -> None:
    """Handle a custom thread that is permanently unsendable (deleted / access
    revoked): deactivate it and announce, so the silent stop is noticed. Stays
    quiet if `tid` isn't a tracked custom thread."""
    reason = (
        "thread not found (deleted?)"
        if isinstance(exc, discord.NotFound)
        else "access forbidden"
    )
    if await set_custom_thread_active(str(tid), False):
        logger.error(f"Custom thread {tid} permanently unsendable ({reason}); deactivated")
        await _announce(channel, format_thread_disabled_notice(tid, reason))


async def get_thread_obj(tid: int, channel: discord.TextChannel | None) -> discord.Thread | None:
    """Return cached discord.Thread or fetch & cache it."""
    th = THREAD_CACHE.get(tid)
    if th is not None:
        return th
    try:
        ch = await discord_api_call_with_backoff(bot.fetch_channel, tid)
    except (discord.NotFound, discord.Forbidden) as e:
        THREAD_CACHE.pop(tid, None)
        await _deactivate_dead_thread(tid, e, channel)
        return None
    except discord.HTTPException as e:
        logger.warning(f"Failed to fetch channel {tid}: {e}")
        return None
    except Exception as e:
        logger.warning(f"Unexpected error fetching channel {tid}: {e}")
        return None
    if isinstance(ch, discord.Thread):
        THREAD_CACHE[tid] = ch
        return ch
    return None


# --------------------------------------------------------------------------- #
# ── Health monitor                                                           #
# --------------------------------------------------------------------------- #

async def eventstream_health_monitor(channel: discord.TextChannel | None = None):
    """
    Monitor the health of the EventStream connection.
    If no events are received within EventStreamConfig.TIMEOUT_SECS, assume the
    connection is dead and terminate the process for external restart.

    If a channel is supplied, a notice is posted there before the restart so the
    outage is visible (this path runs in the async loop, so the send can flush).
    """
    logger.info(
        f"EventStream health monitor started (timeout: {EventStreamConfig.TIMEOUT_SECS}s, "
        f"check interval: {EventStreamConfig.CHECK_INTERVAL_SECS}s)"
    )

    # Give the stream some time to initialize before monitoring
    await asyncio.sleep(EventStreamConfig.CHECK_INTERVAL_SECS)

    while True:
        await asyncio.sleep(EventStreamConfig.CHECK_INTERVAL_SECS)

        with _last_event_lock:
            snapshot = last_event_time

        if snapshot is None:
            logger.warning("EventStream health check: No events received yet")
            continue

        time_since_last_event = time.time() - snapshot

        if time_since_last_event > EventStreamConfig.TIMEOUT_SECS:
            # Log at detection time, before the (possibly slow, retrying)
            # Discord announce — the stall must reach the log even if Discord
            # is degraded at the same moment.
            logger.error(
                f"EventStream TIMEOUT: No events received for {time_since_last_event:.0f}s "
                f"(threshold: {EventStreamConfig.TIMEOUT_SECS}s). Terminating process for restart."
            )
            # Announce before dying (best-effort; runs in the async loop so the
            # send can complete before SIGTERM tears the loop down).
            await _announce(channel, format_eventstream_timeout_notice(time_since_last_event))
            # Async flavor of terminate_process: the grace period must not
            # block the event loop, so in-flight sends can flush while
            # SIGTERM teardown proceeds.
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(RetryConfig.GRACE_PERIOD_SECS)
            sys.exit(1)  # Force exit if SIGTERM didn't work


# --------------------------------------------------------------------------- #
# ── Producer (daemon thread)                                                 #
# --------------------------------------------------------------------------- #

def producer_loop(loop: asyncio.AbstractEventLoop, event_queue: "asyncio.Queue[dict]") -> None:
    """
    Blocking producer: iterates EventStreams and enqueues events onto the
    asyncio queue, with exponential backoff and reconnection on errors.

    Runs in a daemon thread so it cannot keep the process alive after the
    event loop closes (e.g. during redeploys); every sleep/enqueue path
    re-checks loop.is_closed() before touching the loop.

    Updates the module-level last_event_time heartbeat on each event.
    """
    global last_event_time
    retry_backoff_seconds = RetryConfig.INITIAL_BACKOFF_SECS
    max_retry_backoff_seconds = RetryConfig.MAX_BACKOFF_SECS
    stream: EventStreams | None = None
    while True:
        try:
            if stream is None:
                stream = new_stream()
            for evt in stream:
                retry_backoff_seconds = RetryConfig.INITIAL_BACKOFF_SECS  # reset on success

                # Update heartbeat timestamp
                with _last_event_lock:
                    last_event_time = time.time()

                # Put event in queue with timeout to detect blocking
                if loop.is_closed():
                    logger.info("Event loop closed; producer thread exiting")
                    return
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        asyncio.wait_for(event_queue.put(evt), timeout=EventStreamConfig.QUEUE_PUT_TIMEOUT_SECS),
                        loop
                    )
                    future.result(timeout=EventStreamConfig.QUEUE_RESULT_TIMEOUT_SECS)
                except concurrent.futures.TimeoutError:
                    logger.error(
                        "Queue.put() timed out - queue may be blocked. "
                        f"Queue size: {event_queue.qsize()}/{event_queue.maxsize}"
                    )
                    # Critical failure: the processing pipeline is stuck.
                    terminate_process("blocked event queue")
                except Exception as e:
                    if loop.is_closed():
                        logger.info("Event loop closed; producer thread exiting")
                        return
                    logger.error(f"Failed to enqueue event: {e}")
            # Iterator ended unexpectedly; reinit at the live tail.
            # (No `since=` — see note on new_stream().)
            logger.warning("EventStreams iterator ended; reinitializing")
            stream = new_stream()
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            body = None
            try:
                if getattr(e, "response", None) is not None:
                    body = e.response.text
            except Exception:
                body = None
            if status == 403:
                logger.error("EventStreams HTTP 403. Body:\n%s", (body or "(no body)")[:EventStreamConfig.ERROR_BODY_LIMIT])
            else:
                logger.error("EventStreams HTTP error %s. Body:\n%s", status, (body or "(no body)")[:EventStreamConfig.ERROR_BODY_LIMIT])
            logger.info("Retrying after %ds", retry_backoff_seconds)
            time.sleep(retry_backoff_seconds)
            if loop.is_closed():
                logger.info("Event loop closed; producer thread exiting")
                return
            retry_backoff_seconds = min(retry_backoff_seconds * 2, max_retry_backoff_seconds)
            # Reinitialize at the live tail. (No `since=` — see new_stream().)
            try:
                stream = new_stream()
            except Exception as e2:
                logger.exception("Reinit after HTTP error failed: %r", e2)
                terminate_process("unable to reinitialize EventStream after HTTP error")
        except Exception as e:
            logger.exception("EventStreams error: %r; retrying in %ds", e, retry_backoff_seconds)
            time.sleep(retry_backoff_seconds)
            if loop.is_closed():
                logger.info("Event loop closed; producer thread exiting")
                return
            retry_backoff_seconds = min(retry_backoff_seconds * 2, max_retry_backoff_seconds)
            # If backoff has reached max, we're in a persistent failure state
            if retry_backoff_seconds >= max_retry_backoff_seconds:
                terminate_process(
                    f"EventStream failing persistently (backoff reached {max_retry_backoff_seconds}s)"
                )


# --------------------------------------------------------------------------- #
# ── Consumer                                                                 #
# --------------------------------------------------------------------------- #

async def _dispatch(
    change: dict,
    targets: list[tuple[int, discord.abc.Messageable, str]],
    receiver_targets: list[tuple[str, str]],
    channel: discord.TextChannel | None,
) -> None:
    """Send one event to its matched thread and webhook targets."""
    # Format at most once per link style.
    msg_cache: dict[str, str] = {}

    def _get_msg(style: str) -> str:
        if style not in msg_cache:
            msg_cache[style] = truncate_for_discord(format_change(change, link_style=style))
        return msg_cache[style]

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "Sending change to %d thread(s) and %d receiver(s)",
            len(targets),
            len(receiver_targets),
        )

    for tid, tgt, style in targets:
        sent = await send_message_with_backoff(tgt, _get_msg(style))
        if sent is None:
            # Send to a cached thread failed (deleted, access revoked, or a
            # transient blip). Evict it so the next matching event misses the
            # cache and re-runs get_thread_obj's classification: a successful
            # refetch means it was transient (re-cached, no harm); a
            # NotFound/Forbidden deactivates the thread and announces it.
            # Without this, a thread that goes permanently unsendable while
            # cached would drop every event silently.
            THREAD_CACHE.pop(tid, None)

    for key, style in receiver_targets:
        webhook_url = WEBHOOKS.get(key)
        if not webhook_url:
            logger.warning(f"Receiver '{key}' matched but no webhook URL")
            continue
        try:
            await send_webhook_with_backoff(webhook_url, _get_msg(style), key)
        except WebhookError as e:
            # Permanent failure (404/403/401/bad URL): disable the receiver
            # so it stops trying a webhook that cannot succeed, and announce
            # it in the main channel so the silent death gets noticed.
            logger.error(f"Webhook error for receiver '{key}': {e}")
            await set_receiver_errored(key)
            await _announce(channel, format_receiver_disabled_notice(key, str(e)))
        except TransientWebhookError as e:
            # Recoverable failure (5xx/network/timeout): drop this one event
            # but keep the receiver active so a passing hiccup cannot silence
            # it indefinitely. (Already logged at WARNING in the sender.)
            logger.debug(f"Transient webhook failure for receiver '{key}': {e}")


async def process_event(change: dict, channel: discord.TextChannel | None) -> None:
    """Route one event: staleness gate → pure matching → dispatch.

    Public: this is the consumer's per-event entry point and the seam the
    pipeline tests drive.
    """
    if _is_stale(change):
        return
    # Some events may lack user (rare for rc, common for revision-create);
    # nothing sensible to route.
    if not change.get("user"):
        return

    customs = await active_custom_filters()
    receivers = await active_receiver_filters()

    # Ultra-cheap global short-circuit before any regex-heavy work.
    if fast_reject(change, [f for _, f in customs] + [f for _, f in receivers]):
        return

    # The thread_id is carried so a failed send can evict the cached thread
    # object (see _dispatch).
    targets: list[tuple[int, discord.abc.Messageable, str]] = []
    for tid, style in match_filters(change, customs, "thread"):
        th = await get_thread_obj(tid, channel)
        if th is not None:
            targets.append((tid, th, style))

    receiver_targets = match_filters(change, receivers, "receiver")

    if targets or receiver_targets:
        await _dispatch(change, targets, receiver_targets, channel)


async def stream_worker(channel: discord.TextChannel):
    """
    Background task: start the producer thread and route each incoming
    Wikimedia event to matching custom threads and webhook receivers.
    """
    logger.info("Starting EventStreams worker")
    loop = asyncio.get_running_loop()

    # A bounded queue provides back-pressure if Discord throttles us.
    event_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=EventStreamConfig.QUEUE_MAX_SIZE)

    threading.Thread(
        target=producer_loop,
        args=(loop, event_queue),
        daemon=True,
        name="eventstream-producer",
    ).start()

    while True:
        change = await event_queue.get()
        try:
            await process_event(change, channel)
        except Exception:
            # One malformed event must not kill the consumer — the producer
            # would keep filling the queue until back-pressure suicides the
            # whole process. Drop the event and keep routing.
            logger.exception("Unhandled error processing event; event dropped")
        finally:
            event_queue.task_done()
