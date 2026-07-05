"""Shared test factories: one definition of the canonical event and filter
shapes so routing and pipeline tests can't drift apart."""

from src.models import CustomFilter, FilterConfig


def make_change(**overrides) -> dict:
    """A representative recentchange edit event."""
    change = {
        "type": "edit",
        "user": "Alice",
        "title": "Sandbox",
        "comment": "test edit",
        "server_name": "en.wikipedia.org",
        "revision": {"new": 1},
    }
    change.update(overrides)
    return change


def make_filter(**camel_overrides) -> CustomFilter:
    """A CustomFilter built from camelCase config keys via the real schema."""
    return CustomFilter(FilterConfig.from_dict(camel_overrides))
