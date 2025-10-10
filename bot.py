#!/usr/bin/env python3
"""
fez_collector - Discord edition
--------------------------------
* Monitors MediaWiki EventStreams and posts changes to Discord.
* Reads **and** persists state (config) to a JSON file whose location is
  supplied in the `FEZ_COLLECTOR_STATE` environment variable.
* Lets authorised users update the config live from Discord **slash**
  commands ( `/...` ).  Legacy **bang** prefixes (`!...`) are retained for
  backward compatibility.

**Environment Variables:**
* `FEZ_COLLECTOR_DISCORD_TOKEN` - Discord bot token (required)
* `FEZ_COLLECTOR_CHANNEL_ID`   - Parent channel ID for threads (required)
* `USER_AGENT`                 - (optional) UA for Wikimedia requests. Default: "DiscordFezCollector/1.0 (https://github.com/L235/DiscordFezCollector; User:L235)"
* `FEZ_COLLECTOR_STATE`        - Path to config JSON file (default: "./state/config.json")

**Available Commands (preferred -> legacy):**
* `/ping`  (`!ping`) - Test bot responsiveness
* `/add <Username>`  (`!add`) - Create a "User:<Username>" thread **and** add the user to `userIncludeList`
* `/addcustom <name>`  (`!addcustom`) - Create a generic filter thread (parent channel only)
* `/globalconfig getraw`  - Download full configuration as a JSON attachment
* `/globalconfig setraw` - **Replace** the entire configuration from an attached JSON file (**dangerous**)
* `/activate` / `/deactivate` - Toggle activity for the *current thread*
* `/config [get]` - Show current thread configuration
* `/config set <key> <json>` - Set configuration value
* `/config getraw` - Download raw JSON for the current thread
* `/config setraw` - **Replace** the current thread configuration from an attached JSON file (**dangerous**)
* `/config add|remove|clear ...` - Mutate list-type configuration fields

**Config Schema (v0.8):**

      {
          "version": "0.8",
          "threads": {
              "<thread_id>": {
                  "name": "User:Username",       # display only
                  "active": true,
                  "config": {
                      "siteName": "",            # '' => any; e.g. "en.wikipedia.org"
                      "pageIncludePatterns": [],
                      "pageExcludePatterns": [],
                      "userExcludeList": [],
                      "userIncludeList": ["Username"],
                      "summaryIncludePatterns": [],
                      "summaryExcludePatterns": []
                  }
              }
          }
      }
"""
VERSION = "0.8-discord-harmonised"

import asyncio
import concurrent.futures
import json
import requests
import signal
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import re, io
from re import RegexFlag, compile, search
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands

from pywikibot import Site
from pywikibot import config as pwb_config
from pywikibot.comms.eventstreams import EventStreams

# --------------------------------------------------------------------------- #
# ── Logging setup                                                            #
# --------------------------------------------------------------------------- #

def setup_logging():
    """Configure logging with proper formatting and levels."""
    # Create logs directory if it doesn't exist
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    
    # Configure logging format
    log_format = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Set up root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(log_format)
    root_logger.addHandler(console_handler)
    
    # File handler for all logs
    file_handler = logging.FileHandler(log_dir / "fez_collector.log")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(log_format)
    root_logger.addHandler(file_handler)
    
    # File handler for errors only
    error_handler = logging.FileHandler(log_dir / "errors.log")
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(log_format)
    root_logger.addHandler(error_handler)
    
    return logging.getLogger(__name__)

# Initialize logging
logger = setup_logging()
logger.info(f"fez_collector {VERSION} initialising...")

# --------------------------------------------------------------------------- #
# ── Constants                                                                #
# --------------------------------------------------------------------------- #

# Common constants
CONFIG_VERSION = "0.8"
UTF8_ENCODING = "utf-8"
ISO_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
JSON_INDENT = 2

# Discord limits
DISCORD_MESSAGE_LIMIT = 2000
TRUNCATED_MESSAGE_SUFFIX = "..."
TRUNCATION_BUFFER = 10  # 1990 = 2000 - 10

# Timeout and retry configuration
class RetryConfig:
    INITIAL_BACKOFF_SECS = 5
    MAX_BACKOFF_SECS = 300
    GRACE_PERIOD_SECS = 5

# EventStream configuration
class EventStreamConfig:
    TIMEOUT_SECS = int(os.getenv("EVENTSTREAM_TIMEOUT_SECS", "600"))  # 10 minutes default
    CHECK_INTERVAL_SECS = 60  # Check every minute
    QUEUE_MAX_SIZE = 2000
    QUEUE_PUT_TIMEOUT_SECS = 30.0
    QUEUE_RESULT_TIMEOUT_SECS = 35.0  # Slightly longer than put timeout
    ERROR_BODY_LIMIT = 2000

# --------------------------------------------------------------------------- #
# ── Environment / runtime configuration                                      #
# --------------------------------------------------------------------------- #

DISCORD_TOKEN      = os.getenv("FEZ_COLLECTOR_DISCORD_TOKEN")
DISCORD_CHANNEL_ID = int(os.getenv("FEZ_COLLECTOR_CHANNEL_ID", "0"))  # numeric
STATE_FILE         = Path(os.getenv("FEZ_COLLECTOR_STATE", "./state/config.json"))
STALENESS_SECS     = 2 * 60 * 60  # two hours

# Track last event received time
last_event_time: Optional[float] = None
USER_AGENT         = os.getenv(
    "USER_AGENT",
    "DiscordFezCollector/1.0 (https://github.com/L235/DiscordFezCollector; User:L235)"
)

if not DISCORD_TOKEN or not DISCORD_CHANNEL_ID:
    logger.error("FEZ_COLLECTOR_DISCORD_TOKEN or FEZ_COLLECTOR_CHANNEL_ID missing")
    sys.exit("FEZ_COLLECTOR_DISCORD_TOKEN or FEZ_COLLECTOR_CHANNEL_ID missing")

# --------------------------------------------------------------------------- #
# ── Config management                                                        #
# --------------------------------------------------------------------------- #



DEFAULT_CUSTOM_CONFIG: Dict[str, Any] = {
    "siteName": "",
    "pageIncludePatterns": [],
    "pageExcludePatterns": [],
    "userExcludeList": [],
    "userIncludeList": [],
    "summaryIncludePatterns": [],
    "summaryExcludePatterns": [],
}

# Config schema (see docstring):
DEFAULT_CONFIG: Dict[str, Any] = {"version": CONFIG_VERSION, "threads": {}}


def load_config() -> dict:
    if not STATE_FILE.exists():
        logger.info(f"Creating new config file at {STATE_FILE}")
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    logger.debug(f"Loading config from {STATE_FILE}")
    with STATE_FILE.open(encoding=UTF8_ENCODING) as fp:
        raw = json.load(fp)
    # ensure key exists - older configs will silently keep working,
    # but we no longer mutate them in-place.
    raw.setdefault("threads", {})
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("Loaded config with %d threads", len(raw.get("threads", {})))
    return raw


def save_config(cfg: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding=UTF8_ENCODING) as fp:
        json.dump(cfg, fp, indent=JSON_INDENT, sort_keys=True)
    tmp.replace(STATE_FILE)
    logger.debug(f"Config saved to {STATE_FILE}")


CONFIG: Dict[str, Any] = load_config()
CONFIG_LOCK = asyncio.Lock()  # to prevent simultaneous writes

# --------------------------------------------------------------------------- #
# ── Custom thread helpers                                                    #
# --------------------------------------------------------------------------- #

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


def _blank_custom_cfg() -> dict:
    return json.loads(json.dumps(DEFAULT_CUSTOM_CONFIG))  # deep copy


async def ensure_custom_thread_entry(thread: discord.Thread, *, create_if_missing: bool = False) -> Optional[dict]:
    """
    Ensure CONFIG entry exists for given discord.Thread. Returns entry (dict) or None.
    """
    tid = str(thread.id)
    # Every thread (incl. "User:" ones) is now a first-class custom thread.

    async with CONFIG_LOCK:
        entry = CONFIG["threads"].get(tid)
        if entry is None and create_if_missing:
            logger.info(f"Creating new config entry for thread {thread.name} ({tid})")
            entry = {
                "name": thread.name,
                "active": True,
                "config": _blank_custom_cfg(),
            }
            CONFIG["threads"][tid] = entry
            save_config(CONFIG)
        return entry


async def set_custom_thread_active(thread_id: int, active: bool) -> bool:
    async with CONFIG_LOCK:
        entry = CONFIG["threads"].get(str(thread_id))
        if not entry:
            logger.warning(f"Attempted to set active state for unknown thread {thread_id}")
            return False
        old_state = entry.get("active", False)
        entry["active"] = active
        save_config(CONFIG)
        logger.info(f"Thread {thread_id} active state changed: {old_state} -> {active}")
        return True


async def update_custom_thread_config(thread_id: int, new_cfg: dict) -> bool:
    async with CONFIG_LOCK:
        entry = CONFIG["threads"].get(str(thread_id))
        if not entry:
            logger.warning(f"Attempted to update config for unknown thread {thread_id}")
            return False
        entry["config"] = new_cfg
        save_config(CONFIG)
        logger.info(f"Updated config for thread {thread_id}")
        return True


def _normalise_list_val(v: Any) -> List[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]


async def mutate_custom_thread_config_list(thread_id: int, key: str, *, add: Optional[List[str]] = None,
                                           remove: Optional[List[str]] = None, clear: bool = False) -> Tuple[bool, Optional[List[str]]]:
    """
    Mutate an array field of a custom thread config. Returns (ok, new_list|None).
    """
    async with CONFIG_LOCK:
        entry = CONFIG["threads"].get(str(thread_id))
        if not entry:
            logger.warning(f"Attempted to mutate config for unknown thread {thread_id}")
            return False, None
        cfg = entry["config"]
        if key not in cfg:
            # create list field
            cfg[key] = []
        if clear:
            logger.info(f"Clearing {key} for thread {thread_id}")
            cfg[key] = []
        else:
            lst = list(cfg[key])
            if add:
                logger.info(f"Adding {add} to {key} for thread {thread_id}")
                for it in add:
                    if it not in lst:
                        lst.append(it)
            if remove:
                logger.info(f"Removing {remove} from {key} for thread {thread_id}")
                lst = [it for it in lst if it not in remove]
            cfg[key] = lst
        save_config(CONFIG)
        return True, list(cfg[key])


def _compile_pattern_list(patterns: List[str]) -> Optional[re.Pattern]:
    """
    Accept list-ish input; coerce scalars to a single-element list.
    """
    if isinstance(patterns, str):
        patterns = [patterns]
    elif not isinstance(patterns, list):
        try:
            patterns = list(patterns)
        except Exception:
            patterns = [str(patterns)]
    patterns = [str(p) for p in patterns if p]
    if not patterns:
        return None
    return compile(f"({'|'.join(patterns)})", RegexFlag.IGNORECASE)


class CustomFilter:
    __slots__ = (
        "site_name",
        "page_include",
        "page_exclude",
        "sum_include",
        "sum_exclude",
        "user_include",
        "user_exclude",
    )

    def __init__(self, cfg: dict):
        self.site_name   = cfg.get("siteName", "") or ""
        self.page_include = _compile_pattern_list(cfg.get("pageIncludePatterns", []))
        self.page_exclude = _compile_pattern_list(cfg.get("pageExcludePatterns", []))
        self.sum_include  = _compile_pattern_list(cfg.get("summaryIncludePatterns", []))
        self.sum_exclude  = _compile_pattern_list(cfg.get("summaryExcludePatterns", []))
        self.user_include = set(cfg.get("userIncludeList", []))
        self.user_exclude = set(cfg.get("userExcludeList", []))

    def matches(self, change: dict) -> bool:
        user = change["user"]
        # Some EventStreams variants may lack 'title'; guard.
        title = change.get("title", "")
        comment = change.get("log_action_comment") or change.get("comment") or ""
        server = change.get("server_name", "")

        if self.site_name and self.site_name.lower() != server.lower():
            return False
        # If we require a siteName AND title is empty (rare), bail out.
        if self.site_name and not title:
            return False
        if user in self.user_exclude:
            return False
        if self.page_exclude and search(self.page_exclude, title):
            return False
        if self.sum_exclude and search(self.sum_exclude, comment):
            return False
        # include
        if user in self.user_include:
            return True
        if self.page_include and search(self.page_include, title):
            return True
        if self.sum_include and search(self.sum_include, comment):
            return True
        return False


# --------------------------------------------------------------------------- #
# ── MediaWiki EventStreams setup                                             #
# --------------------------------------------------------------------------- #

#
# We no longer restrict to a single wiki; collect everything and filter later.
# This will include non-English projects unless custom filters narrow scope.
#
site = Site()  # default site; EventStreams ignores this for global streams
# -------------------------------------------------------------------- #
# Set a descriptive Pywikibot UA.
pwb_config.user_agent_description = USER_AGENT

# -------------------------------------------------------------------- #
# EventStreams requires its `since=` parameter to be either a Unix-ms
# epoch or an ISO-8601 timestamp *without* micro-seconds and without a
# literal "+" in the TZ designator (the plus would be decoded as
# whitespace on the server side).  Using "Z" explicitly marks UTC and
# avoids URL-encoding issues.
# -------------------------------------------------------------------- #

_NOW_UTC_ISO = datetime.now(timezone.utc).replace(microsecond=0).strftime(ISO_TIMESTAMP_FORMAT)

stream = EventStreams(
    streams=["recentchange", "revision-create"],
    since=_NOW_UTC_ISO,
    # Also set UA header for the EventStreams connection.
    headers={"user-agent": USER_AGENT},
)
# (no register_filter)

# --------------------------------------------------------------------------- #
# ── Caches                                                                   #
# --------------------------------------------------------------------------- #

#  thread_id → (fingerprint, CustomFilter)
FILTER_CACHE: Dict[int, Tuple[str, "CustomFilter"]] = {}
#  thread_id → discord.Thread
THREAD_CACHE: Dict[int, discord.Thread] = {}

def _fingerprint(cfg: dict) -> str:
    """Deterministic (& cheap) fingerprint for a config dict."""
    return json.dumps(cfg, sort_keys=True)

# --------------------------------------------------------------------------- #
# ── Discord bot setup                                                        #
# --------------------------------------------------------------------------- #

# Use "!" *and* register slash commands; documentation promotes "/".
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
# Guard so we start only one worker across reconnects.
_stream_task: Optional[asyncio.Task] = None
_health_monitor_task: Optional[asyncio.Task] = None

# --------------------------------------------------------------------------- #
# ── Message formatting                                                        #
# --------------------------------------------------------------------------- #

def format_change(change: dict) -> str:
    """Turn one EventStreams record into a concise Discord message."""
    user = change['user']
    title = change.get("title", "(no title)")
    comment = change.get("comment") or "(no summary)"
    
    if change["type"] == "log":
        log_comment = change.get("log_action_comment") or comment
        link = f"https://{change.get('server_name','')}/w/index.php?title=Special:Log&logid={change.get('log_id','')}"
        return f"**{user}** {log_comment} \n<{link}>"

    # For regular edits
    page_url = f"https://{change['server_name']}/wiki/{title.replace(' ', '_')}"
    
    # Handle different change types that may or may not have revision info
    if change["type"] == "edit" and "revision" in change:
        diff_url = f"https://{change['server_name']}/w/index.php?diff={change['revision']['new']}"
        return f"**{user}** edited **[{title}](<{page_url}>)** ({comment}) \n<{diff_url}>"
    elif change["type"] == "new":
        return f"**{user}** created **[{title}](<{page_url}>)** ({comment})"
    elif change["type"] == "categorize":
        return f"**{user}** categorized **[{title}](<{page_url}>)** ({comment})"
    else:
        # Fallback for other change types
        return f"**{user}** modified **[{title}](<{page_url}>)** ({comment})"

# --------------------------------------------------------------------------- #
# ── EventStreams processing                                                  #
# --------------------------------------------------------------------------- #

async def eventstream_health_monitor():
    """
    Monitor the health of the EventStream connection.
    If no events are received within EVENTSTREAM_TIMEOUT_SECS, assume the
    connection is dead and terminate the process for external restart.
    """
    global last_event_time
    
    logger.info(
        f"EventStream health monitor started (timeout: {EVENTSTREAM_TIMEOUT_SECS}s, "
        f"check interval: {EVENTSTREAM_CHECK_INTERVAL_SECS}s)"
    )
    
    # Give the stream some time to initialize before monitoring
    await asyncio.sleep(EVENTSTREAM_CHECK_INTERVAL_SECS)
    
    while True:
        await asyncio.sleep(EVENTSTREAM_CHECK_INTERVAL_SECS)
        
        if last_event_time is None:
            logger.warning("EventStream health check: No events received yet")
            continue
        
        time_since_last_event = time.time() - last_event_time
        
        if time_since_last_event > EVENTSTREAM_TIMEOUT_SECS:
            logger.error(
                f"EventStream TIMEOUT: No events received for {time_since_last_event:.0f}s "
                f"(threshold: {EVENTSTREAM_TIMEOUT_SECS}s). Terminating process for restart."
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

    async def active_custom_filters() -> List[Tuple[int, "CustomFilter"]]:
        """
        Return list of (thread_id, CustomFilter) for **active** custom threads,
        rebuilding a filter only if its config actually changed.
        """
        async with CONFIG_LOCK:
            snapshot = list(CONFIG["threads"].items())

        out: List[Tuple[int, "CustomFilter"]] = []
        for tid_str, entry in snapshot:
            if not entry.get("active", False):
                FILTER_CACHE.pop(int(tid_str), None)
                continue
            tid        = int(tid_str)
            fp         = _fingerprint(entry["config"])
            cached     = FILTER_CACHE.get(tid)
            if cached and cached[0] == fp:
                filt = cached[1]
            else:
                filt = CustomFilter(entry["config"])
                FILTER_CACHE[tid] = (fp, filt)
            out.append((tid, filt))

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Found %d active custom filters", len(out))
        return out

    async def get_thread_obj(tid: int) -> Optional[discord.Thread]:
        """Return cached discord.Thread or fetch & cache it."""
        th = THREAD_CACHE.get(tid)
        if th is not None:
            return th
        try:
            ch = await bot.fetch_channel(tid)
        except (discord.NotFound, discord.Forbidden):
            THREAD_CACHE.pop(tid, None)
            return None
        except Exception:
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
        
        This runs in a thread pool executor to avoid blocking the async event loop.
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
                    last_event_time = time.time()
                    
                    # Put event in queue with timeout to detect blocking
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

    loop.run_in_executor(None, _producer)

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

        # Determine routing targets
        targets: List[discord.abc.Messageable] = []

        # Custom threads only
        customs = await active_custom_filters()

        # ----------  (#2) ultra-cheap global short-circuit ---------------- #
        user_whitelist  = {u for _, f in customs for u in f.user_include}
        page_regexes    = [f.page_include for _, f in customs if f.page_include]
        summary_regexes = [f.sum_include for _, f in customs if f.sum_include]

        title   = change.get("title", "")
        comment = change.get("log_action_comment") or change.get("comment") or ""

        if (
            change["user"] not in user_whitelist
            and not any(rx.search(title)   for rx in page_regexes)
            and not any(rx.search(comment) for rx in summary_regexes)
        ):
            event_queue.task_done()
            continue  # fast reject before any regex-heavy work

        if customs:
            for tid, filt in customs:
                try:
                    if filt.matches(change):
                        th = await get_thread_obj(tid)
                        if th is not None:
                            targets.append(th)
                except Exception as e:  # pragma: no cover
                    logger.warning(f"Custom filter error for {tid}: {e}")

        if not targets:
            event_queue.task_done()
            continue  # nothing to do

        msg = format_change(change)
        if len(msg) > DISCORD_MESSAGE_LIMIT:
            msg = msg[:DISCORD_MESSAGE_LIMIT - TRUNCATION_BUFFER] + TRUNCATED_MESSAGE_SUFFIX

        # Send to all targets; fire and forget
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Sending change to %d target(s): %s",
                len(targets),
                [tgt.name for tgt in targets],
            )
        for tgt in targets:
            await loop.create_task(tgt.send(msg))
        event_queue.task_done()

# --------------------------------------------------------------------------- #
# Global task references
# --------------------------------------------------------------------------- #
_health_monitor_task: Optional[asyncio.Task] = None
# --------------------------------------------------------------------------- #
# ── Discord commands                                                         #
# --------------------------------------------------------------------------- #

def authorised(ctx) -> bool:
    """Gatekeeper: only allow guild moderators or the bot owner."""
    return ctx.author.guild_permissions.manage_guild or ctx.author.id == bot.owner_id





@bot.hybrid_command(name="fezhelp",
                    description="Show available commands",
                    with_app_command=True)
async def fezhelp_cmd(ctx: commands.Context):
    """Display available commands and their descriptions."""
    logger.info(f"Help command from {ctx.author} in {ctx.channel}")
    help_text = """**Available Commands:**

**Basic Commands:**
* `/fezhelp` - Show this help message

**Thread Management:**
* `/add <Username>` - Create a "User:Username" thread and add the user to `userIncludeList`
* `/addcustom <name>` - Create a generic filter thread (parent channel only)
* `/activate` - Activate current thread
* `/deactivate` - Deactivate current thread

**Global Configuration:**
* `/globalconfig getraw` - Download full configuration as JSON
* `/globalconfig setraw` - Replace configuration from attached JSON file (DANGEROUS)

**Thread Configuration:**
* `/config` - Show current thread configuration
* `/config getraw` - Download raw JSON for current thread
* `/config setraw` - Replace current-thread configuration from attached JSON (DANGEROUS)
* `/config set <key> <json>` - Set configuration value
* `/config add <key> <value>` - Add to list configuration
* `/config remove <key> <value>` - Remove from list configuration
* `/config clear <key>` - Clear list configuration

**Configuration Keys:**
* `siteName` - Filter by site (e.g., "en.wikipedia.org")
* `pageIncludePatterns` - Pages to include (regex patterns)
* `pageExcludePatterns` - Pages to exclude (regex patterns)
* `userIncludeList` - Users to include
* `userExcludeList` - Users to exclude
* `summaryIncludePatterns` - Summary patterns to include
* `summaryExcludePatterns` - Summary patterns to exclude

**Legacy Commands (still work):**
* `!add`, `!addcustom`, `!activate`, `!deactivate`, `!config`"""
    
    await ctx.reply(help_text)





@bot.hybrid_command(name="add",
                    description="Create a per-user custom thread and include them",
                    with_app_command=True)
async def add_cmd(ctx: commands.Context, *, user: str):
    """
    Convenience wrapper that delegates to the new `/addcustom` logic,
    then appends the supplied username to `userIncludeList`.
    """
    logger.info(f"Add command from {ctx.author} for user '{user}' in {ctx.channel}")
    
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        logger.warning(f"Add command attempted in wrong channel: {ctx.channel.id} (expected {DISCORD_CHANNEL_ID})")
        await ctx.reply("Run `/add` in the parent channel.")
        return

    # create "User:&lt;Username&gt;" thread
    try:
        thread = await ctx.channel.create_thread(
            name=f"User:{user}",
            type=discord.ChannelType.public_thread
        )
        logger.info(f"Created thread {thread.name} ({thread.id}) for user {user}")
    except Exception as e:
        logger.error(f"Failed to create thread for user {user}: {e}")
        await ctx.reply(f"Failed to create thread: {e}")
        return

    await ensure_custom_thread_entry(thread, create_if_missing=True)
    _, new_list = await mutate_custom_thread_config_list(
        thread.id, "userIncludeList", add=[user]
    )
    await ctx.reply(f"Tracking **{user}** in <#{thread.id}>.\n`userIncludeList`: `{new_list}`")


# --------------------------------------------------------------------------- #
# ── Global configuration ("/globalconfig ...")                                 #
# --------------------------------------------------------------------------- #


@bot.hybrid_group(name="globalconfig",
                  invoke_without_command=True,
                  description="Global configuration operations",
                  with_app_command=True)
@commands.check(authorised)
async def globalconfig_group(ctx: commands.Context):
    """`/globalconfig` (no subcommand) -> `/globalconfig getraw`."""
    await ctx.invoke(globalconfig_getraw_cmd)


@globalconfig_group.command(name="getraw",
                            description="Download full configuration as JSON")
@commands.check(authorised)
async def globalconfig_getraw_cmd(ctx: commands.Context):  # noqa: N802
    logger.info(f"Global config getraw command from {ctx.author}")
    async with CONFIG_LOCK:
        data = json.dumps(CONFIG, indent=JSON_INDENT).encode(UTF8_ENCODING)
    file = discord.File(io.BytesIO(data), filename="global_config.json")
    await ctx.reply(file=file, mention_author=False)


@globalconfig_group.command(name="setraw",
                            description="Replace configuration from attached JSON (DANGEROUS)")
@commands.check(authorised)
async def globalconfig_setraw_cmd(ctx: commands.Context):  # noqa: N802
    logger.warning(f"Global config setraw command from {ctx.author} - DANGEROUS OPERATION")
    
    if not ctx.message.attachments:
        logger.warning("Global config setraw attempted without attachment")
        await ctx.reply("Attach a **JSON** file to `/globalconfig setraw`.", mention_author=False)
        return
    attachment = ctx.message.attachments[0]
    try:
        raw = await attachment.read()
        new_cfg = json.loads(raw)
        logger.info(f"Successfully parsed global config from attachment: {len(new_cfg.get('threads', {}))} threads")
    except Exception as e:
        logger.error(f"Failed to parse global config attachment: {e}")
        await ctx.reply(f"Could not parse attachment as JSON: {e}", mention_author=False)
        return
    async with CONFIG_LOCK:
        CONFIG.clear()
        CONFIG.update(new_cfg)
        save_config(CONFIG)
    logger.info("Global configuration replaced successfully")
    await ctx.reply("Global configuration **replaced**.", mention_author=False)





# --------------------------------------------------------------------------- #
# ── Custom thread commands                                                   #
# --------------------------------------------------------------------------- #

def _in_parent_channel(ctx: commands.Context) -> bool:
    return ctx.channel.id == DISCORD_CHANNEL_ID


def _in_custom_thread(ctx: commands.Context) -> bool:
    """True when inside *any* thread spawned from the parent channel."""
    return (
        isinstance(ctx.channel, discord.Thread)
        and ctx.channel.parent_id == DISCORD_CHANNEL_ID
    )


def _parse_json_arg(s: str) -> Any:
    try:
        return json.loads(s)
    except Exception:
        return s  # fall back to raw string


def _deepcopy_cfg(obj: Any) -> Any:
    """Cheap JSON round-trip copy."""
    try:
        return json.loads(json.dumps(obj))
    except Exception:
        # worst case, return original; caller should treat as immutable
        return obj


@bot.hybrid_command(name="addcustom",
                    description="Create a custom-filter thread (parent channel only)",
                    with_app_command=True)
async def addcustom_cmd(ctx: commands.Context, *, threadname: str = ""):
    """/addcustom <threadname> → create a custom filter thread (in parent channel only)."""
    logger.info(f"Addcustom command from {ctx.author} for thread '{threadname}' in {ctx.channel}")
    
    if not _in_parent_channel(ctx):
        logger.warning(f"Addcustom command attempted in wrong channel: {ctx.channel.id}")
        await ctx.reply("Use `/addcustom` in the parent channel.")
        return
    if not threadname.strip():
        logger.warning("Addcustom command attempted without thread name")
        await ctx.reply("Please provide a thread name: `/addcustom <name>`.")
        return
    try:
        thread = await ctx.channel.create_thread(
            name=threadname,
            type=discord.ChannelType.public_thread
        )
        logger.info(f"Created custom thread {thread.name} ({thread.id})")
    except Exception as e:  # pragma: no cover
        logger.error(f"Failed to create custom thread '{threadname}': {e}")
        await ctx.reply(f"Failed to create thread: {e}")
        return
    await ensure_custom_thread_entry(thread, create_if_missing=True)
    await ctx.reply(f"Thread created: <#{thread.id}> (active). Configure with `/config` inside the thread.")


async def _require_custom_thread(ctx: commands.Context) -> Optional[dict]:
    if not _in_custom_thread(ctx):
        await ctx.reply("This command must be used inside a thread.")
        return None
    entry = await ensure_custom_thread_entry(ctx.channel, create_if_missing=False)
    if entry is None:
        # thread exists but no entry (perhaps race); create one
        entry = await ensure_custom_thread_entry(ctx.channel, create_if_missing=True)
    return entry


@bot.hybrid_command(name="activate",
                    description="Activate current custom thread",
                    with_app_command=True)
async def activate_custom_thread_cmd(ctx: commands.Context):
    logger.info(f"Activate command from {ctx.author} in thread {ctx.channel.id}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    ok = await set_custom_thread_active(ctx.channel.id, True)
    if ok:
        await ctx.reply("Activated.")
    else:
        logger.error(f"Failed to activate thread {ctx.channel.id}")
        await ctx.reply("Failed to activate (missing config?).")


@bot.hybrid_command(name="deactivate",
                    description="Deactivate current custom thread",
                    with_app_command=True)
async def deactivate_custom_thread_cmd(ctx: commands.Context):
    logger.info(f"Deactivate command from {ctx.author} in thread {ctx.channel.id}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    ok = await set_custom_thread_active(ctx.channel.id, False)
    if ok:
        await ctx.reply("Deactivated.")
    else:
        logger.error(f"Failed to deactivate thread {ctx.channel.id}")
        await ctx.reply("Failed to deactivate (missing config?).")


@bot.hybrid_group(name="config",
                  invoke_without_command=True,
                  description="Thread configuration operations",
                  with_app_command=True)
async def config_group(ctx: commands.Context):
    """`/config` (no subcommand) -> `/config get`."""
    await ctx.invoke(config_get_cmd)


@config_group.command(name="get",
                      description="Show current thread configuration")
async def config_get_cmd(ctx: commands.Context):
    logger.info(f"Config get command from {ctx.author} in thread {ctx.channel.id}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    pretty = json.dumps(entry["config"], indent=JSON_INDENT)
    await ctx.reply(f"```json\n{pretty}\n```")


@config_group.command(name="set",
                      description="Set a configuration key")
async def config_set_cmd(ctx: commands.Context, key: str, *, value: str):
    logger.info(f"Config set command from {ctx.author} in thread {ctx.channel.id}: {key} = {value}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    parsed = _parse_json_arg(value)
    # If key unknown, set anyway
    cfg = _deepcopy_cfg(entry["config"])
    cfg[key] = parsed
    ok = await update_custom_thread_config(ctx.channel.id, cfg)
    if ok:
        await ctx.reply(f"Set `{key}`: `{parsed}`")
    else:
        logger.error(f"Failed to set config {key} for thread {ctx.channel.id}")
        await ctx.reply("Failed to set.")


@config_group.command(name="add",
                      description="Append value(s) to a list key")
async def config_add_cmd(ctx: commands.Context, key: str, *, value: str):
    logger.info(f"Config add command from {ctx.author} in thread {ctx.channel.id}: {key} += {value}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(_parse_json_arg(value))
    ok, new_list = await mutate_custom_thread_config_list(ctx.channel.id, key, add=vals)
    if ok:
        await ctx.reply(f"Added to `{key}`: `{vals}`\nNow: `{new_list}`")
    else:
        logger.error(f"Failed to add to config {key} for thread {ctx.channel.id}")
        await ctx.reply("Failed to add.")


@config_group.command(name="remove",
                      description="Remove value(s) from a list key")
async def config_remove_cmd(ctx: commands.Context, key: str, *, value: str):
    logger.info(f"Config remove command from {ctx.author} in thread {ctx.channel.id}: {key} -= {value}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(_parse_json_arg(value))
    ok, new_list = await mutate_custom_thread_config_list(ctx.channel.id, key, remove=vals)
    if ok:
        await ctx.reply(f"Removed from `{key}`: `{vals}`\nNow: `{new_list}`")
    else:
        logger.error(f"Failed to remove from config {key} for thread {ctx.channel.id}")
        await ctx.reply("Failed to remove.")


@config_group.command(name="clear",
                      description="Clear a list key")
async def config_clear_cmd(ctx: commands.Context, key: str):
    logger.info(f"Config clear command from {ctx.author} in thread {ctx.channel.id}: clear {key}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    ok, _ = await mutate_custom_thread_config_list(ctx.channel.id, key, clear=True)
    if ok:
        await ctx.reply(f"Cleared `{key}`.")
    else:
        logger.error(f"Failed to clear config {key} for thread {ctx.channel.id}")
        await ctx.reply("Failed to clear.")


# --------------------------------------------------------------------------- #
# ── /config … raw variants (per‑thread)                                     #
# --------------------------------------------------------------------------- #


@config_group.command(name="getraw",
                      description="Download raw JSON configuration for this thread")
@commands.check(authorised)
async def config_getraw_cmd(ctx: commands.Context):
    logger.info(f"Config getraw command from {ctx.author} in thread {ctx.channel.id}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    data = json.dumps(entry["config"], indent=JSON_INDENT).encode(UTF8_ENCODING)
    file = discord.File(io.BytesIO(data),
                        filename=f"thread_{ctx.channel.id}_config.json")
    await ctx.reply(file=file, mention_author=False)


@config_group.command(name="setraw",
                      description="Replace this thread's configuration from attached JSON (DANGEROUS)")
@commands.check(authorised)
async def config_setraw_cmd(ctx: commands.Context):
    logger.warning(f"Config setraw command from {ctx.author} in thread {ctx.channel.id}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return

    if not ctx.message.attachments:
        await ctx.reply("Attach a **JSON** file to `/config setraw`.",
                        mention_author=False)
        return

    attachment = ctx.message.attachments[0]
    try:
        raw = await attachment.read()
        new_cfg = json.loads(raw)
        logger.info(f"Parsed thread config for {ctx.channel.id} successfully")
    except Exception as e:
        logger.error(f"Failed to parse thread config: {e}")
        await ctx.reply(f"Could not parse attachment as JSON: {e}",
                        mention_author=False)
        return

    ok = await update_custom_thread_config(ctx.channel.id, new_cfg)
    if ok:
        await ctx.reply("Thread configuration **replaced**.",
                        mention_author=False)
    else:
        await ctx.reply("Failed to replace configuration.",
                        mention_author=False)

# --------------------------------------------------------------------------- #
# ── Lifecycle events                                                         #
# --------------------------------------------------------------------------- #

@bot.event
async def on_error(event, *args, **kwargs):
    """Log any errors that occur in Discord events."""
    logger.error(f"Error in event {event}: {args} {kwargs}")

@bot.event
async def on_command_error(ctx, error):
    """Log command errors."""
    if isinstance(error, commands.CheckFailure):
        logger.warning(f"Unauthorized command attempt by {ctx.author} in {ctx.channel}: {ctx.message.content}")
    elif isinstance(error, commands.CommandNotFound):
        logger.debug(f"Unknown command by {ctx.author}: {ctx.message.content}")
    else:
        logger.error(f"Command error from {ctx.author} in {ctx.channel}: {error}")

# ------------------------------------------------------------------------ #
#  Thread-cache invalidation                                               #
# ------------------------------------------------------------------------ #

@bot.event
async def on_thread_update(before: discord.Thread, after: discord.Thread):
    THREAD_CACHE[after.id] = after

@bot.event
async def on_thread_delete(thread: discord.Thread):
    THREAD_CACHE.pop(thread.id, None)

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (id {bot.user.id})")
    # Register (or update) global application commands with Discord.
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} application command(s)")
    except Exception as e:
        logger.warning(f"Failed to sync application commands: {e}")

    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    # Preload custom thread entries for existing threads (best effort)
    async with CONFIG_LOCK:
        # nothing to do here; entries created on demand
        pass

    if channel is None:
        logger.error(f"Could not find channel ID {DISCORD_CHANNEL_ID}")
        sys.exit(f"Could not find channel ID {DISCORD_CHANNEL_ID}")
    logger.info(f"Found target channel: {channel.name} ({channel.id})")
    # Start the background EventStreams task and health monitor once.
    global _stream_task, _health_monitor_task
    
    if _stream_task is None or _stream_task.done():
        logger.info("Starting EventStreams worker task")
        _stream_task = bot.loop.create_task(stream_worker(channel))
    else:
        logger.info("EventStreams worker already running; not starting another one")
    
    if _health_monitor_task is None or _health_monitor_task.done():
        logger.info("Starting EventStream health monitor task")
        _health_monitor_task = bot.loop.create_task(eventstream_health_monitor())
    else:
        logger.info("EventStream health monitor already running")


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    logger.info(f"Received signal {signum}, shutting down gracefully...")
    sys.exit(0)

def main():
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        logger.info("Starting Discord bot...")
        bot.run(DISCORD_TOKEN)
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down...")
    except Exception as e:
        logger.error(f"Unexpected error in main: {e}")
        raise
    finally:
        logger.info("Bot shutdown complete")


if __name__ == "__main__":
    main()
