"""Comprehensive tests for src.config entity operations."""

import json
from pathlib import Path

import pytest

import src.config as config_mod
from src.config import (
    CONFIG,
    _normalise_list_val,
    load_config,
    parse_webhooks_env,
    save_config,
)


# ---------------------------------------------------------------------------
# Fixture: isolate every test from the real state file / CONFIG dict
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """
    Redirect STATE_FILE to a temp path, reset CONFIG to a clean baseline,
    and persist it so every test starts from a known state.
    """
    temp_state = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "STATE_FILE", temp_state)

    CONFIG.clear()
    CONFIG.update({"version": "0.9", "threads": {}, "receivers": {}})
    save_config(CONFIG)


# ===================================================================== #
#  TestNormaliseListVal                                                  #
# ===================================================================== #

class TestNormaliseListVal:
    def test_string_becomes_single_element_list(self):
        assert _normalise_list_val("hello") == ["hello"]

    def test_list_passes_through(self):
        assert _normalise_list_val(["a", "b"]) == ["a", "b"]

    def test_list_elements_coerced_to_str(self):
        assert _normalise_list_val([1, 2, 3]) == ["1", "2", "3"]

    def test_non_list_non_string_wrapped(self):
        assert _normalise_list_val(42) == ["42"]

    def test_empty_list(self):
        assert _normalise_list_val([]) == []


# ===================================================================== #
#  TestParseWebhooksEnv                                                 #
# ===================================================================== #

class TestParseWebhooksEnv:
    def test_empty_string_returns_empty(self, monkeypatch):
        monkeypatch.setenv("FEZ_WEBHOOKS", "")
        assert parse_webhooks_env() == {}

    def test_valid_single_webhook(self, monkeypatch):
        url = "https://discord.com/api/webhooks/123/abc"
        monkeypatch.setenv("FEZ_WEBHOOKS", f"mykey={url}")
        result = parse_webhooks_env()
        assert result == {"mykey": url}

    def test_multiple_webhooks(self, monkeypatch):
        url1 = "https://discord.com/api/webhooks/111/aaa"
        url2 = "https://discordapp.com/api/webhooks/222/bbb"
        monkeypatch.setenv("FEZ_WEBHOOKS", f"k1={url1},k2={url2}")
        result = parse_webhooks_env()
        assert result == {"k1": url1, "k2": url2}

    def test_invalid_http_url_skipped(self, monkeypatch):
        monkeypatch.setenv("FEZ_WEBHOOKS", "bad=http://discord.com/api/webhooks/1/a")
        assert parse_webhooks_env() == {}

    def test_missing_equals_skipped(self, monkeypatch):
        monkeypatch.setenv("FEZ_WEBHOOKS", "noequalssign")
        assert parse_webhooks_env() == {}

    def test_non_discord_https_url_skipped(self, monkeypatch):
        monkeypatch.setenv("FEZ_WEBHOOKS", "bad=https://example.com/hook")
        assert parse_webhooks_env() == {}


# ===================================================================== #
#  TestGetEntityEntry                                                   #
# ===================================================================== #

class TestGetEntityEntry:
    @pytest.mark.asyncio
    async def test_get_existing_thread(self):
        CONFIG["threads"]["t1"] = {"name": "Thread 1", "config": {}}
        from src.config import get_entity_entry

        result = await get_entity_entry("threads", "t1")
        assert result is not None
        assert result["name"] == "Thread 1"

    @pytest.mark.asyncio
    async def test_get_missing_thread(self):
        from src.config import get_entity_entry

        result = await get_entity_entry("threads", "nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_existing_receiver(self):
        CONFIG["receivers"]["r1"] = {"name": "Receiver 1", "config": {}}
        from src.config import get_entity_entry

        result = await get_entity_entry("receivers", "r1")
        assert result is not None
        assert result["name"] == "Receiver 1"

    @pytest.mark.asyncio
    async def test_get_missing_receiver(self):
        from src.config import get_entity_entry

        result = await get_entity_entry("receivers", "nope")
        assert result is None


# ===================================================================== #
#  TestUpdateEntityConfig                                               #
# ===================================================================== #

class TestUpdateEntityConfig:
    @pytest.mark.asyncio
    async def test_update_thread_config(self):
        CONFIG["threads"]["t1"] = {"name": "T", "config": {"old": True}}
        from src.config import update_entity_config

        ok = await update_entity_config("threads", "t1", {"new": True})
        assert ok is True
        assert CONFIG["threads"]["t1"]["config"] == {"new": True}

    @pytest.mark.asyncio
    async def test_update_missing_entity(self):
        from src.config import update_entity_config

        ok = await update_entity_config("threads", "missing", {"x": 1})
        assert ok is False

    @pytest.mark.asyncio
    async def test_update_receiver_config(self):
        CONFIG["receivers"]["r1"] = {"name": "R", "config": {"old": True}}
        from src.config import update_entity_config

        ok = await update_entity_config("receivers", "r1", {"new": True})
        assert ok is True
        assert CONFIG["receivers"]["r1"]["config"] == {"new": True}

    @pytest.mark.asyncio
    async def test_update_persists_to_disk(self):
        CONFIG["threads"]["t1"] = {"name": "T", "config": {"before": True}}
        from src.config import update_entity_config

        await update_entity_config("threads", "t1", {"after": True})

        # Reload from disk
        raw = json.loads(config_mod.STATE_FILE.read_text(encoding="utf-8"))
        assert raw["threads"]["t1"]["config"] == {"after": True}


# ===================================================================== #
#  TestMutateEntityConfigList                                           #
# ===================================================================== #

class TestMutateEntityConfigList:
    @pytest.mark.asyncio
    async def test_add_to_list(self):
        CONFIG["threads"]["t1"] = {"name": "T", "config": {"items": ["a"]}}
        from src.config import mutate_entity_config_list

        ok, lst = await mutate_entity_config_list("threads", "t1", "items", add=["b"])
        assert ok is True
        assert lst == ["a", "b"]

    @pytest.mark.asyncio
    async def test_add_deduplicates(self):
        CONFIG["threads"]["t1"] = {"name": "T", "config": {"items": ["a", "b"]}}
        from src.config import mutate_entity_config_list

        ok, lst = await mutate_entity_config_list("threads", "t1", "items", add=["b"])
        assert ok is True
        assert lst == ["a", "b"]  # no duplicate

    @pytest.mark.asyncio
    async def test_remove_from_list(self):
        CONFIG["threads"]["t1"] = {"name": "T", "config": {"items": ["a", "b", "c"]}}
        from src.config import mutate_entity_config_list

        ok, lst = await mutate_entity_config_list("threads", "t1", "items", remove=["b"])
        assert ok is True
        assert lst == ["a", "c"]

    @pytest.mark.asyncio
    async def test_clear_list(self):
        CONFIG["threads"]["t1"] = {"name": "T", "config": {"items": ["a", "b"]}}
        from src.config import mutate_entity_config_list

        ok, lst = await mutate_entity_config_list("threads", "t1", "items", clear=True)
        assert ok is True
        assert lst == []

    @pytest.mark.asyncio
    async def test_missing_entity(self):
        from src.config import mutate_entity_config_list

        ok, lst = await mutate_entity_config_list("threads", "missing", "items", add=["x"])
        assert ok is False
        assert lst is None

    @pytest.mark.asyncio
    async def test_creates_missing_key(self):
        CONFIG["threads"]["t1"] = {"name": "T", "config": {}}
        from src.config import mutate_entity_config_list

        ok, lst = await mutate_entity_config_list("threads", "t1", "new_field", add=["x"])
        assert ok is True
        assert lst == ["x"]
        assert CONFIG["threads"]["t1"]["config"]["new_field"] == ["x"]

    @pytest.mark.asyncio
    async def test_works_for_receivers(self):
        CONFIG["receivers"]["r1"] = {"name": "R", "config": {"tags": ["one"]}}
        from src.config import mutate_entity_config_list

        ok, lst = await mutate_entity_config_list("receivers", "r1", "tags", add=["two"])
        assert ok is True
        assert lst == ["one", "two"]

    @pytest.mark.asyncio
    async def test_remove_nonexistent_value_is_noop(self):
        CONFIG["threads"]["t1"] = {"name": "T", "config": {"items": ["a", "b"]}}
        from src.config import mutate_entity_config_list

        ok, lst = await mutate_entity_config_list("threads", "t1", "items", remove=["z"])
        assert ok is True
        assert lst == ["a", "b"]


# ===================================================================== #
#  TestConfigPersistence                                                #
# ===================================================================== #

class TestConfigPersistence:
    def test_save_and_reload_from_disk(self):
        CONFIG["threads"]["t1"] = {"name": "T", "config": {}}
        save_config(CONFIG)

        raw = json.loads(config_mod.STATE_FILE.read_text(encoding="utf-8"))
        assert "t1" in raw["threads"]

    def test_load_missing_file_creates_default(self, tmp_path, monkeypatch):
        missing = tmp_path / "subdir" / "state.json"
        monkeypatch.setattr(config_mod, "STATE_FILE", missing)

        loaded = load_config()
        assert missing.exists()
        assert "threads" in loaded
        assert "receivers" in loaded

    def test_load_old_config_without_receivers_key(self, tmp_path, monkeypatch):
        old_file = tmp_path / "old_config.json"
        old_file.write_text(json.dumps({"version": "0.9", "threads": {}}))
        monkeypatch.setattr(config_mod, "STATE_FILE", old_file)

        loaded = load_config()
        assert "receivers" in loaded
