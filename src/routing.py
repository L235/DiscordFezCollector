"""Pure routing decisions for the event pipeline.

Given an EventStreams change record and the active filters, decide which
targets (custom threads / webhook receivers) should get the event. No I/O
here: everything is deterministic and unit-testable. The async plumbing
(queues, Discord sends, caches) lives in src/event_streams.py.
"""

from typing import TypeVar

from src.constants import (
    DISCORD_MESSAGE_LIMIT,
    TRUNCATED_MESSAGE_SUFFIX,
    TRUNCATION_RESERVE,
)
from src.logging_setup import logger
from src.models import CustomFilter

K = TypeVar("K")  # target key: thread id (int) or receiver key (str)


def event_comment(change: dict) -> str:
    """The comment text used for summary matching (log events carry it in
    log_action_comment)."""
    return change.get("log_action_comment") or change.get("comment") or ""


def fast_reject(change: dict, filters: list[CustomFilter]) -> bool:
    """Ultra-cheap global short-circuit: True when *no* filter could possibly
    match this change, so all per-target matching can be skipped.

    A change can only match some filter if its user is in some include list,
    or its title matches some page-include regex, or its comment matches some
    summary-include regex (CustomFilter.matches has no other accept paths).
    """
    user_whitelist = {u for f in filters for u in f.user_include}
    page_regexes = [f.page_include for f in filters if f.page_include]
    summary_regexes = [f.sum_include for f in filters if f.sum_include]

    title = change.get("title", "")
    comment = event_comment(change)

    return (
        change.get("user") not in user_whitelist
        and not any(rx.search(title) for rx in page_regexes)
        and not any(rx.search(comment) for rx in summary_regexes)
    )


def match_filters(
    change: dict,
    keyed_filters: list[tuple[K, CustomFilter]],
    error_label: str,
) -> list[tuple[K, str]]:
    """Return (key, link_style) for every filter that matches the change.

    A filter that raises is logged (with error_label naming the target kind)
    and skipped; one broken filter must not block the others.
    """
    matched: list[tuple[K, str]] = []
    for key, filt in keyed_filters:
        try:
            if filt.matches(change):
                matched.append((key, filt.link_style))
        except Exception as e:
            logger.warning(f"{error_label} {key!r}: {e}")
    return matched


def truncate_for_discord(msg: str) -> str:
    """Clamp a message to Discord's length limit, marking the truncation."""
    if len(msg) > DISCORD_MESSAGE_LIMIT:
        return msg[: DISCORD_MESSAGE_LIMIT - TRUNCATION_RESERVE] + TRUNCATED_MESSAGE_SUFFIX
    return msg
