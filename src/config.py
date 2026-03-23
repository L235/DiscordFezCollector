import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

from src.constants import UTF8_ENCODING, JSON_INDENT, CONFIG_VERSION
from src.logging_setup import logger

# --------------------------------------------------------------------------- #
# ── Environment / runtime configuration                                      #
# --------------------------------------------------------------------------- #

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
        if not url.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")):
            logger.warning(f"Webhook URL doesn't match Discord webhook pattern: {url}")
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
    "linkStyle": "title",  # "title" = link page title, "action" = link action verb
}

# Config schema (see docstring):
DEFAULT_CONFIG: Dict[str, Any] = {"version": CONFIG_VERSION, "threads": {}, "receivers": {}}


def load_config() -> dict:
    if not STATE_FILE.exists():
        logger.info(f"Creating new config file at {STATE_FILE}")
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    logger.debug(f"Loading config from {STATE_FILE}")
    with STATE_FILE.open(encoding=UTF8_ENCODING) as fp:
        raw = json.load(fp)
    # ensure keys exist - older configs will silently keep working,
    # but we no longer mutate them in-place.
    raw.setdefault("threads", {})
    raw.setdefault("receivers", {})
    # Backfill missing keys from DEFAULT_CUSTOM_CONFIG into existing entries
    for entry in list(raw["threads"].values()) + list(raw.get("receivers", {}).values()):
        cfg = entry.get("config")
        if cfg is not None:
            for k, v in DEFAULT_CUSTOM_CONFIG.items():
                if k not in cfg:
                    cfg[k] = v if isinstance(v, str) else list(v)
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
# ── Helper functions for config mutation                                     #
# --------------------------------------------------------------------------- #

def _blank_custom_cfg() -> dict:
    return json.loads(json.dumps(DEFAULT_CUSTOM_CONFIG))  # deep copy

async def ensure_custom_thread_entry(thread_id: str, thread_name: str, *, create_if_missing: bool = False) -> Optional[dict]:
    """
    Ensure CONFIG entry exists for given thread. Returns entry (dict) or None.
    Modified to take ID and name strings instead of discord.Thread object to avoid discord dependency here?
    But the original took discord.Thread.
    Let's take ID and name to keep this module clean of discord imports if possible.
    """
    async with CONFIG_LOCK:
        entry = CONFIG["threads"].get(thread_id)
        if entry is None and create_if_missing:
            logger.info(f"Creating new config entry for thread {thread_name} ({thread_id})")
            entry = {
                "name": thread_name,
                "active": True,
                "config": _blank_custom_cfg(),
            }
            CONFIG["threads"][thread_id] = entry
            save_config(CONFIG)
        return entry

async def set_custom_thread_active(thread_id: str, active: bool) -> bool:
    async with CONFIG_LOCK:
        entry = CONFIG["threads"].get(thread_id)
        if not entry:
            logger.warning(f"Attempted to set active state for unknown thread {thread_id}")
            return False
        old_state = entry.get("active", False)
        entry["active"] = active
        save_config(CONFIG)
        logger.info(f"Thread {thread_id} active state changed: {old_state} -> {active}")
        return True

def _normalise_list_val(v: Any) -> List[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]

# --------------------------------------------------------------------------- #
# ── Unified entity operations                                               #
# --------------------------------------------------------------------------- #

async def get_entity_entry(entity_type: str, entity_id: str) -> Optional[dict]:
    """Get config entry for a thread or receiver by entity type and ID."""
    async with CONFIG_LOCK:
        return CONFIG.get(entity_type, {}).get(entity_id)


async def update_entity_config(entity_type: str, entity_id: str, new_cfg: dict) -> bool:
    """Replace an entity's config dict. Works for both threads and receivers."""
    async with CONFIG_LOCK:
        entity = CONFIG.get(entity_type, {}).get(entity_id)
        if not entity:
            logger.warning(f"Attempted to update config for unknown {entity_type} entry {entity_id}")
            return False
        entity["config"] = new_cfg
        save_config(CONFIG)
        logger.info(f"Updated config for {entity_type} entry {entity_id}")
        return True


async def mutate_entity_config_list(
    entity_type: str, entity_id: str, field: str,
    *, add: Optional[List[str]] = None,
    remove: Optional[List[str]] = None,
    clear: bool = False
) -> Tuple[bool, Optional[List[str]]]:
    """Mutate a list field in any entity's config. Works for both threads and receivers."""
    async with CONFIG_LOCK:
        entity = CONFIG.get(entity_type, {}).get(entity_id)
        if not entity:
            logger.warning(f"Attempted to mutate config for unknown {entity_type} entry {entity_id}")
            return False, None
        cfg = entity["config"]
        if field not in cfg:
            cfg[field] = []
        if clear:
            logger.info(f"Clearing {field} for {entity_type} entry {entity_id}")
            cfg[field] = []
        else:
            lst = list(cfg[field])
            if add:
                logger.info(f"Adding {add} to {field} for {entity_type} entry {entity_id}")
                for it in add:
                    if it not in lst:
                        lst.append(it)
            if remove:
                logger.info(f"Removing {remove} from {field} for {entity_type} entry {entity_id}")
                lst = [it for it in lst if it not in remove]
            cfg[field] = lst
        save_config(CONFIG)
        return True, list(cfg[field])


# --------------------------------------------------------------------------- #
# ── Backward-compatible wrappers (delegate to unified operations)           #
# --------------------------------------------------------------------------- #

async def update_custom_thread_config(thread_id: str, new_cfg: dict) -> bool:
    return await update_entity_config("threads", thread_id, new_cfg)

async def mutate_custom_thread_config_list(thread_id: str, key: str, *, add: Optional[List[str]] = None,
                                           remove: Optional[List[str]] = None, clear: bool = False) -> Tuple[bool, Optional[List[str]]]:
    return await mutate_entity_config_list("threads", thread_id, key, add=add, remove=remove, clear=clear)

def _validate_receiver_key(key: str) -> bool:
    """Validate receiver key format: alphanumeric + underscores only."""
    return bool(re.match(r'^[a-zA-Z0-9_]+$', key))

async def get_receiver_entry(key: str) -> Optional[dict]:
    """Get a receiver config entry by key."""
    return await get_entity_entry("receivers", key)

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
        logger.info(f"Receiver '{key}' {'activated' if active else 'deactivated'}")
    state = "activated" if active else "deactivated"
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
    return await update_entity_config("receivers", key, new_cfg)

async def mutate_receiver_config_list(key: str, field: str, *, add: Optional[List[str]] = None,
                                     remove: Optional[List[str]] = None, clear: bool = False) -> Tuple[bool, Optional[List[str]]]:
    """Mutate a list field in a receiver's config."""
    return await mutate_entity_config_list("receivers", key, field, add=add, remove=remove, clear=clear)
