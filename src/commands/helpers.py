import copy
import json
from enum import Enum
from typing import Any

import discord
from discord.ext import commands

from src.config import (
    DISCORD_CHANNEL_ID,
    OWNER_ID,
    _normalise_list_val,
    ensure_custom_thread_entry,
    mutate_entity_config_list,
)
from src.models import FilterConfig


def authorised(ctx) -> bool:
    """Gatekeeper: only allow the bot owner."""
    return ctx.author.id == OWNER_ID


# Alias kept for use with @commands.check
is_bot_owner = authorised


def _in_parent_channel(ctx: commands.Context) -> bool:
    """True when in the parent channel. TODO #18: change to `in` when DISCORD_CHANNEL_ID becomes a set."""
    return ctx.channel.id == DISCORD_CHANNEL_ID


def _in_custom_thread(ctx: commands.Context) -> bool:
    """True when inside any thread spawned from the parent channel."""
    return (
        isinstance(ctx.channel, discord.Thread)
        and ctx.channel.parent_id == DISCORD_CHANNEL_ID
    )


def _parse_json_arg(s: str) -> Any:
    try:
        return json.loads(s)
    except Exception:
        return s


def _deepcopy_cfg(obj: Any) -> Any:
    return copy.deepcopy(obj)


def validate_config_value(key: str, value: Any) -> tuple[str | None, Any]:
    """Validate a user-supplied `<key> = <value>` config assignment against the
    FilterConfig schema. Returns (error_message, normalized_value); error is
    None when valid. Rejecting unknown keys here stops typos from silently
    creating dead config entries.
    """
    if key not in FilterConfig.JSON_KEYS:
        valid = ", ".join(f"`{k}`" for k in FilterConfig.JSON_KEYS)
        return f"Unknown config key `{key}`. Valid keys: {valid}", None
    if key == "linkStyle":
        if value not in FilterConfig.LINK_STYLES:
            styles = ", ".join(f"`{s}`" for s in FilterConfig.LINK_STYLES)
            return f"`linkStyle` must be one of: {styles}", None
        return None, value
    if key == "siteName":
        if not isinstance(value, str):
            return "`siteName` must be a string.", None
        return None, value
    return None, _normalise_list_val(value)


def require_list_key(key: str) -> str | None:
    """Error message when *key* is not a list-valued config key; None if OK."""
    if key not in FilterConfig.LIST_KEYS:
        valid = ", ".join(f"`{k}`" for k in sorted(FilterConfig.LIST_KEYS))
        return f"`{key}` is not a list config key. List keys: {valid}"
    return None


async def _require_custom_thread(ctx: commands.Context) -> dict | None:
    if not _in_custom_thread(ctx):
        await ctx.reply("This command must be used inside a thread.")
        return None
    return await ensure_custom_thread_entry(str(ctx.channel.id), ctx.channel.name, create_if_missing=True)


class FilterType(Enum):
    PAGE = "page"
    USER = "user"
    SUMMARY = "summary"


_INCLUDE_KEY_MAP = {
    FilterType.PAGE: "pageIncludePatterns",
    FilterType.USER: "userIncludeList",
    FilterType.SUMMARY: "summaryIncludePatterns",
}

_EXCLUDE_KEY_MAP = {
    FilterType.PAGE: "pageExcludePatterns",
    FilterType.USER: "userExcludeList",
    FilterType.SUMMARY: "summaryExcludePatterns",
}

# Action to (key_map, mutation_kwargs_key) mapping
_ACTION_MAP = {
    "add_include": (_INCLUDE_KEY_MAP, "add"),
    "remove_include": (_INCLUDE_KEY_MAP, "remove"),
    "add_exclude": (_EXCLUDE_KEY_MAP, "add"),
    "remove_exclude": (_EXCLUDE_KEY_MAP, "remove"),
}

_ACTION_LABELS = {
    "add_include": "Now tracking",
    "remove_include": "Removed from tracking",
    "add_exclude": "Now ignoring",
    "remove_exclude": "Removed from ignore list",
}


async def mutate_filter(
    ctx: commands.Context,
    entity_type: str,
    entity_id: str,
    target: FilterType,
    value: str,
    *,
    action: str,
    entity_label: str = "",
) -> None:
    """
    Single function handling all track/ignore/untrack/unignore for any entity type.
    """
    key_map, kwarg = _ACTION_MAP[action]
    config_key = key_map[target]
    vals = _normalise_list_val(value)

    ok, new_list = await mutate_entity_config_list(
        entity_type, entity_id, config_key, **{kwarg: vals}
    )
    label = _ACTION_LABELS[action]
    prefix = f"{entity_label} " if entity_label else ""
    if ok:
        await ctx.reply(f"{prefix}{label} {target.value}: `{vals}`\n`{config_key}`: `{new_list}`")
    else:
        await ctx.reply(f"{prefix}Failed to update {target.value}.")
