"""Tests for multi-channel parent ID parsing."""

import pytest


class TestParseChannelIds:
    """Test DISCORD_CHANNEL_IDS parsing from env vars."""

    def test_plural_env_var_comma_separated(self, monkeypatch):
        monkeypatch.setenv("FEZ_COLLECTOR_CHANNEL_IDS", "111,222,333")
        monkeypatch.delenv("FEZ_COLLECTOR_CHANNEL_ID", raising=False)
        from src.config import parse_channel_ids_env
        result = parse_channel_ids_env()
        assert result == {111, 222, 333}

    def test_plural_env_var_single_value(self, monkeypatch):
        monkeypatch.setenv("FEZ_COLLECTOR_CHANNEL_IDS", "111")
        monkeypatch.delenv("FEZ_COLLECTOR_CHANNEL_ID", raising=False)
        from src.config import parse_channel_ids_env
        result = parse_channel_ids_env()
        assert result == {111}

    def test_singular_env_var_fallback(self, monkeypatch):
        monkeypatch.delenv("FEZ_COLLECTOR_CHANNEL_IDS", raising=False)
        monkeypatch.setenv("FEZ_COLLECTOR_CHANNEL_ID", "999")
        from src.config import parse_channel_ids_env
        result = parse_channel_ids_env()
        assert result == {999}

    def test_plural_wins_over_singular(self, monkeypatch):
        monkeypatch.setenv("FEZ_COLLECTOR_CHANNEL_IDS", "111,222")
        monkeypatch.setenv("FEZ_COLLECTOR_CHANNEL_ID", "999")
        from src.config import parse_channel_ids_env
        result = parse_channel_ids_env()
        assert result == {111, 222}

    def test_whitespace_trimmed(self, monkeypatch):
        monkeypatch.setenv("FEZ_COLLECTOR_CHANNEL_IDS", " 111 , 222 ")
        monkeypatch.delenv("FEZ_COLLECTOR_CHANNEL_ID", raising=False)
        from src.config import parse_channel_ids_env
        result = parse_channel_ids_env()
        assert result == {111, 222}

    def test_neither_env_var_returns_zero_set(self, monkeypatch):
        monkeypatch.delenv("FEZ_COLLECTOR_CHANNEL_IDS", raising=False)
        monkeypatch.delenv("FEZ_COLLECTOR_CHANNEL_ID", raising=False)
        from src.config import parse_channel_ids_env
        result = parse_channel_ids_env()
        assert result == {0}
