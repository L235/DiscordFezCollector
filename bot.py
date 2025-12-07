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
* `/thread <name>` (`!addcustom`) - Create a generic filter thread (parent channel only)
* `/userthread <Username>` (`!add`) - Create a "User:<Username>" thread **and** add the user to `userIncludeList`
* `/activate` / `/deactivate` - Toggle activity for the *current thread*
* `/status` - Show current thread configuration (alias for `/config get`)
* `/track page|user|summary <value>` - Add to include filters
* `/ignore page|user|summary <value>` - Add to exclude filters
* `/untrack page|user|summary <value>` - Remove from include filters
* `/unignore page|user|summary <value>` - Remove from exclude filters
* `/globalconfig getraw`  - Download full configuration as a JSON attachment
* `/globalconfig setraw` - **Replace** the entire configuration from an attached JSON file (**dangerous**)
* `/config [get]` - Show current thread configuration
* `/config set <key> <json>` - Set configuration value
* `/config getraw` - Download raw JSON for the current thread
* `/config setraw` - **Replace** the current thread configuration from an attached JSON file (**dangerous**)
* `/config add|remove|clear ...` - Mutate list-type configuration fields

**Config Schema (v0.9):**

      {
          "version": "0.9",
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
          },
          "receivers": {
              "<key>": {                        # key matches FEZ_WEBHOOKS entry
                  "name": "Display name",
                  "active": true,
                  "errored": false,             # set true on webhook failure
                  "config": { ... }             # same structure as thread config
              }
          }
      }

**Environment Variables (new):**
* `FEZ_WEBHOOKS` - Comma-separated webhook mappings: "key1=https://...,key2=https://..."
"""
VERSION = "0.9-webhooks"

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
CONFIG_VERSION = "0.9"
UTF8_ENCODING = "utf-8"
ISO_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
JSON_INDENT = 2

# Discord limits
DISCORD_MESSAGE_LIMIT = 2000
TRUNCATED_MESSAGE_SUFFIX = "..."
TRUNCATION_BUFFER = 10  # 1990 = 2000 - 10

# Timeout and retry configuration (for EventStreams)
class RetryConfig:
    INITIAL_BACKOFF_SECS = int(os.getenv("RETRY_INITIAL_BACKOFF_SECS", "5"))
    MAX_BACKOFF_SECS = int(os.getenv("RETRY_MAX_BACKOFF_SECS", "300"))
    GRACE_PERIOD_SECS = int(os.getenv("RETRY_GRACE_PERIOD_SECS", "5"))

# Discord API rate limit configuration
class DiscordRateLimitConfig:
    MAX_ATTEMPTS = int(os.getenv("DISCORD_RATE_LIMIT_MAX_ATTEMPTS", "5"))
    INITIAL_BACKOFF_SECS = float(os.getenv("DISCORD_RATE_LIMIT_INITIAL_BACKOFF_SECS", "1"))
    MAX_BACKOFF_SECS = float(os.getenv("DISCORD_RATE_LIMIT_MAX_BACKOFF_SECS", "60"))

# EventStream configuration
class EventStreamConfig:
    TIMEOUT_SECS = int(os.getenv("EVENTSTREAM_TIMEOUT_SECS", "600"))  # 10 minutes default
    CHECK_INTERVAL_SECS = int(os.getenv("EVENTSTREAM_CHECK_INTERVAL_SECS", "60"))
    QUEUE_MAX_SIZE = int(os.getenv("EVENTSTREAM_QUEUE_MAX_SIZE", "2000"))
    QUEUE_PUT_TIMEOUT_SECS = float(os.getenv("EVENTSTREAM_QUEUE_PUT_TIMEOUT_SECS", "30.0"))
    QUEUE_RESULT_TIMEOUT_SECS = float(os.getenv("EVENTSTREAM_QUEUE_RESULT_TIMEOUT_SECS", "35.0"))
    ERROR_BODY_LIMIT = int(os.getenv("EVENTSTREAM_ERROR_BODY_LIMIT", "2000"))

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
# ── Webhook URL parsing                                                       #
# --------------------------------------------------------------------------- #

def parse_webhooks_env() -> Dict[str, str]:
    """
    Parse FEZ_WEBHOOKS env var.
    Format: "key1=https://discord.com/api/webhooks/...,key2=https://..."
    Returns dict mapping key -> URL.
    """
    raw = os.getenv("FEZ_WEBHOOKS", "")
    if not raw.strip():
        return {}
    result = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" not in pair:
            logger.warning(f"Invalid webhook entry (missing '='): {pair}")
            continue
        key, url = pair.split("=", 1)
        key = key.strip()
        url = url.strip()
        if not key:
            logger.warning(f"Empty webhook key in: {pair}")
            continue
        if not url.startswith("https://"):
            logger.warning(f"Webhook URL must start with https://: {url}")
            continue
        result[key] = url
    if result:
        logger.info(f"Loaded {len(result)} webhook(s) from FEZ_WEBHOOKS: {list(result.keys())}")
    return result

WEBHOOKS: Dict[str, str] = parse_webhooks_env()

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
DEFAULT_CONFIG: Dict[str, Any] = {"version": CONFIG_VERSION, "threads": {}, "receivers": {}}


def load_config() -> dict:
    if not STATE_FILE.exists():
        logger.info(f"Creating new config file at {STATE_FILE}")
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    logger.debug(f"Loading config from {STATE_FILE}")
    with STATE_FILE.open(encoding=UTF8_ENCODING) as fp:
        raw = json.load(fp)
    # ensure keys exist - older configs will silently keep working,
    # but we no longer mutate them in-place.
    raw.setdefault("threads", {})
    raw.setdefault("receivers", {})
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
# ── Discord API rate limit handling                                          #
# --------------------------------------------------------------------------- #

async def discord_api_call_with_backoff(coro_func, *args, **kwargs):
    """
    Execute a Discord API coroutine with exponential backoff on rate limits.

    Args:
        coro_func: An async callable (not an awaitable) that makes a Discord API call
        *args, **kwargs: Arguments to pass to coro_func

    Returns:
        The result of the Discord API call

    Raises:
        discord.NotFound: If the resource was not found (404)
        discord.Forbidden: If access is denied (403)
        discord.HTTPException: If max attempts exceeded or other HTTP error
    """
    backoff = DiscordRateLimitConfig.INITIAL_BACKOFF_SECS
    last_exception = None

    for attempt in range(DiscordRateLimitConfig.MAX_ATTEMPTS):
        try:
            return await coro_func(*args, **kwargs)
        except (discord.NotFound, discord.Forbidden):
            # Let these propagate directly - caller should handle them
            raise
        except discord.HTTPException as e:
            last_exception = e
            if e.status == 429:
                # Discord rate limit - use retry_after if provided, otherwise use backoff
                retry_after = getattr(e, 'retry_after', None)
                wait_time = retry_after if retry_after else backoff

                logger.warning(
                    f"Discord rate limit hit (429). Attempt {attempt + 1}/{DiscordRateLimitConfig.MAX_ATTEMPTS}. "
                    f"Waiting {wait_time:.1f}s before retry..."
                )
                await asyncio.sleep(wait_time)
                backoff = min(backoff * 2, DiscordRateLimitConfig.MAX_BACKOFF_SECS)
            else:
                # Non-rate-limit error, don't retry
                logger.error(f"Discord API error (status {e.status}): {e}")
                raise

    # Max attempts exceeded
    logger.error(
        f"Discord API call failed after {DiscordRateLimitConfig.MAX_ATTEMPTS} attempts. "
        f"Last error: {last_exception}"
    )
    raise last_exception


async def send_message_with_backoff(target: discord.abc.Messageable, content: str) -> Optional[discord.Message]:
    """
    Send a message to a Discord channel/thread with rate limit handling.

    Args:
        target: The channel or thread to send to
        content: The message content

    Returns:
        The sent Message object, or None if sending failed
    """
    try:
        return await discord_api_call_with_backoff(target.send, content)
    except discord.HTTPException as e:
        logger.error(f"Failed to send message to {getattr(target, 'name', target)}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error sending message to {getattr(target, 'name', target)}: {e}")
        return None

# --------------------------------------------------------------------------- #
# ── Webhook sending with backoff (using discord.py native support)            #
# --------------------------------------------------------------------------- #

class WebhookError(Exception):
    """Raised when a webhook send fails permanently."""
    pass


# Cache for webhook objects: url -> discord.Webhook
_WEBHOOK_CACHE: Dict[str, discord.Webhook] = {}


async def send_webhook_with_backoff(
    webhook_url: str,
    content: str,
    receiver_key: str
) -> bool:
    """
    Send a message to a Discord webhook with exponential backoff on rate limits.
    Uses discord.py's native webhook support.

    Args:
        webhook_url: The Discord webhook URL
        content: The message content
        receiver_key: Key identifying this receiver (for logging)

    Returns:
        True if message was sent successfully

    Raises:
        WebhookError: If the webhook is permanently broken (404, 401, etc.)
    """
    # Get or create webhook object (discord.py handles the session internally)
    webhook = _WEBHOOK_CACHE.get(webhook_url)
    if webhook is None:
        try:
            webhook = discord.Webhook.from_url(webhook_url, client=bot)
            _WEBHOOK_CACHE[webhook_url] = webhook
        except Exception as e:
            logger.error(f"Invalid webhook URL for '{receiver_key}': {e}")
            raise WebhookError(f"Invalid webhook URL: {e}")

    try:
        # Use the existing backoff helper - it handles 429s and retries
        await discord_api_call_with_backoff(webhook.send, content)
        return True
    except discord.NotFound:
        # Webhook was deleted
        _WEBHOOK_CACHE.pop(webhook_url, None)
        logger.error(f"Webhook not found for '{receiver_key}' (deleted?)")
        raise WebhookError("Webhook not found (404)")
    except discord.Forbidden:
        # Permission denied
        _WEBHOOK_CACHE.pop(webhook_url, None)
        logger.error(f"Webhook forbidden for '{receiver_key}'")
        raise WebhookError("Webhook forbidden (403)")
    except discord.HTTPException as e:
        logger.error(f"Webhook HTTP error for '{receiver_key}': {e}")
        raise WebhookError(f"HTTP error: {e}")

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

# --------------------------------------------------------------------------- #
# ── Receiver (webhook) helpers                                                #
# --------------------------------------------------------------------------- #

def _validate_receiver_key(key: str) -> bool:
    """Validate receiver key format: alphanumeric + underscores only."""
    return bool(re.match(r'^[a-zA-Z0-9_]+$', key))


async def get_receiver_entry(key: str) -> Optional[dict]:
    """Get a receiver config entry by key."""
    async with CONFIG_LOCK:
        return CONFIG.get("receivers", {}).get(key)


async def create_receiver(key: str, name: str) -> Tuple[bool, str]:
    """
    Create a new receiver entry.
    Returns (success, message).
    """
    if not _validate_receiver_key(key):
        return False, "Key must be alphanumeric with underscores only"
    if key not in WEBHOOKS:
        return False, f"No webhook URL found for key '{key}' in FEZ_WEBHOOKS env var"

    async with CONFIG_LOCK:
        if key in CONFIG.get("receivers", {}):
            return False, f"Receiver '{key}' already exists"
        CONFIG.setdefault("receivers", {})[key] = {
            "name": name,
            "active": False,  # Start inactive until explicitly activated
            "errored": False,
            "config": _blank_custom_cfg(),
        }
        save_config(CONFIG)
        logger.info(f"Created receiver '{key}' with name '{name}'")
    return True, f"Created receiver '{key}'"


async def delete_receiver(key: str) -> Tuple[bool, str]:
    """Delete a receiver entry."""
    async with CONFIG_LOCK:
        receivers = CONFIG.get("receivers", {})
        if key not in receivers:
            return False, f"Receiver '{key}' not found"
        del receivers[key]
        save_config(CONFIG)
        logger.info(f"Deleted receiver '{key}'")
    return True, f"Deleted receiver '{key}'"


async def set_receiver_active(key: str, active: bool) -> Tuple[bool, str]:
    """Set receiver active state."""
    if active and key not in WEBHOOKS:
        return False, f"Cannot activate: no webhook URL for '{key}' in FEZ_WEBHOOKS"

    async with CONFIG_LOCK:
        receivers = CONFIG.get("receivers", {})
        if key not in receivers:
            return False, f"Receiver '{key}' not found"
        entry = receivers[key]
        if active and entry.get("errored"):
            # Reset errored state when reactivating
            entry["errored"] = False
            logger.info(f"Cleared error state for receiver '{key}'")
        entry["active"] = active
        save_config(CONFIG)
        state = "activated" if active else "deactivated"
        logger.info(f"Receiver '{key}' {state}")
    return True, f"Receiver '{key}' {state}"


async def set_receiver_errored(key: str) -> bool:
    """Mark a receiver as errored (called when webhook fails)."""
    async with CONFIG_LOCK:
        receivers = CONFIG.get("receivers", {})
        if key not in receivers:
            return False
        receivers[key]["errored"] = True
        receivers[key]["active"] = False  # Auto-deactivate on error
        save_config(CONFIG)
        logger.warning(f"Receiver '{key}' marked as errored and deactivated")
    return True


async def update_receiver_config(key: str, new_cfg: dict) -> bool:
    """Update a receiver's filter config."""
    async with CONFIG_LOCK:
        receivers = CONFIG.get("receivers", {})
        if key not in receivers:
            return False
        receivers[key]["config"] = new_cfg
        save_config(CONFIG)
        logger.info(f"Updated config for receiver '{key}'")
    return True


async def mutate_receiver_config_list(
    key: str,
    field: str,
    *,
    add: Optional[List[str]] = None,
    remove: Optional[List[str]] = None,
    clear: bool = False
) -> Tuple[bool, Optional[List[str]]]:
    """Mutate a list field in a receiver's config."""
    async with CONFIG_LOCK:
        receivers = CONFIG.get("receivers", {})
        if key not in receivers:
            return False, None
        cfg = receivers[key]["config"]
        if field not in cfg:
            cfg[field] = []
        if clear:
            cfg[field] = []
        else:
            lst = list(cfg[field])
            if add:
                for it in add:
                    if it not in lst:
                        lst.append(it)
            if remove:
                lst = [it for it in lst if it not in remove]
            cfg[field] = lst
        save_config(CONFIG)
        return True, list(cfg[field])


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
#  receiver_key → (fingerprint, CustomFilter)
RECEIVER_FILTER_CACHE: Dict[str, Tuple[str, "CustomFilter"]] = {}

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

    async def active_receiver_filters() -> List[Tuple[str, "CustomFilter"]]:
        """
        Return list of (receiver_key, CustomFilter) for **active** receivers,
        rebuilding a filter only if its config actually changed.
        """
        async with CONFIG_LOCK:
            snapshot = list(CONFIG.get("receivers", {}).items())

        out: List[Tuple[str, "CustomFilter"]] = []
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
        receiver_targets: List[str] = []  # List of receiver keys to send to

        # Custom threads and receivers
        customs = await active_custom_filters()
        receivers = await active_receiver_filters()

        # ----------  (#2) ultra-cheap global short-circuit ---------------- #
        # Combine filters from both threads and receivers for fast-reject
        all_filters = [(None, f) for _, f in customs] + [(None, f) for _, f in receivers]
        user_whitelist  = {u for _, f in all_filters for u in f.user_include}
        page_regexes    = [f.page_include for _, f in all_filters if f.page_include]
        summary_regexes = [f.sum_include for _, f in all_filters if f.sum_include]

        title   = change.get("title", "")
        comment = change.get("log_action_comment") or change.get("comment") or ""

        if (
            change["user"] not in user_whitelist
            and not any(rx.search(title)   for rx in page_regexes)
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
                            targets.append(th)
                except Exception as e:  # pragma: no cover
                    logger.warning(f"Custom filter error for {tid}: {e}")

        # Match against receiver filters
        if receivers:
            for key, filt in receivers:
                try:
                    if filt.matches(change):
                        receiver_targets.append(key)
                except Exception as e:
                    logger.warning(f"Receiver filter error for '{key}': {e}")

        if not targets and not receiver_targets:
            event_queue.task_done()
            continue  # nothing to do

        msg = format_change(change)
        if len(msg) > DISCORD_MESSAGE_LIMIT:
            msg = msg[:DISCORD_MESSAGE_LIMIT - TRUNCATION_BUFFER] + TRUNCATED_MESSAGE_SUFFIX

        # Send to thread targets with rate limit handling
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Sending change to %d thread(s) and %d receiver(s)",
                len(targets),
                len(receiver_targets),
            )
        for tgt in targets:
            await send_message_with_backoff(tgt, msg)

        # Send to receiver webhooks (uses discord.py native webhook support)
        for key in receiver_targets:
            webhook_url = WEBHOOKS.get(key)
            if not webhook_url:
                logger.warning(f"Receiver '{key}' matched but no webhook URL")
                continue
            try:
                await send_webhook_with_backoff(webhook_url, msg, key)
            except WebhookError as e:
                logger.error(f"Webhook error for receiver '{key}': {e}")
                # Mark receiver as errored so it stops receiving
                await set_receiver_errored(key)

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
* `/status` - Show current thread configuration

**Thread Management (run in parent channel):**
* `/thread <name>` - Create a new tracking thread
* `/userthread <Username>` - Create a "User:Username" thread and auto-add to userIncludeList
* `/activate` - Activate current thread
* `/deactivate` - Deactivate current thread

**Tracking Shortcuts (run inside a thread):**
* `/track page|user|summary <value>` - Add to include filters
* `/ignore page|user|summary <value>` - Add to exclude filters
* `/untrack page|user|summary <value>` - Remove from include filters
* `/unignore page|user|summary <value>` - Remove from exclude filters

**Global Configuration:**
* `/globalconfig getraw` - Download full configuration as JSON
* `/globalconfig setraw` - Replace configuration from attached JSON file (DANGEROUS)

**Advanced Thread Configuration:**
* `/config` - Show current thread configuration (same as `/status`)
* `/config set <key> <json>` - Set configuration value
* `/config add <key> <value>` - Add to list configuration
* `/config remove <key> <value>` - Remove from list configuration
* `/config clear <key>` - Clear list configuration
* `/config getraw` - Download raw JSON for current thread
* `/config setraw` - Replace current-thread configuration from attached JSON (DANGEROUS)

**Receiver Commands (owner only - for webhook feeds):**
* `/receiver list` - List all receivers
* `/receiver add <key> <name>` - Create a new receiver
* `/receiver remove <key>` - Delete a receiver
* `/receiver activate <key>` / `/receiver deactivate <key>` - Toggle receiver
* `/receiver config <key>` - Show receiver configuration
* `/receiver track|ignore|untrack|unignore <key> page|user|summary <value>` - Manage filters

**Configuration Keys:**
* `siteName` - Filter by site (e.g., "en.wikipedia.org")
* `pageIncludePatterns` / `pageExcludePatterns` - Page regex patterns
* `userIncludeList` / `userExcludeList` - User lists
* `summaryIncludePatterns` / `summaryExcludePatterns` - Summary regex patterns

**Legacy Commands (still work):**
* `!add` (→ `/userthread`), `!addcustom` (→ `/thread`), `!activate`, `!deactivate`, `!config`"""

    await ctx.reply(help_text)





@bot.hybrid_command(name="userthread",
                    aliases=["add"],
                    description="Create a per-user custom thread and include them",
                    with_app_command=True)
async def userthread_cmd(ctx: commands.Context, *, user: str):
    """
    Convenience wrapper that creates a User:<username> thread,
    then appends the supplied username to `userIncludeList`.
    """
    logger.info(f"Userthread command from {ctx.author} for user '{user}' in {ctx.channel}")

    if ctx.channel.id != DISCORD_CHANNEL_ID:
        logger.warning(f"Userthread command attempted in wrong channel: {ctx.channel.id} (expected {DISCORD_CHANNEL_ID})")
        await ctx.reply("Run `/userthread` in the parent channel.")
        return

    # create "User:<Username>" thread
    try:
        thread = await discord_api_call_with_backoff(
            ctx.channel.create_thread,
            name=f"User:{user}",
            type=discord.ChannelType.public_thread
        )
        logger.info(f"Created thread {thread.name} ({thread.id}) for user {user}")
    except discord.HTTPException as e:
        logger.error(f"Failed to create thread for user {user}: {e}")
        await ctx.reply(f"Failed to create thread: {e}")
        return
    except Exception as e:
        logger.error(f"Unexpected error creating thread for user {user}: {e}")
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


@bot.hybrid_command(name="thread",
                    aliases=["addcustom"],
                    description="Create a custom-filter thread (parent channel only)",
                    with_app_command=True)
async def thread_cmd(ctx: commands.Context, *, threadname: str = ""):
    """/thread <threadname> → create a custom filter thread (in parent channel only)."""
    logger.info(f"Thread command from {ctx.author} for thread '{threadname}' in {ctx.channel}")
    
    if not _in_parent_channel(ctx):
        logger.warning(f"Thread command attempted in wrong channel: {ctx.channel.id}")
        await ctx.reply("Use `/thread` in the parent channel.")
        return
    if not threadname.strip():
        logger.warning("Thread command attempted without thread name")
        await ctx.reply("Please provide a thread name: `/thread <name>`.")
        return
    try:
        thread = await discord_api_call_with_backoff(
            ctx.channel.create_thread,
            name=threadname,
            type=discord.ChannelType.public_thread
        )
        logger.info(f"Created custom thread {thread.name} ({thread.id})")
    except discord.HTTPException as e:
        logger.error(f"Failed to create custom thread '{threadname}': {e}")
        await ctx.reply(f"Failed to create thread: {e}")
        return
    except Exception as e:
        logger.error(f"Unexpected error creating custom thread '{threadname}': {e}")
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
# ── Tracking shortcut commands                                               #
# --------------------------------------------------------------------------- #

# Mapping from shortcut target to config key
_INCLUDE_KEY_MAP = {
    "page": "pageIncludePatterns",
    "user": "userIncludeList",
    "summary": "summaryIncludePatterns",
}

_EXCLUDE_KEY_MAP = {
    "page": "pageExcludePatterns",
    "user": "userExcludeList",
    "summary": "summaryExcludePatterns",
}


@bot.hybrid_group(name="track",
                  invoke_without_command=True,
                  description="Add to include filters (shortcut for /config add)",
                  with_app_command=True)
async def track_group(ctx: commands.Context):
    """`/track` (no subcommand) -> show usage."""
    await ctx.reply("Usage: `/track page|user|summary <value>`")


@track_group.command(name="page",
                     description="Add a page pattern to include")
async def track_page_cmd(ctx: commands.Context, *, pattern: str):
    logger.info(f"Track page command from {ctx.author}: {pattern}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(pattern)
    ok, new_list = await mutate_custom_thread_config_list(
        ctx.channel.id, "pageIncludePatterns", add=vals
    )
    if ok:
        await ctx.reply(f"Now tracking pages matching: `{vals}`\n`pageIncludePatterns`: `{new_list}`")
    else:
        await ctx.reply("Failed to add pattern.")


@track_group.command(name="user",
                     description="Add a user to include")
async def track_user_cmd(ctx: commands.Context, *, username: str):
    logger.info(f"Track user command from {ctx.author}: {username}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(username)
    ok, new_list = await mutate_custom_thread_config_list(
        ctx.channel.id, "userIncludeList", add=vals
    )
    if ok:
        await ctx.reply(f"Now tracking user(s): `{vals}`\n`userIncludeList`: `{new_list}`")
    else:
        await ctx.reply("Failed to add user.")


@track_group.command(name="summary",
                     description="Add a summary pattern to include")
async def track_summary_cmd(ctx: commands.Context, *, pattern: str):
    logger.info(f"Track summary command from {ctx.author}: {pattern}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(pattern)
    ok, new_list = await mutate_custom_thread_config_list(
        ctx.channel.id, "summaryIncludePatterns", add=vals
    )
    if ok:
        await ctx.reply(f"Now tracking summaries matching: `{vals}`\n`summaryIncludePatterns`: `{new_list}`")
    else:
        await ctx.reply("Failed to add pattern.")


@bot.hybrid_group(name="ignore",
                  invoke_without_command=True,
                  description="Add to exclude filters (shortcut for /config add)",
                  with_app_command=True)
async def ignore_group(ctx: commands.Context):
    """`/ignore` (no subcommand) -> show usage."""
    await ctx.reply("Usage: `/ignore page|user|summary <value>`")


@ignore_group.command(name="page",
                      description="Add a page pattern to exclude")
async def ignore_page_cmd(ctx: commands.Context, *, pattern: str):
    logger.info(f"Ignore page command from {ctx.author}: {pattern}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(pattern)
    ok, new_list = await mutate_custom_thread_config_list(
        ctx.channel.id, "pageExcludePatterns", add=vals
    )
    if ok:
        await ctx.reply(f"Now ignoring pages matching: `{vals}`\n`pageExcludePatterns`: `{new_list}`")
    else:
        await ctx.reply("Failed to add pattern.")


@ignore_group.command(name="user",
                      description="Add a user to exclude")
async def ignore_user_cmd(ctx: commands.Context, *, username: str):
    logger.info(f"Ignore user command from {ctx.author}: {username}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(username)
    ok, new_list = await mutate_custom_thread_config_list(
        ctx.channel.id, "userExcludeList", add=vals
    )
    if ok:
        await ctx.reply(f"Now ignoring user(s): `{vals}`\n`userExcludeList`: `{new_list}`")
    else:
        await ctx.reply("Failed to add user.")


@ignore_group.command(name="summary",
                      description="Add a summary pattern to exclude")
async def ignore_summary_cmd(ctx: commands.Context, *, pattern: str):
    logger.info(f"Ignore summary command from {ctx.author}: {pattern}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(pattern)
    ok, new_list = await mutate_custom_thread_config_list(
        ctx.channel.id, "summaryExcludePatterns", add=vals
    )
    if ok:
        await ctx.reply(f"Now ignoring summaries matching: `{vals}`\n`summaryExcludePatterns`: `{new_list}`")
    else:
        await ctx.reply("Failed to add pattern.")


@bot.hybrid_group(name="untrack",
                  invoke_without_command=True,
                  description="Remove from include filters (shortcut for /config remove)",
                  with_app_command=True)
async def untrack_group(ctx: commands.Context):
    """`/untrack` (no subcommand) -> show usage."""
    await ctx.reply("Usage: `/untrack page|user|summary <value>`")


@untrack_group.command(name="page",
                       description="Remove a page pattern from include list")
async def untrack_page_cmd(ctx: commands.Context, *, pattern: str):
    logger.info(f"Untrack page command from {ctx.author}: {pattern}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(pattern)
    ok, new_list = await mutate_custom_thread_config_list(
        ctx.channel.id, "pageIncludePatterns", remove=vals
    )
    if ok:
        await ctx.reply(f"Removed page pattern(s): `{vals}`\n`pageIncludePatterns`: `{new_list}`")
    else:
        await ctx.reply("Failed to remove pattern.")


@untrack_group.command(name="user",
                       description="Remove a user from include list")
async def untrack_user_cmd(ctx: commands.Context, *, username: str):
    logger.info(f"Untrack user command from {ctx.author}: {username}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(username)
    ok, new_list = await mutate_custom_thread_config_list(
        ctx.channel.id, "userIncludeList", remove=vals
    )
    if ok:
        await ctx.reply(f"Removed user(s): `{vals}`\n`userIncludeList`: `{new_list}`")
    else:
        await ctx.reply("Failed to remove user.")


@untrack_group.command(name="summary",
                       description="Remove a summary pattern from include list")
async def untrack_summary_cmd(ctx: commands.Context, *, pattern: str):
    logger.info(f"Untrack summary command from {ctx.author}: {pattern}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(pattern)
    ok, new_list = await mutate_custom_thread_config_list(
        ctx.channel.id, "summaryIncludePatterns", remove=vals
    )
    if ok:
        await ctx.reply(f"Removed summary pattern(s): `{vals}`\n`summaryIncludePatterns`: `{new_list}`")
    else:
        await ctx.reply("Failed to remove pattern.")


@bot.hybrid_group(name="unignore",
                  invoke_without_command=True,
                  description="Remove from exclude filters (shortcut for /config remove)",
                  with_app_command=True)
async def unignore_group(ctx: commands.Context):
    """`/unignore` (no subcommand) -> show usage."""
    await ctx.reply("Usage: `/unignore page|user|summary <value>`")


@unignore_group.command(name="page",
                        description="Remove a page pattern from exclude list")
async def unignore_page_cmd(ctx: commands.Context, *, pattern: str):
    logger.info(f"Unignore page command from {ctx.author}: {pattern}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(pattern)
    ok, new_list = await mutate_custom_thread_config_list(
        ctx.channel.id, "pageExcludePatterns", remove=vals
    )
    if ok:
        await ctx.reply(f"Removed page exclude pattern(s): `{vals}`\n`pageExcludePatterns`: `{new_list}`")
    else:
        await ctx.reply("Failed to remove pattern.")


@unignore_group.command(name="user",
                        description="Remove a user from exclude list")
async def unignore_user_cmd(ctx: commands.Context, *, username: str):
    logger.info(f"Unignore user command from {ctx.author}: {username}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(username)
    ok, new_list = await mutate_custom_thread_config_list(
        ctx.channel.id, "userExcludeList", remove=vals
    )
    if ok:
        await ctx.reply(f"Removed user(s) from exclude list: `{vals}`\n`userExcludeList`: `{new_list}`")
    else:
        await ctx.reply("Failed to remove user.")


@unignore_group.command(name="summary",
                        description="Remove a summary pattern from exclude list")
async def unignore_summary_cmd(ctx: commands.Context, *, pattern: str):
    logger.info(f"Unignore summary command from {ctx.author}: {pattern}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(pattern)
    ok, new_list = await mutate_custom_thread_config_list(
        ctx.channel.id, "summaryExcludePatterns", remove=vals
    )
    if ok:
        await ctx.reply(f"Removed summary exclude pattern(s): `{vals}`\n`summaryExcludePatterns`: `{new_list}`")
    else:
        await ctx.reply("Failed to remove pattern.")


# --------------------------------------------------------------------------- #
# ── Status command (alias for /config get)                                   #
# --------------------------------------------------------------------------- #

@bot.hybrid_command(name="status",
                    description="Show current thread configuration",
                    with_app_command=True)
async def status_cmd(ctx: commands.Context):
    """Alias for `/config get` - shows current thread configuration."""
    logger.info(f"Status command from {ctx.author} in thread {ctx.channel.id}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    pretty = json.dumps(entry["config"], indent=JSON_INDENT)
    await ctx.reply(f"```json\n{pretty}\n```")


# --------------------------------------------------------------------------- #
# ── Receiver (webhook) commands                                               #
# --------------------------------------------------------------------------- #

def is_bot_owner(ctx) -> bool:
    """Check if user is the bot owner."""
    return ctx.author.id == bot.owner_id


@bot.hybrid_group(name="receiver",
                  invoke_without_command=True,
                  description="Manage webhook receivers (owner only)",
                  with_app_command=True)
@commands.check(is_bot_owner)
async def receiver_group(ctx: commands.Context):
    """`/receiver` (no subcommand) -> show usage."""
    await ctx.reply(
        "**Receiver Commands (owner only):**\n"
        "* `/receiver list` - List all receivers\n"
        "* `/receiver add <key> <name>` - Create a new receiver\n"
        "* `/receiver remove <key>` - Delete a receiver\n"
        "* `/receiver activate <key>` - Activate a receiver\n"
        "* `/receiver deactivate <key>` - Deactivate a receiver\n"
        "* `/receiver config <key>` - Show receiver config\n"
        "* `/receiver track <key> page|user|summary <value>` - Add to include filters\n"
        "* `/receiver ignore <key> page|user|summary <value>` - Add to exclude filters\n"
        "* `/receiver untrack <key> page|user|summary <value>` - Remove from include filters\n"
        "* `/receiver unignore <key> page|user|summary <value>` - Remove from exclude filters"
    )


@receiver_group.command(name="list",
                        description="List all receivers")
@commands.check(is_bot_owner)
async def receiver_list_cmd(ctx: commands.Context):
    """List all configured receivers."""
    logger.info(f"Receiver list command from {ctx.author}")
    async with CONFIG_LOCK:
        receivers = CONFIG.get("receivers", {})

    if not receivers:
        await ctx.reply("No receivers configured.")
        return

    lines = ["**Configured Receivers:**"]
    for key, entry in receivers.items():
        status_parts = []
        if entry.get("active"):
            status_parts.append("active")
        else:
            status_parts.append("inactive")
        if entry.get("errored"):
            status_parts.append("ERRORED")
        has_webhook = "webhook" if key in WEBHOOKS else "NO WEBHOOK"
        status = ", ".join(status_parts)
        lines.append(f"* `{key}`: {entry.get('name', '(unnamed)')} [{status}, {has_webhook}]")

    await ctx.reply("\n".join(lines))


@receiver_group.command(name="add",
                        description="Create a new receiver")
@commands.check(is_bot_owner)
async def receiver_add_cmd(ctx: commands.Context, key: str, *, name: str):
    """Create a new receiver with the given key and name."""
    logger.info(f"Receiver add command from {ctx.author}: key={key}, name={name}")
    ok, msg = await create_receiver(key, name)
    await ctx.reply(msg)


@receiver_group.command(name="remove",
                        description="Delete a receiver")
@commands.check(is_bot_owner)
async def receiver_remove_cmd(ctx: commands.Context, key: str):
    """Delete a receiver by key."""
    logger.info(f"Receiver remove command from {ctx.author}: key={key}")
    ok, msg = await delete_receiver(key)
    await ctx.reply(msg)


@receiver_group.command(name="activate",
                        description="Activate a receiver")
@commands.check(is_bot_owner)
async def receiver_activate_cmd(ctx: commands.Context, key: str):
    """Activate a receiver by key."""
    logger.info(f"Receiver activate command from {ctx.author}: key={key}")
    ok, msg = await set_receiver_active(key, True)
    await ctx.reply(msg)


@receiver_group.command(name="deactivate",
                        description="Deactivate a receiver")
@commands.check(is_bot_owner)
async def receiver_deactivate_cmd(ctx: commands.Context, key: str):
    """Deactivate a receiver by key."""
    logger.info(f"Receiver deactivate command from {ctx.author}: key={key}")
    ok, msg = await set_receiver_active(key, False)
    await ctx.reply(msg)


@receiver_group.command(name="config",
                        description="Show receiver configuration")
@commands.check(is_bot_owner)
async def receiver_config_cmd(ctx: commands.Context, key: str):
    """Show configuration for a receiver."""
    logger.info(f"Receiver config command from {ctx.author}: key={key}")
    entry = await get_receiver_entry(key)
    if not entry:
        await ctx.reply(f"Receiver `{key}` not found.")
        return
    pretty = json.dumps(entry, indent=JSON_INDENT)
    await ctx.reply(f"**Receiver `{key}`:**\n```json\n{pretty}\n```")


@receiver_group.command(name="setconfig",
                        description="Set a configuration key for a receiver")
@commands.check(is_bot_owner)
async def receiver_setconfig_cmd(ctx: commands.Context, key: str, config_key: str, *, value: str):
    """Set a configuration value for a receiver."""
    logger.info(f"Receiver setconfig command from {ctx.author}: {key}.{config_key} = {value}")
    entry = await get_receiver_entry(key)
    if not entry:
        await ctx.reply(f"Receiver `{key}` not found.")
        return
    parsed = _parse_json_arg(value)
    cfg = _deepcopy_cfg(entry["config"])
    cfg[config_key] = parsed
    ok = await update_receiver_config(key, cfg)
    if ok:
        await ctx.reply(f"Set `{config_key}` for receiver `{key}`: `{parsed}`")
    else:
        await ctx.reply("Failed to set configuration.")


# --------------------------------------------------------------------------- #
# ── Receiver track/ignore commands                                            #
# --------------------------------------------------------------------------- #

@receiver_group.group(name="track",
                      invoke_without_command=True,
                      description="Add to receiver include filters")
@commands.check(is_bot_owner)
async def receiver_track_group(ctx: commands.Context):
    """`/receiver track` (no subcommand) -> show usage."""
    await ctx.reply("Usage: `/receiver track page|user|summary <key> <value>`")


@receiver_track_group.command(name="page",
                              description="Add a page pattern to receiver include list")
@commands.check(is_bot_owner)
async def receiver_track_page_cmd(ctx: commands.Context, key: str, *, pattern: str):
    """Add a page pattern to receiver's include list."""
    logger.info(f"Receiver track page command from {ctx.author}: {key} += {pattern}")
    vals = _normalise_list_val(pattern)
    ok, new_list = await mutate_receiver_config_list(key, "pageIncludePatterns", add=vals)
    if ok:
        await ctx.reply(f"Receiver `{key}` now tracking pages: `{vals}`\n`pageIncludePatterns`: `{new_list}`")
    else:
        await ctx.reply(f"Receiver `{key}` not found.")


@receiver_track_group.command(name="user",
                              description="Add a user to receiver include list")
@commands.check(is_bot_owner)
async def receiver_track_user_cmd(ctx: commands.Context, key: str, *, username: str):
    """Add a user to receiver's include list."""
    logger.info(f"Receiver track user command from {ctx.author}: {key} += {username}")
    vals = _normalise_list_val(username)
    ok, new_list = await mutate_receiver_config_list(key, "userIncludeList", add=vals)
    if ok:
        await ctx.reply(f"Receiver `{key}` now tracking user(s): `{vals}`\n`userIncludeList`: `{new_list}`")
    else:
        await ctx.reply(f"Receiver `{key}` not found.")


@receiver_track_group.command(name="summary",
                              description="Add a summary pattern to receiver include list")
@commands.check(is_bot_owner)
async def receiver_track_summary_cmd(ctx: commands.Context, key: str, *, pattern: str):
    """Add a summary pattern to receiver's include list."""
    logger.info(f"Receiver track summary command from {ctx.author}: {key} += {pattern}")
    vals = _normalise_list_val(pattern)
    ok, new_list = await mutate_receiver_config_list(key, "summaryIncludePatterns", add=vals)
    if ok:
        await ctx.reply(f"Receiver `{key}` now tracking summaries: `{vals}`\n`summaryIncludePatterns`: `{new_list}`")
    else:
        await ctx.reply(f"Receiver `{key}` not found.")


@receiver_group.group(name="ignore",
                      invoke_without_command=True,
                      description="Add to receiver exclude filters")
@commands.check(is_bot_owner)
async def receiver_ignore_group(ctx: commands.Context):
    """`/receiver ignore` (no subcommand) -> show usage."""
    await ctx.reply("Usage: `/receiver ignore page|user|summary <key> <value>`")


@receiver_ignore_group.command(name="page",
                               description="Add a page pattern to receiver exclude list")
@commands.check(is_bot_owner)
async def receiver_ignore_page_cmd(ctx: commands.Context, key: str, *, pattern: str):
    """Add a page pattern to receiver's exclude list."""
    logger.info(f"Receiver ignore page command from {ctx.author}: {key} += {pattern}")
    vals = _normalise_list_val(pattern)
    ok, new_list = await mutate_receiver_config_list(key, "pageExcludePatterns", add=vals)
    if ok:
        await ctx.reply(f"Receiver `{key}` now ignoring pages: `{vals}`\n`pageExcludePatterns`: `{new_list}`")
    else:
        await ctx.reply(f"Receiver `{key}` not found.")


@receiver_ignore_group.command(name="user",
                               description="Add a user to receiver exclude list")
@commands.check(is_bot_owner)
async def receiver_ignore_user_cmd(ctx: commands.Context, key: str, *, username: str):
    """Add a user to receiver's exclude list."""
    logger.info(f"Receiver ignore user command from {ctx.author}: {key} += {username}")
    vals = _normalise_list_val(username)
    ok, new_list = await mutate_receiver_config_list(key, "userExcludeList", add=vals)
    if ok:
        await ctx.reply(f"Receiver `{key}` now ignoring user(s): `{vals}`\n`userExcludeList`: `{new_list}`")
    else:
        await ctx.reply(f"Receiver `{key}` not found.")


@receiver_ignore_group.command(name="summary",
                               description="Add a summary pattern to receiver exclude list")
@commands.check(is_bot_owner)
async def receiver_ignore_summary_cmd(ctx: commands.Context, key: str, *, pattern: str):
    """Add a summary pattern to receiver's exclude list."""
    logger.info(f"Receiver ignore summary command from {ctx.author}: {key} += {pattern}")
    vals = _normalise_list_val(pattern)
    ok, new_list = await mutate_receiver_config_list(key, "summaryExcludePatterns", add=vals)
    if ok:
        await ctx.reply(f"Receiver `{key}` now ignoring summaries: `{vals}`\n`summaryExcludePatterns`: `{new_list}`")
    else:
        await ctx.reply(f"Receiver `{key}` not found.")


@receiver_group.group(name="untrack",
                      invoke_without_command=True,
                      description="Remove from receiver include filters")
@commands.check(is_bot_owner)
async def receiver_untrack_group(ctx: commands.Context):
    """`/receiver untrack` (no subcommand) -> show usage."""
    await ctx.reply("Usage: `/receiver untrack page|user|summary <key> <value>`")


@receiver_untrack_group.command(name="page",
                                description="Remove a page pattern from receiver include list")
@commands.check(is_bot_owner)
async def receiver_untrack_page_cmd(ctx: commands.Context, key: str, *, pattern: str):
    """Remove a page pattern from receiver's include list."""
    logger.info(f"Receiver untrack page command from {ctx.author}: {key} -= {pattern}")
    vals = _normalise_list_val(pattern)
    ok, new_list = await mutate_receiver_config_list(key, "pageIncludePatterns", remove=vals)
    if ok:
        await ctx.reply(f"Removed page pattern(s) from receiver `{key}`: `{vals}`\n`pageIncludePatterns`: `{new_list}`")
    else:
        await ctx.reply(f"Receiver `{key}` not found.")


@receiver_untrack_group.command(name="user",
                                description="Remove a user from receiver include list")
@commands.check(is_bot_owner)
async def receiver_untrack_user_cmd(ctx: commands.Context, key: str, *, username: str):
    """Remove a user from receiver's include list."""
    logger.info(f"Receiver untrack user command from {ctx.author}: {key} -= {username}")
    vals = _normalise_list_val(username)
    ok, new_list = await mutate_receiver_config_list(key, "userIncludeList", remove=vals)
    if ok:
        await ctx.reply(f"Removed user(s) from receiver `{key}`: `{vals}`\n`userIncludeList`: `{new_list}`")
    else:
        await ctx.reply(f"Receiver `{key}` not found.")


@receiver_untrack_group.command(name="summary",
                                description="Remove a summary pattern from receiver include list")
@commands.check(is_bot_owner)
async def receiver_untrack_summary_cmd(ctx: commands.Context, key: str, *, pattern: str):
    """Remove a summary pattern from receiver's include list."""
    logger.info(f"Receiver untrack summary command from {ctx.author}: {key} -= {pattern}")
    vals = _normalise_list_val(pattern)
    ok, new_list = await mutate_receiver_config_list(key, "summaryIncludePatterns", remove=vals)
    if ok:
        await ctx.reply(f"Removed summary pattern(s) from receiver `{key}`: `{vals}`\n`summaryIncludePatterns`: `{new_list}`")
    else:
        await ctx.reply(f"Receiver `{key}` not found.")


@receiver_group.group(name="unignore",
                      invoke_without_command=True,
                      description="Remove from receiver exclude filters")
@commands.check(is_bot_owner)
async def receiver_unignore_group(ctx: commands.Context):
    """`/receiver unignore` (no subcommand) -> show usage."""
    await ctx.reply("Usage: `/receiver unignore page|user|summary <key> <value>`")


@receiver_unignore_group.command(name="page",
                                 description="Remove a page pattern from receiver exclude list")
@commands.check(is_bot_owner)
async def receiver_unignore_page_cmd(ctx: commands.Context, key: str, *, pattern: str):
    """Remove a page pattern from receiver's exclude list."""
    logger.info(f"Receiver unignore page command from {ctx.author}: {key} -= {pattern}")
    vals = _normalise_list_val(pattern)
    ok, new_list = await mutate_receiver_config_list(key, "pageExcludePatterns", remove=vals)
    if ok:
        await ctx.reply(f"Removed page exclude pattern(s) from receiver `{key}`: `{vals}`\n`pageExcludePatterns`: `{new_list}`")
    else:
        await ctx.reply(f"Receiver `{key}` not found.")


@receiver_unignore_group.command(name="user",
                                 description="Remove a user from receiver exclude list")
@commands.check(is_bot_owner)
async def receiver_unignore_user_cmd(ctx: commands.Context, key: str, *, username: str):
    """Remove a user from receiver's exclude list."""
    logger.info(f"Receiver unignore user command from {ctx.author}: {key} -= {username}")
    vals = _normalise_list_val(username)
    ok, new_list = await mutate_receiver_config_list(key, "userExcludeList", remove=vals)
    if ok:
        await ctx.reply(f"Removed user(s) from receiver `{key}` exclude list: `{vals}`\n`userExcludeList`: `{new_list}`")
    else:
        await ctx.reply(f"Receiver `{key}` not found.")


@receiver_unignore_group.command(name="summary",
                                 description="Remove a summary pattern from receiver exclude list")
@commands.check(is_bot_owner)
async def receiver_unignore_summary_cmd(ctx: commands.Context, key: str, *, pattern: str):
    """Remove a summary pattern from receiver's exclude list."""
    logger.info(f"Receiver unignore summary command from {ctx.author}: {key} -= {pattern}")
    vals = _normalise_list_val(pattern)
    ok, new_list = await mutate_receiver_config_list(key, "summaryExcludePatterns", remove=vals)
    if ok:
        await ctx.reply(f"Removed summary exclude pattern(s) from receiver `{key}`: `{vals}`\n`summaryExcludePatterns`: `{new_list}`")
    else:
        await ctx.reply(f"Receiver `{key}` not found.")


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
