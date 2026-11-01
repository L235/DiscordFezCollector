import asyncio
import concurrent.futures
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import discord
import requests
from pywikibot import config as pwb_config
from pywikibot.comms.eventstreams import EventStreams

from src.constants import (
    DISCORD_MESSAGE_LIMIT,
    TRUNCATION_RESERVE,
    TRUNCATED_MESSAGE_SUFFIX,
)
from src.logging_setup import logger
from src.models import EventStreamConfig, RetryConfig
from src import config as config_state
from src.delivery import DestinationDispatcher
from src.routing import RoutingPlanCache
from src.config import (
    CONFIG,
    WEBHOOKS,
    USER_AGENT,
    STALENESS_SECS,
    set_receiver_errored,
    set_custom_thread_active,
)
from src.discord_utils import (
    discord_api_call_with_backoff,
    send_message_with_backoff,
    send_webhook_with_backoff,
    WebhookError,
    TransientWebhookError,
)
from src.bot_instance import bot

# --------------------------------------------------------------------------- #
# ── MediaWiki EventStreams setup                                             #
# --------------------------------------------------------------------------- #

pwb_config.user_agent_description = USER_AGENT

# NOTE: We deliberately do *not* pass `since=`. Supplying a `since` timestamp
# forces stream.wikimedia.org to perform a Kafka offset seek, which
# intermittently returns HTTP 500 (~20-25% of connections, reproduced
# directly). Since we always reconnected with `since=now` anyway, the seek
# bought nothing over the live tail while causing the 500s. Omitting `since`
# connects at the live tail, which never 500s. Brief reconnect gaps (a few
# seconds, a few times a day) are well within the STALENESS_SECS tolerance.
# Full analysis: https://github.com/L235/DiscordFezCollector/issues/46
stream = EventStreams(
    streams=["recentchange", "revision-create"],
    headers={"user-agent": USER_AGENT},
)

# --------------------------------------------------------------------------- #
# ── Caches & runtime state                                                   #
# --------------------------------------------------------------------------- #

ROUTING_CACHE = RoutingPlanCache()
#  thread_id → discord.Thread
THREAD_CACHE: Dict[int, discord.Thread] = {}
#  Track last event received time (for health monitoring)
_last_event_lock = threading.Lock()
last_event_time: Optional[float] = None

async def get_routing_plan():
    # _config_edit publishes state and revision synchronously on this loop.
    # Do not acquire its edit lock: it spans potentially slow storage I/O.
    return ROUTING_CACHE.get(CONFIG, WEBHOOKS, config_state.CONFIG_REVISION)


def _event_ts_to_epoch(change: dict) -> Optional[float]:
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

def format_change(change: dict, *, link_style: str = "title") -> str:
    """Turn one EventStreams record into a concise Discord message.

    Args:
        change: The EventStreams change record.
        link_style: "title" to link the page title (default),
                    "action" to link the action verb instead.
    """
    user = change['user']
    title = change.get("title", "(no title)")
    comment = change.get("comment") or "(no summary)"
    
    if change["type"] == "log":
        log_comment = change.get("log_action_comment") or comment
        link = f"https://{change.get('server_name','')}/w/index.php?title=Special:Log&logid={change.get('log_id','')}"

        # Better linking for AbuseFilter hits
        if change.get("log_type") == "abusefilter" and change.get("log_action") == "hit":
            log_params = change.get("log_params")
            if isinstance(log_params, dict) and "log" in log_params:
                link = f"https://{change.get('server_name','')}/wiki/Special:AbuseLog/{log_params['log']}"

        return f"**{user}** {log_comment} \n<{link}>"

    # For regular edits
    page_url = f"https://{change['server_name']}/wiki/{title.replace(' ', '_')}"
    
    # Handle different change types that may or may not have revision info
    if change["type"] == "edit" and "revision" in change:
        diff_url = f"https://{change['server_name']}/w/index.php?diff={change['revision']['new']}"
        if link_style == "action":
            return f"**{user}** [edited](<{diff_url}>) **[{title}](<{page_url}>)** ({comment})"
        return f"**{user}** edited **[{title}](<{page_url}>)** ({comment}) \n<{diff_url}>"
    elif change["type"] == "new":
        if link_style == "action":
            return f"**{user}** [created](<{page_url}>) **{title}** ({comment})"
        return f"**{user}** created **[{title}](<{page_url}>)** ({comment})"
    elif change["type"] == "categorize":
        if link_style == "action":
            return f"**{user}** [categorized](<{page_url}>) **{title}** ({comment})"
        return f"**{user}** categorized **[{title}](<{page_url}>)** ({comment})"
    else:
        # Fallback for other change types
        if link_style == "action":
            return f"**{user}** [modified](<{page_url}>) **{title}** ({comment})"
        return f"**{user}** modified **[{title}](<{page_url}>)** ({comment})"

def format_receiver_disabled_notice(key: str, reason: str) -> str:
    """Build the main-channel notice posted when a receiver is auto-disabled.

    Surfaces the silent failure: which receiver, why, and how to recover — so a
    permanently-broken webhook is noticed instead of dying quietly.
    """
    return (
        f"⚠️ Receiver **{key}** was automatically disabled after a webhook "
        f"failure: {reason}\nReactivate with `/receiver activate {key}` once the "
        f"webhook is fixed."
    )


def format_thread_disabled_notice(thread_id: int, reason: str) -> str:
    """Build the main-channel notice posted when a custom thread is auto-disabled
    because it became permanently unsendable (deleted / no access)."""
    return (
        f"⚠️ Custom thread <#{thread_id}> (`{thread_id}`) was automatically "
        f"deactivated: {reason}\nRecreate the thread (or restore access) to "
        f"resume routing."
    )


def format_startup_notice() -> str:
    """Build the main-channel notice posted once when the bot (re)starts."""
    return "✅ **DiscordFezCollector** started and connected to Wikimedia EventStreams."


def format_eventstream_timeout_notice(seconds: float) -> str:
    """Build the main-channel notice posted before the process restarts itself
    because the Wikimedia stream went silent."""
    return (
        f"⚠️ No Wikimedia events received for {int(seconds)}s — the stream looks "
        f"stalled; restarting the bot to recover."
    )


async def _announce(channel: Optional[discord.TextChannel], text: str) -> None:
    """Best-effort post of an operator notice to the main channel; no-op when no
    channel is available. (send_message_with_backoff swallows its own errors.)"""
    if channel is not None:
        await send_message_with_backoff(channel, text)


async def _deactivate_dead_thread(
    tid: int, exc: Exception, channel: Optional[discord.TextChannel]
) -> None:
    """Handle a custom thread that is permanently unsendable (deleted / access
    revoked): deactivate it and announce, so the silent stop is noticed. Stays
    quiet if `tid` isn't a tracked custom thread."""
    reason = (
        "thread not found (deleted?)"
        if isinstance(exc, discord.NotFound)
        else "access forbidden"
    )
    if await set_custom_thread_active(str(tid), False, best_effort=True):
        logger.error(f"Custom thread {tid} permanently unsendable ({reason}); deactivated")
        await _announce(channel, format_thread_disabled_notice(tid, reason))

async def eventstream_health_monitor(channel: Optional[discord.TextChannel] = None):
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
            logger.error(
                f"EventStream TIMEOUT: No events received for {time_since_last_event:.0f}s "
                f"(threshold: {EventStreamConfig.TIMEOUT_SECS}s). Terminating process for restart."
            )
            # Announce before dying (best-effort; runs in the async loop so the
            # send can complete before SIGTERM tears the loop down).
            await _announce(channel, format_eventstream_timeout_notice(time_since_last_event))
            # Terminate the process - external process manager should restart it
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(RetryConfig.GRACE_PERIOD_SECS)  # Grace period
            sys.exit(1)  # Force exit if SIGTERM didn't work

async def stream_worker(channel: discord.TextChannel):
    """
    Background task: tail MediaWiki EventStreams and route posts to **active
    custom threads**.  (Per-user routing removed in v0.7.)
    """
    logger.info("Starting EventStreams worker")
    loop = asyncio.get_running_loop()

    async def get_thread_obj(tid: int) -> Optional[discord.Thread]:
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

    async def deliver(destination, message):
        kind, key = destination
        # This synchronous read sees only the last atomically published state.
        entity_type = "threads" if kind == "thread" else "receivers"
        entry = CONFIG.get(entity_type, {}).get(str(key))
        if not entry or not entry.get("active", False) or entry.get("errored", False):
            return
        if kind == "thread":
            target = await get_thread_obj(key)
            if target is not None:
                sent = await send_message_with_backoff(target, message)
                if sent is None:
                    THREAD_CACHE.pop(key, None)
            return
        webhook_url = WEBHOOKS.get(key)
        if not webhook_url:
            return
        try:
            await send_webhook_with_backoff(webhook_url, message, key)
        except WebhookError as exc:
            logger.error("Webhook error for receiver '%s': %s", key, exc)
            await set_receiver_errored(key)
            await _announce(channel, format_receiver_disabled_notice(key, str(exc)))
        except TransientWebhookError:
            logger.debug("Transient webhook failure for receiver '%s': event dropped", key)

    dispatcher = DestinationDispatcher(
        deliver, max_queue=EventStreamConfig.DESTINATION_QUEUE_MAX_SIZE
    )

    # ------------------------------------------------------------------ #
    # Consume the blocking EventStreams iterator in a thread-pool so the
    # asyncio event-loop never stalls on network reads.  A bounded queue
    # provides back-pressure if Discord throttles us.
    # ------------------------------------------------------------------ #
    event_queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=EventStreamConfig.QUEUE_MAX_SIZE)

    def _producer():
        """
        Blocking producer: iterates EventStreams and enqueues messages.
        Adds simple exponential backoff on errors and logs 403 bodies.
        
        This runs in a daemon thread to avoid blocking the async event loop.
        Updates global last_event_time on each successful event.
        """
        retry_backoff_seconds = RetryConfig.INITIAL_BACKOFF_SECS
        max_retry_backoff_seconds = RetryConfig.MAX_BACKOFF_SECS
        global stream
        while True:
            try:
                for evt in stream:
                    global last_event_time
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
                        # This is a critical failure - the processing pipeline is stuck
                        logger.error("Terminating process due to blocked queue")
                        os.kill(os.getpid(), signal.SIGTERM)
                        time.sleep(RetryConfig.GRACE_PERIOD_SECS)
                        sys.exit(1)
                    except Exception as e:
                        if loop.is_closed():
                            logger.info("Event loop closed; producer thread exiting")
                            return
                        logger.error(f"Failed to enqueue event: {e}")
                # Iterator ended unexpectedly; reinit at the live tail.
                # (No `since=` — see note at module-level stream construction.)
                logger.warning("EventStreams iterator ended; reinitializing")
                stream = EventStreams(
                    streams=["recentchange", "revision-create"],
                    headers={"user-agent": USER_AGENT},
                )
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
                # Reinitialize at the live tail.
                # (No `since=` — see note at module-level stream construction.)
                try:
                    stream = EventStreams(
                        streams=["recentchange", "revision-create"],
                        headers={"user-agent": USER_AGENT},
                    )
                except Exception as e2:
                    logger.exception("Reinit after HTTP error failed: %r", e2)
                    # If we can't reinitialize, the stream is broken
                    logger.error("Unable to reinitialize EventStream after HTTP error. Terminating.")
                    os.kill(os.getpid(), signal.SIGTERM)
                    time.sleep(RetryConfig.GRACE_PERIOD_SECS)
                    sys.exit(1)
            except Exception as e:
                logger.exception("EventStreams error: %r; retrying in %ds", e, retry_backoff_seconds)
                time.sleep(retry_backoff_seconds)
                if loop.is_closed():
                    logger.info("Event loop closed; producer thread exiting")
                    return
                retry_backoff_seconds = min(retry_backoff_seconds * 2, max_retry_backoff_seconds)
                # If backoff has reached max, we're in a persistent failure state
                if retry_backoff_seconds >= max_retry_backoff_seconds:
                    logger.error(
                        f"EventStream has been failing for extended period "
                        f"(backoff reached {max_retry_backoff_seconds}s). Terminating process."
                    )
                    os.kill(os.getpid(), signal.SIGTERM)
                    time.sleep(RetryConfig.GRACE_PERIOD_SECS)
                    sys.exit(1)

    # Use a daemon thread so the producer cannot keep the process alive
    # after the event loop closes (e.g. during redeploys).
    t = threading.Thread(target=_producer, daemon=True, name="eventstream-producer")
    t.start()

    try:
        while True:
            change = await event_queue.get()
            try:
                ts = _event_ts_to_epoch(change)
                if ts is None:
                    if not getattr(stream_worker, "_warned_missing_ts", False):
                        logger.warning("Event without timestamp encountered; treating as fresh. Further warnings suppressed.")
                        setattr(stream_worker, "_warned_missing_ts", True)
                elif time.time() - ts > STALENESS_SECS:
                    continue
                if not change.get("user"):
                    continue

                plan = await get_routing_plan()
                messages = {}
                for kind, key, style in plan.route(change):
                    if style not in messages:
                        message = format_change(change, link_style=style)
                        if len(message) > DISCORD_MESSAGE_LIMIT:
                            message = message[:DISCORD_MESSAGE_LIMIT - TRUNCATION_RESERVE] + TRUNCATED_MESSAGE_SUFFIX
                        messages[style] = message
                    dispatcher.submit((kind, key), messages[style])
            finally:
                event_queue.task_done()
    finally:
        await dispatcher.close()
