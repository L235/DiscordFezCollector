"""Domain models: the filter config schema and its compiled matcher."""

import re
from dataclasses import dataclass, field
from re import RegexFlag, compile, search
from typing import Any, ClassVar


def normalise_str_list(v: Any) -> list[str]:
    """Coerce a user/JSON-supplied value into a list of strings."""
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]


@dataclass
class FilterConfig:
    """Schema for one filter config (custom thread or receiver).

    The single source of truth for the config keys: the camelCase names in
    JSON_KEYS are the on-disk format and the names users type in
    `/config set|add|remove`. from_dict() is tolerant (missing keys default,
    scalars coerce to lists, bad linkStyle falls back to "title") because
    configs are hand-edited via setraw and may predate newer keys.
    """

    site_name: str = ""
    page_include_patterns: list[str] = field(default_factory=list)
    page_exclude_patterns: list[str] = field(default_factory=list)
    user_exclude_list: list[str] = field(default_factory=list)
    user_include_list: list[str] = field(default_factory=list)
    summary_include_patterns: list[str] = field(default_factory=list)
    summary_exclude_patterns: list[str] = field(default_factory=list)
    link_style: str = "title"  # "title" = link page title, "action" = link action verb

    LINK_STYLES: ClassVar[tuple[str, ...]] = ("title", "action")

    #  JSON/config-file key → attribute name (declaration order = output order)
    JSON_KEYS: ClassVar[dict[str, str]] = {
        "siteName": "site_name",
        "pageIncludePatterns": "page_include_patterns",
        "pageExcludePatterns": "page_exclude_patterns",
        "userExcludeList": "user_exclude_list",
        "userIncludeList": "user_include_list",
        "summaryIncludePatterns": "summary_include_patterns",
        "summaryExcludePatterns": "summary_exclude_patterns",
        "linkStyle": "link_style",
    }
    #  The keys holding lists (everything except the two scalar keys)
    LIST_KEYS: ClassVar[frozenset[str]] = frozenset(
        k for k in JSON_KEYS if k not in ("siteName", "linkStyle")
    )

    @classmethod
    def from_dict(cls, cfg: dict) -> "FilterConfig":
        kwargs: dict[str, Any] = {}
        for json_key, attr in cls.JSON_KEYS.items():
            if json_key not in cfg:
                continue
            v = cfg[json_key]
            if json_key == "siteName":
                kwargs[attr] = str(v) if v else ""
            elif json_key == "linkStyle":
                kwargs[attr] = v if v in cls.LINK_STYLES else "title"
            else:
                kwargs[attr] = normalise_str_list(v)
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for json_key, attr in self.JSON_KEYS.items():
            v = getattr(self, attr)
            out[json_key] = list(v) if isinstance(v, list) else v
        return out


def _compile_pattern_list(patterns: list[str]) -> re.Pattern | None:
    """Combine a pattern list into one case-insensitive alternation regex.

    Tolerates list-ish input (scalars coerce to a single-element list) for
    direct callers; empty patterns are dropped.
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
        "link_style",
    )

    def __init__(self, cfg: "dict | FilterConfig"):
        fc = cfg if isinstance(cfg, FilterConfig) else FilterConfig.from_dict(cfg)
        self.site_name = fc.site_name
        self.page_include = _compile_pattern_list(fc.page_include_patterns)
        self.page_exclude = _compile_pattern_list(fc.page_exclude_patterns)
        self.sum_include = _compile_pattern_list(fc.summary_include_patterns)
        self.sum_exclude = _compile_pattern_list(fc.summary_exclude_patterns)
        self.user_include = set(fc.user_include_list)
        self.user_exclude = set(fc.user_exclude_list)
        self.link_style = fc.link_style

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
