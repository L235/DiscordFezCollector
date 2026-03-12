import os
import re
from re import RegexFlag, compile, search
from typing import List, Optional

# Timeout and retry configuration (for EventStreams)
class RetryConfig:
    INITIAL_BACKOFF_SECS = int(os.getenv("RETRY_INITIAL_BACKOFF_SECS", "5"))
    MAX_BACKOFF_SECS = int(os.getenv("RETRY_MAX_BACKOFF_SECS", "300"))
    GRACE_PERIOD_SECS = int(os.getenv("RETRY_GRACE_PERIOD_SECS", "5"))

# Discord API retry configuration (for 5xx server errors)
class DiscordRetryConfig:
    MAX_ATTEMPTS = int(os.getenv("DISCORD_RETRY_MAX_ATTEMPTS",
                       os.getenv("DISCORD_RATE_LIMIT_MAX_ATTEMPTS", "5")))
    INITIAL_BACKOFF_SECS = float(os.getenv("DISCORD_RETRY_INITIAL_BACKOFF_SECS",
                                 os.getenv("DISCORD_RATE_LIMIT_INITIAL_BACKOFF_SECS", "2")))
    MAX_BACKOFF_SECS = float(os.getenv("DISCORD_RETRY_MAX_BACKOFF_SECS",
                             os.getenv("DISCORD_RATE_LIMIT_MAX_BACKOFF_SECS", "60")))

# EventStream configuration
class EventStreamConfig:
    TIMEOUT_SECS = int(os.getenv("EVENTSTREAM_TIMEOUT_SECS", "600"))  # 10 minutes default
    CHECK_INTERVAL_SECS = int(os.getenv("EVENTSTREAM_CHECK_INTERVAL_SECS", "60"))
    QUEUE_MAX_SIZE = int(os.getenv("EVENTSTREAM_QUEUE_MAX_SIZE", "2000"))
    QUEUE_PUT_TIMEOUT_SECS = float(os.getenv("EVENTSTREAM_QUEUE_PUT_TIMEOUT_SECS", "30.0"))
    QUEUE_RESULT_TIMEOUT_SECS = float(os.getenv("EVENTSTREAM_QUEUE_RESULT_TIMEOUT_SECS", "35.0"))
    ERROR_BODY_LIMIT = int(os.getenv("EVENTSTREAM_ERROR_BODY_LIMIT", "2000"))

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
        self.site_name = cfg.get("siteName", "") or ""
        self.page_include = _compile_pattern_list(cfg.get("pageIncludePatterns", []))
        self.page_exclude = _compile_pattern_list(cfg.get("pageExcludePatterns", []))
        self.sum_include = _compile_pattern_list(cfg.get("summaryIncludePatterns", []))
        self.sum_exclude = _compile_pattern_list(cfg.get("summaryExcludePatterns", []))
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
