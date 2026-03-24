import asyncio
import concurrent.futures
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import discord
import requests
from pywikibot import Site
from pywikibot import config as pwb_config
from pywikibot.comms.eventstreams import EventStreams

from src.constants import (
    ISO_TIMESTAMP_FORMAT,
    DISCORD_MESSAGE_LIMIT,
    TRUNCATION_RESERVE,
    TRUNCATED_MESSAGE_SUFFIX,
)
from src.logging_setup import logger
from src.models import EventStreamConfig, RetryConfig, CustomFilter
from src.config import (
    CONFIG,
    CONFIG_LOCK,
    DISCORD_CHANNEL_IDS,
    WEBHOOKS,
    USER_AGENT,
    STALENESS_SECS,
    set_receiver_errored,
)
from src.discord_utils import (
    discord_api_call_with_backoff,
    send_message_with_backoff,
    send_webhook_with_backoff,
    WebhookError,
)
from src.bot_instance import bot

# --------------------------------------------------------------------------- #
# ── MediaWiki EventStreams setup                                             #
# --------------------------------------------------------------------------- #

site = Site()  # default site; EventStreams ignores this for global streams
pwb_config.user_agent_description = USER_AGENT

# EventStreams requires `since=` as Unix-ms epoch or ISO-8601 timestamp
# *without* microseconds and without "+" in TZ (would decode as whitespace).
_NOW_UTC_ISO = datetime.now(timezone.utc).replace(microsecond=0).strftime(ISO_TIMESTAMP_FORMAT)

stream = EventStreams(
    streams=["recentchange", "revision-create"],
    since=_NOW_UTC_ISO,
    headers={"user-agent": USER_AGENT},
)

# --------------------------------------------------------------------------- #
# ── Caches & runtime state                                                   #
# --------------------------------------------------------------------------- #

#  thread_id → (fingerprint, CustomFilter)
FILTER_CACHE: Dict[int, Tuple[str, CustomFilter]] = {}
#  thread_id → discord.Thread
THREAD_CACHE: Dict[int, discord.Thread] = {}
#  receiver_key → (fingerprint, CustomFilter)
RECEIVER_FILTER_CACHE: Dict[str, Tuple[str, CustomFilter]] = {}
#  Track last event received time (for health monitoring)
_last_event_lock = threading.Lock()
last_event_time: Optional[float] = None

def _fingerprint(cfg: dict) -> str:
    """Deterministic (& cheap) fingerprint for a config dict."""
    return json.dumps(cfg, sort_keys=True)

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

async def eventstream_health_monitor():
    """
    Monitor the health of the EventStream connection.
    If no events are received within EventStreamConfig.TIMEOUT_SECS, assume the
    connection is dead and terminate the process for external restart.
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

    async def active_custom_filters() -> List[Tuple[int, CustomFilter]]:
        """
        Return list of (thread_id, CustomFilter) for **active** custom threads,
        rebuilding a filter only if its config actually changed.
        """
        async with CONFIG_LOCK:
            snapshot = list(CONFIG["threads"].items())

        out: List[Tuple[int, CustomFilter]] = []
        for tid_str, entry in snapshot:
            if not entry.get("active", False):
                FILTER_CACHE.pop(int(tid_str), None)
                continue
            tid = int(tid_str)
            fp = _fingerprint(entry["config"])
            cached = FILTER_CACHE.get(tid)
            if cached and cached[0] == fp:
                filt = cached[1]
            else:
                filt = CustomFilter(entry["config"])
                FILTER_CACHE[tid] = (fp, filt)
            out.append((tid, filt))

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Found %d active custom filters", len(out))
        return out

    async def active_receiver_filters() -> List[Tuple[str, CustomFilter]]:
        """
        Return list of (receiver_key, CustomFilter) for **active** receivers,
        rebuilding a filter only if its config actually changed.
        """
        async with CONFIG_LOCK:
            snapshot = list(CONFIG.get("receivers", {}).items())

        out: List[Tuple[str, CustomFilter]] = []
        for key, entry in snapshot:
            # Skip inactive or errored receivers, or those without webhook URLs
            if not entry.get("active", False) or entry.get("errored", False):
                RECEIVER_FILTER_CACHE.pop(key, None)
                continue
            if key not in WEBHOOKS:
                logger.warning(f"Receiver '{key}' active but no webhook URL in env")
                continue
            fp = _fingerprint(entry["config"])
            cached = RECEIVER_FILTER_CACHE.get(key)
            if cached and cached[0] == fp:
                filt = cached[1]
            else:
                filt = CustomFilter(entry["config"])
                RECEIVER_FILTER_CACHE[key] = (fp, filt)
            out.append((key, filt))

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Found %d active receiver filters", len(out))
        return out

    async def get_thread_obj(tid: int) -> Optional[discord.Thread]:
        """Return cached discord.Thread or fetch & cache it."""
        th = THREAD_CACHE.get(tid)
        if th is not None:
            return th
        try:
            ch = await discord_api_call_with_backoff(bot.fetch_channel, tid)
        except (discord.NotFound, discord.Forbidden):
            THREAD_CACHE.pop(tid, None)
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
                # Iterator ended unexpectedly; reinit.
                logger.warning("EventStreams iterator ended; reinitializing")
                event_stream_since_timestamp = datetime.now(timezone.utc).replace(microsecond=0).strftime(ISO_TIMESTAMP_FORMAT)
                stream = EventStreams(
                    streams=["recentchange", "revision-create"],
                    since=event_stream_since_timestamp,
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
                # Reinitialize with fresh since=now
                try:
                    event_stream_since_timestamp = datetime.now(timezone.utc).replace(microsecond=0).strftime(ISO_TIMESTAMP_FORMAT)
                    stream = EventStreams(
                        streams=["recentchange", "revision-create"],
                        since=event_stream_since_timestamp,
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

    while True:
        change = await event_queue.get()

        # Robust timestamp handling
        ts = _event_ts_to_epoch(change)
        if ts is None:
            # If we cannot determine freshness, treat as *not stale* but log once.
            # To avoid log spam, gate on an attribute.
            if not getattr(stream_worker, "_warned_missing_ts", False):
                logger.warning("Event without timestamp encountered; treating as fresh. Further warnings suppressed.")
                setattr(stream_worker, "_warned_missing_ts", True)
            stale = False
        else:
            current_time = datetime.now(timezone.utc).timestamp()
            stale = (current_time - ts) > STALENESS_SECS

        # Some events may lack user (rare for rc, common for revision-create)
        user = change.get("user")
        if stale:
            event_queue.task_done()
            continue
        if not user:
            # Nothing sensible to route; skip quietly.
            event_queue.task_done()
            continue

        # Determine routing targets: (messageable, link_style) tuples
        targets: List[Tuple[discord.abc.Messageable, str]] = []
        receiver_targets: List[Tuple[str, str]] = []  # (key, link_style)

        # Custom threads and receivers
        customs = await active_custom_filters()
        receivers = await active_receiver_filters()

        # ----------  (#2) ultra-cheap global short-circuit ---------------- #
        # Combine filters from both threads and receivers for fast-reject
        all_filters = [f for _, f in customs] + [f for _, f in receivers]
        user_whitelist = {u for f in all_filters for u in f.user_include}
        page_regexes = [f.page_include for f in all_filters if f.page_include]
        summary_regexes = [f.sum_include for f in all_filters if f.sum_include]

        title = change.get("title", "")
        comment = change.get("log_action_comment") or change.get("comment") or ""

        if (
            change["user"] not in user_whitelist
            and not any(rx.search(title) for rx in page_regexes)
            and not any(rx.search(comment) for rx in summary_regexes)
        ):
            event_queue.task_done()
            continue  # fast reject before any regex-heavy work

        # Match against thread filters
        if customs:
            for tid, filt in customs:
                try:
                    if filt.matches(change):
                        th = await get_thread_obj(tid)
                        if th is not None:
                            targets.append((th, filt.link_style))
                except Exception as e:  # pragma: no cover
                    logger.warning(f"Custom filter error for {tid}: {e}")

        # Match against receiver filters
        if receivers:
            for key, filt in receivers:
                try:
                    if filt.matches(change):
                        receiver_targets.append((key, filt.link_style))
                except Exception as e:
                    logger.warning(f"Receiver filter error for '{key}': {e}")

        if not targets and not receiver_targets:
            event_queue.task_done()
            continue  # nothing to do

        # Cache formatted messages by link_style to avoid re-formatting
        _msg_cache: Dict[str, str] = {}
        def _get_msg(style: str, _change: dict = change) -> str:
            if style not in _msg_cache:
                msg = format_change(_change, link_style=style)
                if len(msg) > DISCORD_MESSAGE_LIMIT:
                    msg = msg[:DISCORD_MESSAGE_LIMIT - TRUNCATION_RESERVE] + TRUNCATED_MESSAGE_SUFFIX
                _msg_cache[style] = msg
            return _msg_cache[style]

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

        # Send to receiver webhooks (uses discord.py native webhook support)
        for key, style in receiver_targets:
            webhook_url = WEBHOOKS.get(key)
            if not webhook_url:
                logger.warning(f"Receiver '{key}' matched but no webhook URL")
                continue
            try:
                await send_webhook_with_backoff(webhook_url, _get_msg(style), key)
            except WebhookError as e:
                logger.error(f"Webhook error for receiver '{key}': {e}")
                # Mark receiver as errored so it stops receiving
                await set_receiver_errored(key)

        event_queue.task_done()