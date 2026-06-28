"""Operator-facing failure notices posted to the main channel.

These cover the pure message formatters (the side-effecting sends are wired in
the worker / on_ready and verified by integration/manual review).
"""
from src.event_streams import (
    format_thread_disabled_notice,
    format_startup_notice,
    format_eventstream_timeout_notice,
)


def test_thread_disabled_notice_names_thread_and_reason():
    note = format_thread_disabled_notice(1397950387641909278, "thread not found (deleted?)")
    assert "1397950387641909278" in note          # the thread id
    assert "thread not found (deleted?)" in note    # why


def test_startup_notice_is_nonempty_and_signals_start():
    note = format_startup_notice()
    assert isinstance(note, str) and note.strip()
    assert "start" in note.lower()


def test_eventstream_timeout_notice_includes_duration_and_restart():
    note = format_eventstream_timeout_notice(600)
    assert "600" in note
    assert "restart" in note.lower()
