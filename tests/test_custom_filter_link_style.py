"""Tests for CustomFilter.link_style attribute."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models import CustomFilter


def test_default_link_style():
    filt = CustomFilter({"userIncludeList": ["TestUser"]})
    assert filt.link_style == "title"


def test_explicit_title_link_style():
    filt = CustomFilter({"userIncludeList": ["TestUser"], "linkStyle": "title"})
    assert filt.link_style == "title"


def test_action_link_style():
    filt = CustomFilter({"userIncludeList": ["TestUser"], "linkStyle": "action"})
    assert filt.link_style == "action"


def test_empty_string_defaults_to_title():
    filt = CustomFilter({"userIncludeList": ["TestUser"], "linkStyle": ""})
    assert filt.link_style == "title"
