"""Tests for format_change with link_style parameter."""
import os
import sys
from pathlib import Path

# Set required env vars before importing anything from the project
os.environ.setdefault("FEZ_COLLECTOR_DISCORD_TOKEN", "test-token")
os.environ.setdefault("FEZ_COLLECTOR_CHANNEL_ID", "123456")

# Add project root to sys.path so we can import the function directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.event_streams import format_change


# ── Fixtures ──────────────────────────────────────────────────────────────── #

def _edit_change():
    return {
        "type": "edit",
        "user": "ExampleUser",
        "title": "Test Page",
        "comment": "Fixed typo",
        "server_name": "en.wikipedia.org",
        "revision": {"new": 12345},
    }


def _new_change():
    return {
        "type": "new",
        "user": "ExampleUser",
        "title": "New Page",
        "comment": "Created page",
        "server_name": "en.wikipedia.org",
    }


def _categorize_change():
    return {
        "type": "categorize",
        "user": "ExampleUser",
        "title": "Category:Test",
        "comment": "Added category",
        "server_name": "en.wikipedia.org",
    }


def _log_change():
    return {
        "type": "log",
        "user": "ExampleUser",
        "title": "User:Vandal",
        "comment": "blocked",
        "log_action_comment": "blocked User:Vandal",
        "server_name": "en.wikipedia.org",
        "log_id": 999,
    }


def _fallback_change():
    return {
        "type": "unknown_type",
        "user": "ExampleUser",
        "title": "Some Page",
        "comment": "something",
        "server_name": "en.wikipedia.org",
    }


# ── Title style (default) ────────────────────────────────────────────────── #

def test_edit_title_style():
    msg = format_change(_edit_change())
    assert "**[Test Page](<https://en.wikipedia.org/wiki/Test_Page>)**" in msg
    assert "edited" in msg
    assert "diff=12345" in msg


def test_new_title_style():
    msg = format_change(_new_change())
    assert "**[New Page](<https://en.wikipedia.org/wiki/New_Page>)**" in msg
    assert "created" in msg


def test_categorize_title_style():
    msg = format_change(_categorize_change())
    assert "**[Category:Test](<https://en.wikipedia.org/wiki/Category:Test>)**" in msg
    assert "categorized" in msg


def test_fallback_title_style():
    msg = format_change(_fallback_change())
    assert "**[Some Page](<https://en.wikipedia.org/wiki/Some_Page>)**" in msg
    assert "modified" in msg


# ── Action style ──────────────────────────────────────────────────────────── #

def test_edit_action_style():
    msg = format_change(_edit_change(), link_style="action")
    # Verb should be linked to the diff URL
    assert "[edited](<https://en.wikipedia.org/w/index.php?diff=12345>)" in msg
    # Title should be bold but not linked
    assert "**Test Page**" in msg
    # No raw diff URL at the end
    assert msg.count("diff=12345") == 1


def test_new_action_style():
    msg = format_change(_new_change(), link_style="action")
    assert "[created](<https://en.wikipedia.org/wiki/New_Page>)" in msg
    assert "**New Page**" in msg


def test_categorize_action_style():
    msg = format_change(_categorize_change(), link_style="action")
    assert "[categorized](<https://en.wikipedia.org/wiki/Category:Test>)" in msg
    assert "**Category:Test**" in msg


def test_fallback_action_style():
    msg = format_change(_fallback_change(), link_style="action")
    assert "[modified](<https://en.wikipedia.org/wiki/Some_Page>)" in msg
    assert "**Some Page**" in msg


# ── Log type is unchanged by link_style ───────────────────────────────────── #

def test_log_title_style():
    msg = format_change(_log_change())
    assert "blocked User:Vandal" in msg
    assert "Special:Log&logid=999" in msg


def test_log_action_style():
    """Log messages should be the same regardless of link_style."""
    msg_title = format_change(_log_change(), link_style="title")
    msg_action = format_change(_log_change(), link_style="action")
    assert msg_title == msg_action


# ── Default link_style is "title" ─────────────────────────────────────────── #

def test_default_is_title_style():
    msg_default = format_change(_edit_change())
    msg_title = format_change(_edit_change(), link_style="title")
    assert msg_default == msg_title
