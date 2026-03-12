# Commands Refactor & Test Foundation — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deduplicate the commands layer, unify config CRUD, split commands.py into focused modules, add comprehensive tests, and auto-generate help text.

**Architecture:** Generic entity-based config operations replace duplicated thread/receiver functions. Commands become thin wrappers over shared helpers. Tests cover config mutation, filter matching, and command helpers as a red-green-refactor safety net.

**Tech Stack:** Python 3, discord.py (hybrid commands), pytest, asyncio

---

### Task 1: Add pytest and fix import-time side effects

**Files:**
- Modify: `requirements.txt`
- Modify: `src/config.py:29-33`
- Modify: `src/main.py:8,84-91`
- Create: `tests/conftest.py`
- Create: `pytest.ini`

**Step 1: Add pytest to requirements.txt**

Add `pytest` to `requirements.txt`:

```
pywikibot
discord.py
boto3
requests-sse
sseclient
pytest
```

**Step 2: Install pytest**

Run: `venv/bin/pip install pytest`

**Step 3: Replace sys.exit in config.py with validate_env()**

In `src/config.py`, replace lines 29-33:

```python
if not DISCORD_TOKEN or not DISCORD_CHANNEL_ID:
    logger.error("FEZ_COLLECTOR_DISCORD_TOKEN or FEZ_COLLECTOR_CHANNEL_ID missing")
    # We might not want to exit here if we are just importing this module for testing or partial usage
    # but the original code did exit. Let's keep it for now.
    sys.exit("FEZ_COLLECTOR_DISCORD_TOKEN or FEZ_COLLECTOR_CHANNEL_ID missing")
```

With:

```python
def validate_env() -> None:
    """Validate required environment variables. Call from main(), not at import time."""
    if not DISCORD_TOKEN or not DISCORD_CHANNEL_ID:
        logger.error("FEZ_COLLECTOR_DISCORD_TOKEN or FEZ_COLLECTOR_CHANNEL_ID missing")
        sys.exit("FEZ_COLLECTOR_DISCORD_TOKEN or FEZ_COLLECTOR_CHANNEL_ID missing")
```

**Step 4: Call validate_env() from main()**

In `src/main.py`, add `validate_env` to the import from `src.config` (line 8), then call it at the start of `main()` (line 84):

```python
from src.config import DISCORD_TOKEN, DISCORD_CHANNEL_ID, validate_env

def main():
    validate_env()
    # ... rest of main
```

**Step 5: Create pytest.ini**

```ini
[pytest]
testpaths = tests
asyncio_mode = auto
```

**Step 6: Create tests/conftest.py**

```python
import os

# Set dummy env vars BEFORE any src imports so config.py doesn't get empty values
os.environ.setdefault("FEZ_COLLECTOR_DISCORD_TOKEN", "test-token")
os.environ.setdefault("FEZ_COLLECTOR_CHANNEL_ID", "123456789")
os.environ.setdefault("FEZ_OWNER_ID", "987654321")
```

**Step 7: Verify imports work in test context**

Run: `venv/bin/python -m pytest tests/ --collect-only`
Expected: pytest collects without import errors (no tests to run yet, but no crashes)

**Step 8: Commit**

```bash
git add requirements.txt src/config.py src/main.py tests/conftest.py pytest.ini
git commit -m "Fix import-time side effects and add pytest infrastructure"
```

---

### Task 2: Unify config.py entity operations

**Files:**
- Modify: `src/config.py:147-322`
- Modify: `src/commands.py` (update imports — but we'll do this in Task 4 when splitting)

**Step 1: Write failing tests for unified entity operations**

Create `tests/test_config.py`:

```python
import json
import pytest
from pathlib import Path

from src.config import (
    CONFIG,
    CONFIG_LOCK,
    save_config,
    load_config,
    _normalise_list_val,
    _blank_custom_cfg,
    parse_webhooks_env,
    STATE_FILE,
)


# ── Fixtures ──────────────────────────────────────────────────────────────── #

@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """Use a temp file for STATE_FILE and reset CONFIG between tests."""
    import src.config as cfg_mod

    temp_state = tmp_path / "config.json"
    monkeypatch.setattr(cfg_mod, "STATE_FILE", temp_state)

    # Reset CONFIG to a clean default
    CONFIG.clear()
    CONFIG.update({"version": "0.9", "threads": {}, "receivers": {}})
    save_config(CONFIG)


# ── _normalise_list_val ───────────────────────────────────────────────────── #

class TestNormaliseListVal:
    def test_string_becomes_single_element_list(self):
        assert _normalise_list_val("hello") == ["hello"]

    def test_list_passes_through(self):
        assert _normalise_list_val(["a", "b"]) == ["a", "b"]

    def test_list_elements_coerced_to_str(self):
        assert _normalise_list_val([1, 2]) == ["1", "2"]

    def test_non_list_non_string_wrapped(self):
        assert _normalise_list_val(42) == ["42"]

    def test_empty_list(self):
        assert _normalise_list_val([]) == []


# ── parse_webhooks_env ────────────────────────────────────────────────────── #

class TestParseWebhooksEnv:
    def test_empty_string(self, monkeypatch):
        monkeypatch.setenv("FEZ_WEBHOOKS", "")
        assert parse_webhooks_env() == {}

    def test_valid_single_webhook(self, monkeypatch):
        monkeypatch.setenv("FEZ_WEBHOOKS", "mykey=https://discord.com/api/webhooks/123/abc")
        result = parse_webhooks_env()
        assert result == {"mykey": "https://discord.com/api/webhooks/123/abc"}

    def test_multiple_webhooks(self, monkeypatch):
        monkeypatch.setenv(
            "FEZ_WEBHOOKS",
            "a=https://discord.com/api/webhooks/1/x,b=https://discord.com/api/webhooks/2/y"
        )
        result = parse_webhooks_env()
        assert len(result) == 2
        assert "a" in result and "b" in result

    def test_invalid_url_skipped(self, monkeypatch):
        monkeypatch.setenv("FEZ_WEBHOOKS", "bad=http://example.com")
        assert parse_webhooks_env() == {}

    def test_missing_equals_skipped(self, monkeypatch):
        monkeypatch.setenv("FEZ_WEBHOOKS", "noequalssign")
        assert parse_webhooks_env() == {}

    def test_non_discord_https_skipped(self, monkeypatch):
        monkeypatch.setenv("FEZ_WEBHOOKS", "key=https://example.com/webhook")
        assert parse_webhooks_env() == {}


# ── Unified entity operations ─────────────────────────────────────────────── #

class TestGetEntityEntry:
    @pytest.mark.asyncio
    async def test_get_existing_thread(self):
        from src.config import get_entity_entry
        CONFIG["threads"]["111"] = {"name": "test", "active": True, "config": _blank_custom_cfg()}
        entry = await get_entity_entry("threads", "111")
        assert entry is not None
        assert entry["name"] == "test"

    @pytest.mark.asyncio
    async def test_get_missing_thread(self):
        from src.config import get_entity_entry
        entry = await get_entity_entry("threads", "999")
        assert entry is None

    @pytest.mark.asyncio
    async def test_get_existing_receiver(self):
        from src.config import get_entity_entry
        CONFIG["receivers"]["mykey"] = {"name": "test recv", "active": False, "config": _blank_custom_cfg()}
        entry = await get_entity_entry("receivers", "mykey")
        assert entry is not None
        assert entry["name"] == "test recv"

    @pytest.mark.asyncio
    async def test_get_missing_receiver(self):
        from src.config import get_entity_entry
        entry = await get_entity_entry("receivers", "nope")
        assert entry is None


class TestUpdateEntityConfig:
    @pytest.mark.asyncio
    async def test_update_thread_config(self):
        from src.config import update_entity_config
        CONFIG["threads"]["111"] = {"name": "t", "active": True, "config": _blank_custom_cfg()}
        new_cfg = {"siteName": "en.wikipedia.org", "pageIncludePatterns": ["Test.*"]}
        ok = await update_entity_config("threads", "111", new_cfg)
        assert ok is True
        assert CONFIG["threads"]["111"]["config"]["siteName"] == "en.wikipedia.org"

    @pytest.mark.asyncio
    async def test_update_missing_entity_returns_false(self):
        from src.config import update_entity_config
        ok = await update_entity_config("threads", "999", {})
        assert ok is False

    @pytest.mark.asyncio
    async def test_update_receiver_config(self):
        from src.config import update_entity_config
        CONFIG["receivers"]["rk"] = {"name": "r", "active": False, "config": _blank_custom_cfg()}
        ok = await update_entity_config("receivers", "rk", {"siteName": "test"})
        assert ok is True
        assert CONFIG["receivers"]["rk"]["config"]["siteName"] == "test"

    @pytest.mark.asyncio
    async def test_update_persists_to_disk(self, tmp_path):
        from src.config import update_entity_config
        import src.config as cfg_mod
        CONFIG["threads"]["111"] = {"name": "t", "active": True, "config": _blank_custom_cfg()}
        await update_entity_config("threads", "111", {"siteName": "persisted"})
        # Reload from disk
        raw = json.loads(cfg_mod.STATE_FILE.read_text())
        assert raw["threads"]["111"]["config"]["siteName"] == "persisted"


class TestMutateEntityConfigList:
    @pytest.mark.asyncio
    async def test_add_to_list(self):
        from src.config import mutate_entity_config_list
        CONFIG["threads"]["111"] = {"name": "t", "active": True, "config": _blank_custom_cfg()}
        ok, new_list = await mutate_entity_config_list("threads", "111", "userIncludeList", add=["Alice"])
        assert ok is True
        assert new_list == ["Alice"]

    @pytest.mark.asyncio
    async def test_add_deduplicates(self):
        from src.config import mutate_entity_config_list
        cfg = _blank_custom_cfg()
        cfg["userIncludeList"] = ["Alice"]
        CONFIG["threads"]["111"] = {"name": "t", "active": True, "config": cfg}
        ok, new_list = await mutate_entity_config_list("threads", "111", "userIncludeList", add=["Alice", "Bob"])
        assert ok is True
        assert new_list == ["Alice", "Bob"]

    @pytest.mark.asyncio
    async def test_remove_from_list(self):
        from src.config import mutate_entity_config_list
        cfg = _blank_custom_cfg()
        cfg["userIncludeList"] = ["Alice", "Bob"]
        CONFIG["threads"]["111"] = {"name": "t", "active": True, "config": cfg}
        ok, new_list = await mutate_entity_config_list("threads", "111", "userIncludeList", remove=["Alice"])
        assert ok is True
        assert new_list == ["Bob"]

    @pytest.mark.asyncio
    async def test_clear_list(self):
        from src.config import mutate_entity_config_list
        cfg = _blank_custom_cfg()
        cfg["pageIncludePatterns"] = ["foo", "bar"]
        CONFIG["threads"]["111"] = {"name": "t", "active": True, "config": cfg}
        ok, new_list = await mutate_entity_config_list("threads", "111", "pageIncludePatterns", clear=True)
        assert ok is True
        assert new_list == []

    @pytest.mark.asyncio
    async def test_missing_entity_returns_false(self):
        from src.config import mutate_entity_config_list
        ok, new_list = await mutate_entity_config_list("threads", "999", "userIncludeList", add=["X"])
        assert ok is False
        assert new_list is None

    @pytest.mark.asyncio
    async def test_creates_missing_key(self):
        from src.config import mutate_entity_config_list
        CONFIG["threads"]["111"] = {"name": "t", "active": True, "config": {}}
        ok, new_list = await mutate_entity_config_list("threads", "111", "newField", add=["val"])
        assert ok is True
        assert new_list == ["val"]

    @pytest.mark.asyncio
    async def test_works_for_receivers(self):
        from src.config import mutate_entity_config_list
        CONFIG["receivers"]["rk"] = {"name": "r", "active": True, "config": _blank_custom_cfg()}
        ok, new_list = await mutate_entity_config_list("receivers", "rk", "userExcludeList", add=["Spammer"])
        assert ok is True
        assert new_list == ["Spammer"]

    @pytest.mark.asyncio
    async def test_remove_nonexistent_value_is_noop(self):
        from src.config import mutate_entity_config_list
        cfg = _blank_custom_cfg()
        cfg["userIncludeList"] = ["Alice"]
        CONFIG["threads"]["111"] = {"name": "t", "active": True, "config": cfg}
        ok, new_list = await mutate_entity_config_list("threads", "111", "userIncludeList", remove=["Nonexistent"])
        assert ok is True
        assert new_list == ["Alice"]


# ── Config persistence ────────────────────────────────────────────────────── #

class TestConfigPersistence:
    def test_save_and_reload(self, tmp_path):
        import src.config as cfg_mod
        CONFIG["threads"]["111"] = {"name": "t", "active": True, "config": {"siteName": "test"}}
        save_config(CONFIG)
        raw = json.loads(cfg_mod.STATE_FILE.read_text())
        assert raw["threads"]["111"]["config"]["siteName"] == "test"

    def test_load_missing_file_creates_default(self, tmp_path, monkeypatch):
        import src.config as cfg_mod
        missing = tmp_path / "nonexistent" / "config.json"
        monkeypatch.setattr(cfg_mod, "STATE_FILE", missing)
        loaded = load_config()
        assert "threads" in loaded
        assert "receivers" in loaded
        assert missing.exists()

    def test_load_old_config_gets_defaults(self, tmp_path, monkeypatch):
        """Old configs without 'receivers' key should get it added."""
        import src.config as cfg_mod
        old_config = {"version": "0.9", "threads": {"111": {"name": "t", "active": True, "config": {}}}}
        state_file = tmp_path / "old_config.json"
        state_file.write_text(json.dumps(old_config))
        monkeypatch.setattr(cfg_mod, "STATE_FILE", state_file)
        loaded = load_config()
        assert "receivers" in loaded
```

**Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_config.py -v`
Expected: Tests for `get_entity_entry`, `update_entity_config`, `mutate_entity_config_list` fail with ImportError (functions don't exist yet)

**Step 3: Implement unified entity operations in config.py**

Add these functions to `src/config.py` (after the existing helpers, around line 170):

```python
async def get_entity_entry(entity_type: str, entity_id: str) -> Optional[dict]:
    """Get config entry for a thread or receiver by entity type and ID."""
    async with CONFIG_LOCK:
        return CONFIG.get(entity_type, {}).get(entity_id)


async def update_entity_config(entity_type: str, entity_id: str, new_cfg: dict) -> bool:
    """Replace an entity's config dict. Works for both threads and receivers."""
    async with CONFIG_LOCK:
        entity = CONFIG.get(entity_type, {}).get(entity_id)
        if not entity:
            logger.warning(f"Attempted to update config for unknown {entity_type} entry {entity_id}")
            return False
        entity["config"] = new_cfg
        save_config(CONFIG)
        logger.info(f"Updated config for {entity_type} entry {entity_id}")
        return True


async def mutate_entity_config_list(
    entity_type: str, entity_id: str, field: str,
    *, add: Optional[List[str]] = None,
    remove: Optional[List[str]] = None,
    clear: bool = False
) -> Tuple[bool, Optional[List[str]]]:
    """Mutate a list field in any entity's config. Works for both threads and receivers."""
    async with CONFIG_LOCK:
        entity = CONFIG.get(entity_type, {}).get(entity_id)
        if not entity:
            logger.warning(f"Attempted to mutate config for unknown {entity_type} entry {entity_id}")
            return False, None
        cfg = entity["config"]
        if field not in cfg:
            cfg[field] = []
        if clear:
            logger.info(f"Clearing {field} for {entity_type} entry {entity_id}")
            cfg[field] = []
        else:
            lst = list(cfg[field])
            if add:
                logger.info(f"Adding {add} to {field} for {entity_type} entry {entity_id}")
                for it in add:
                    if it not in lst:
                        lst.append(it)
            if remove:
                logger.info(f"Removing {remove} from {field} for {entity_type} entry {entity_id}")
                lst = [it for it in lst if it not in remove]
            cfg[field] = lst
        save_config(CONFIG)
        return True, list(cfg[field])
```

Also update the existing `get_receiver_entry` to delegate:

```python
async def get_receiver_entry(key: str) -> Optional[dict]:
    """Get a receiver config entry by key."""
    return await get_entity_entry("receivers", key)
```

And update `update_receiver_config` and `update_custom_thread_config` to delegate:

```python
async def update_custom_thread_config(thread_id: str, new_cfg: dict) -> bool:
    return await update_entity_config("threads", thread_id, new_cfg)

async def update_receiver_config(key: str, new_cfg: dict) -> bool:
    return await update_entity_config("receivers", key, new_cfg)
```

And update `mutate_custom_thread_config_list` and `mutate_receiver_config_list` to delegate:

```python
async def mutate_custom_thread_config_list(thread_id: str, key: str, *, add=None, remove=None, clear=False):
    return await mutate_entity_config_list("threads", thread_id, key, add=add, remove=remove, clear=clear)

async def mutate_receiver_config_list(key: str, field: str, *, add=None, remove=None, clear=False):
    return await mutate_entity_config_list("receivers", key, field, add=add, remove=remove, clear=clear)
```

This preserves backward compatibility — existing callers keep working, but the logic is now in one place.

**Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_config.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/config.py tests/test_config.py
git commit -m "Unify config entity operations with comprehensive tests"
```

---

### Task 3: Write model/filter tests

**Files:**
- Create: `tests/test_models.py`

**Step 1: Write comprehensive filter tests**

Create `tests/test_models.py`:

```python
import pytest
from src.models import CustomFilter, _compile_pattern_list


# ── _compile_pattern_list ─────────────────────────────────────────────────── #

class TestCompilePatternList:
    def test_empty_list_returns_none(self):
        assert _compile_pattern_list([]) is None

    def test_single_pattern(self):
        rx = _compile_pattern_list(["foo"])
        assert rx is not None
        assert rx.search("foobar")

    def test_multiple_patterns_or_joined(self):
        rx = _compile_pattern_list(["foo", "bar"])
        assert rx.search("foo")
        assert rx.search("bar")
        assert not rx.search("baz")

    def test_case_insensitive(self):
        rx = _compile_pattern_list(["Foo"])
        assert rx.search("FOO")
        assert rx.search("foo")

    def test_string_input_coerced(self):
        rx = _compile_pattern_list("singlepattern")
        assert rx is not None
        assert rx.search("singlepattern")

    def test_empty_strings_filtered(self):
        rx = _compile_pattern_list(["", "foo", ""])
        assert rx is not None
        assert rx.search("foo")

    def test_all_empty_returns_none(self):
        assert _compile_pattern_list(["", ""]) is None

    def test_non_list_coerced(self):
        rx = _compile_pattern_list(42)
        assert rx is not None
        assert rx.search("42")


# ── CustomFilter.matches ──────────────────────────────────────────────────── #

def _make_change(user="TestUser", title="TestPage", comment="test edit",
                 server_name="en.wikipedia.org", change_type="edit", **extra):
    change = {
        "user": user,
        "title": title,
        "comment": comment,
        "server_name": server_name,
        "type": change_type,
    }
    change.update(extra)
    return change


class TestCustomFilterUserInclude:
    def test_user_in_include_matches(self):
        f = CustomFilter({"userIncludeList": ["Alice"]})
        assert f.matches(_make_change(user="Alice"))

    def test_user_not_in_include_no_match(self):
        f = CustomFilter({"userIncludeList": ["Alice"]})
        assert not f.matches(_make_change(user="Bob"))

    def test_user_include_case_sensitive(self):
        f = CustomFilter({"userIncludeList": ["Alice"]})
        assert not f.matches(_make_change(user="alice"))


class TestCustomFilterUserExclude:
    def test_user_in_exclude_blocked(self):
        f = CustomFilter({"userIncludeList": ["Alice"], "userExcludeList": ["Alice"]})
        assert not f.matches(_make_change(user="Alice"))

    def test_exclude_takes_precedence_over_include(self):
        f = CustomFilter({
            "userIncludeList": ["BadUser"],
            "userExcludeList": ["BadUser"],
        })
        assert not f.matches(_make_change(user="BadUser"))


class TestCustomFilterPagePatterns:
    def test_page_include_regex_match(self):
        f = CustomFilter({"pageIncludePatterns": ["^Talk:.*"]})
        assert f.matches(_make_change(title="Talk:Something"))

    def test_page_include_no_match(self):
        f = CustomFilter({"pageIncludePatterns": ["^Talk:.*"]})
        assert not f.matches(_make_change(title="Main Page"))

    def test_page_exclude_blocks_match(self):
        f = CustomFilter({
            "pageIncludePatterns": [".*"],
            "pageExcludePatterns": ["^User:.*"],
        })
        assert not f.matches(_make_change(title="User:Someone"))
        assert f.matches(_make_change(title="Article"))

    def test_page_patterns_case_insensitive(self):
        f = CustomFilter({"pageIncludePatterns": ["test"]})
        assert f.matches(_make_change(title="TEST PAGE"))


class TestCustomFilterSummaryPatterns:
    def test_summary_include_matches_comment(self):
        f = CustomFilter({"summaryIncludePatterns": ["vandal"]})
        assert f.matches(_make_change(comment="reverted vandalism"))

    def test_summary_include_matches_log_action_comment(self):
        f = CustomFilter({"summaryIncludePatterns": ["blocked"]})
        assert f.matches(_make_change(comment="", log_action_comment="blocked user"))

    def test_summary_exclude_blocks(self):
        f = CustomFilter({
            "summaryIncludePatterns": [".*"],
            "summaryExcludePatterns": ["bot edit"],
        })
        assert not f.matches(_make_change(comment="bot edit"))
        assert f.matches(_make_change(comment="human edit"))


class TestCustomFilterSiteName:
    def test_site_name_matches(self):
        f = CustomFilter({"siteName": "en.wikipedia.org", "userIncludeList": ["Alice"]})
        assert f.matches(_make_change(user="Alice", server_name="en.wikipedia.org"))

    def test_site_name_mismatch_blocks(self):
        f = CustomFilter({"siteName": "en.wikipedia.org", "userIncludeList": ["Alice"]})
        assert not f.matches(_make_change(user="Alice", server_name="de.wikipedia.org"))

    def test_site_name_case_insensitive(self):
        f = CustomFilter({"siteName": "EN.WIKIPEDIA.ORG", "userIncludeList": ["Alice"]})
        assert f.matches(_make_change(user="Alice", server_name="en.wikipedia.org"))

    def test_empty_site_name_matches_all(self):
        f = CustomFilter({"siteName": "", "userIncludeList": ["Alice"]})
        assert f.matches(_make_change(user="Alice", server_name="any.wiki.org"))


class TestCustomFilterCombined:
    def test_no_filters_no_match(self):
        f = CustomFilter({})
        assert not f.matches(_make_change())

    def test_empty_config_no_match(self):
        f = CustomFilter({
            "userIncludeList": [],
            "pageIncludePatterns": [],
            "summaryIncludePatterns": [],
        })
        assert not f.matches(_make_change())

    def test_multiple_include_types_any_matches(self):
        f = CustomFilter({
            "userIncludeList": ["Alice"],
            "pageIncludePatterns": ["^Talk:.*"],
        })
        assert f.matches(_make_change(user="Alice", title="Main Page"))
        assert f.matches(_make_change(user="Bob", title="Talk:Something"))
        assert not f.matches(_make_change(user="Bob", title="Main Page"))

    def test_exclude_checked_before_all_includes(self):
        f = CustomFilter({
            "userIncludeList": ["Spammer"],
            "userExcludeList": ["Spammer"],
            "pageIncludePatterns": [".*"],
        })
        # User is excluded even though page matches
        assert not f.matches(_make_change(user="Spammer", title="Anything"))

    def test_missing_title_with_site_name_returns_false(self):
        f = CustomFilter({"siteName": "en.wikipedia.org", "userIncludeList": ["Alice"]})
        change = _make_change(user="Alice", server_name="en.wikipedia.org")
        change["title"] = ""
        assert not f.matches(change)
```

**Step 2: Run tests to verify they pass**

These test existing code, so they should pass immediately:

Run: `venv/bin/python -m pytest tests/test_models.py -v`
Expected: All tests PASS

**Step 3: Commit**

```bash
git add tests/test_models.py
git commit -m "Add comprehensive filter and model tests"
```

---

### Task 4: Split commands.py into modules

**Files:**
- Create: `src/commands/__init__.py`
- Create: `src/commands/helpers.py`
- Create: `src/commands/thread_commands.py`
- Create: `src/commands/config_commands.py`
- Create: `src/commands/filter_commands.py`
- Create: `src/commands/receiver_commands.py`
- Create: `src/commands/help_commands.py`
- Delete: `src/commands.py`
- Modify: `src/main.py:11` (update import)
- Modify: `src/__init__.py` (update docstring)

This is a large task. The approach: create the new package with all commands split out, update the import in `main.py`, then delete the old `commands.py`. Each command function is moved as-is initially — deduplication happens in Task 5.

**Step 1: Create `src/commands/__init__.py`**

```python
"""
Discord command handlers, split by domain.

Importing this package registers all commands with the bot.
"""
from src.commands import (
    thread_commands,
    config_commands,
    filter_commands,
    receiver_commands,
    help_commands,
)
```

**Step 2: Create `src/commands/helpers.py`**

Move helper functions and shared imports from `commands.py`:

```python
import json
import io
from enum import Enum
from typing import Any, Optional

import discord
from discord.ext import commands

from src.logging_setup import logger
from src.bot_instance import bot
from src.constants import JSON_INDENT, UTF8_ENCODING
from src.config import (
    CONFIG,
    CONFIG_LOCK,
    DISCORD_CHANNEL_ID,
    OWNER_ID,
    save_config,
    ensure_custom_thread_entry,
    _normalise_list_val,
    mutate_entity_config_list,
)


def authorised(ctx) -> bool:
    """Gatekeeper: only allow the bot owner."""
    return ctx.author.id == OWNER_ID


# Alias kept for use with @commands.check
is_bot_owner = authorised


def _in_parent_channel(ctx: commands.Context) -> bool:
    """True when in the parent channel. Ready for #18: accepts set of IDs."""
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
    """Cheap JSON round-trip copy."""
    try:
        return json.loads(json.dumps(obj))
    except Exception:
        return obj


async def _require_custom_thread(ctx: commands.Context) -> Optional[dict]:
    if not _in_custom_thread(ctx):
        await ctx.reply("This command must be used inside a thread.")
        return None
    entry = await ensure_custom_thread_entry(str(ctx.channel.id), ctx.channel.name, create_if_missing=False)
    if entry is None:
        entry = await ensure_custom_thread_entry(str(ctx.channel.id), ctx.channel.name, create_if_missing=True)
    return entry


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
```

**Step 3: Create `src/commands/thread_commands.py`**

Move `/new thread`, `/new userthread`, `/activate`, `/deactivate`, and legacy aliases from `commands.py`:

```python
import discord
from discord.ext import commands

from src.logging_setup import logger
from src.bot_instance import bot
from src.config import (
    ensure_custom_thread_entry,
    mutate_custom_thread_config_list,
    set_custom_thread_active,
)
from src.discord_utils import discord_api_call_with_backoff
from src.commands.helpers import _in_parent_channel, _require_custom_thread


@bot.hybrid_group(name="new",
                  invoke_without_command=True,
                  description="Create new threads",
                  with_app_command=True)
async def new_group(ctx: commands.Context):
    """`/new` (no subcommand) -> show usage."""
    await ctx.reply(
        "**New Thread Commands:**\n"
        "* `/new thread <name>` - Create a custom filter thread\n"
        "* `/new userthread <Username>` - Create a User:<Username> thread and auto-add to userIncludeList"
    )


@new_group.command(name="userthread",
                   description="Create a per-user custom thread and include them")
async def new_userthread_cmd(ctx: commands.Context, *, user: str):
    logger.info(f"Userthread command from {ctx.author} for user '{user}' in {ctx.channel}")
    if not _in_parent_channel(ctx):
        await ctx.reply("Run `/new userthread` in the parent channel.")
        return
    try:
        thread = await discord_api_call_with_backoff(
            ctx.channel.create_thread,
            name=f"User:{user}",
            type=discord.ChannelType.public_thread
        )
        logger.info(f"Created thread {thread.name} ({thread.id}) for user {user}")
    except discord.HTTPException as e:
        logger.error(f"Failed to create thread for user {user}: {e}")
        await ctx.reply(f"Failed to create thread: {e}")
        return
    except Exception as e:
        logger.error(f"Unexpected error creating thread for user {user}: {e}")
        await ctx.reply(f"Failed to create thread: {e}")
        return
    await ensure_custom_thread_entry(str(thread.id), thread.name, create_if_missing=True)
    _, new_list = await mutate_custom_thread_config_list(
        str(thread.id), "userIncludeList", add=[user]
    )
    await ctx.reply(f"Tracking **{user}** in <#{thread.id}>.\n`userIncludeList`: `{new_list}`")


@bot.command(name="add", hidden=True)
async def add_legacy_cmd(ctx: commands.Context, *, user: str):
    """Legacy alias for /new userthread (text command only)."""
    await ctx.invoke(new_userthread_cmd, user=user)


@new_group.command(name="thread",
                   description="Create a custom-filter thread (parent channel only)")
async def new_thread_cmd(ctx: commands.Context, *, threadname: str = ""):
    logger.info(f"Thread command from {ctx.author} for thread '{threadname}' in {ctx.channel}")
    if not _in_parent_channel(ctx):
        await ctx.reply("Use `/new thread` in the parent channel.")
        return
    if not threadname.strip():
        await ctx.reply("Please provide a thread name: `/new thread <name>`.")
        return
    try:
        thread = await discord_api_call_with_backoff(
            ctx.channel.create_thread,
            name=threadname,
            type=discord.ChannelType.public_thread
        )
        logger.info(f"Created custom thread {thread.name} ({thread.id})")
    except discord.HTTPException as e:
        logger.error(f"Failed to create custom thread '{threadname}': {e}")
        await ctx.reply(f"Failed to create thread: {e}")
        return
    except Exception as e:
        logger.error(f"Unexpected error creating custom thread '{threadname}': {e}")
        await ctx.reply(f"Failed to create thread: {e}")
        return
    await ensure_custom_thread_entry(str(thread.id), thread.name, create_if_missing=True)
    await ctx.reply(f"Thread created: <#{thread.id}> (active). Configure with `/config` inside the thread.")


@bot.command(name="addcustom", hidden=True)
async def addcustom_legacy_cmd(ctx: commands.Context, *, threadname: str = ""):
    """Legacy alias for /new thread (text command only)."""
    await ctx.invoke(new_thread_cmd, threadname=threadname)


@bot.hybrid_command(name="activate",
                    description="Activate current custom thread",
                    with_app_command=True)
async def activate_custom_thread_cmd(ctx: commands.Context):
    logger.info(f"Activate command from {ctx.author} in thread {ctx.channel.id}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    ok = await set_custom_thread_active(str(ctx.channel.id), True)
    if ok:
        await ctx.reply("Activated.")
    else:
        await ctx.reply("Failed to activate (missing config?).")


@bot.hybrid_command(name="deactivate",
                    description="Deactivate current custom thread",
                    with_app_command=True)
async def deactivate_custom_thread_cmd(ctx: commands.Context):
    logger.info(f"Deactivate command from {ctx.author} in thread {ctx.channel.id}")
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    ok = await set_custom_thread_active(str(ctx.channel.id), False)
    if ok:
        await ctx.reply("Deactivated.")
    else:
        await ctx.reply("Failed to deactivate (missing config?).")
```

**Step 4: Create `src/commands/config_commands.py`**

Move `/config` group, `/globalconfig` group, and `/status` from `commands.py`:

```python
import json
import io

import discord
from discord.ext import commands

from src.logging_setup import logger
from src.bot_instance import bot
from src.constants import JSON_INDENT, UTF8_ENCODING
from src.config import (
    CONFIG,
    CONFIG_LOCK,
    save_config,
    update_custom_thread_config,
    mutate_custom_thread_config_list,
    _normalise_list_val,
)
from src.commands.helpers import (
    authorised,
    _require_custom_thread,
    _parse_json_arg,
    _deepcopy_cfg,
)


# ── /globalconfig ─────────────────────────────────────────────────────────── #

@bot.hybrid_group(name="globalconfig",
                  invoke_without_command=True,
                  description="Global configuration operations",
                  with_app_command=True)
@commands.check(authorised)
async def globalconfig_group(ctx: commands.Context):
    await ctx.invoke(globalconfig_getraw_cmd)


@globalconfig_group.command(name="getraw",
                            description="Download full configuration as JSON")
@commands.check(authorised)
async def globalconfig_getraw_cmd(ctx: commands.Context):
    logger.info(f"Global config getraw command from {ctx.author}")
    async with CONFIG_LOCK:
        data = json.dumps(CONFIG, indent=JSON_INDENT).encode(UTF8_ENCODING)
    file = discord.File(io.BytesIO(data), filename="global_config.json")
    await ctx.reply(file=file, mention_author=False)


@globalconfig_group.command(name="setraw",
                            description="Replace configuration from attached JSON (DANGEROUS)")
@commands.check(authorised)
async def globalconfig_setraw_cmd(ctx: commands.Context, attachment: discord.Attachment = None):
    logger.warning(f"Global config setraw command from {ctx.author} - DANGEROUS OPERATION")
    if attachment is None:
        if ctx.message is None or not ctx.message.attachments:
            await ctx.reply("Attach a **JSON** file to `/globalconfig setraw`.", mention_author=False)
            return
        attachment = ctx.message.attachments[0]
    try:
        raw = await attachment.read()
        new_cfg = json.loads(raw)
    except Exception as e:
        await ctx.reply(f"Could not parse attachment as JSON: {e}", mention_author=False)
        return
    async with CONFIG_LOCK:
        CONFIG.clear()
        CONFIG.update(new_cfg)
        save_config(CONFIG)
    await ctx.reply("Global configuration **replaced**.", mention_author=False)


# ── /config ───────────────────────────────────────────────────────────────── #

@bot.hybrid_group(name="config",
                  invoke_without_command=True,
                  description="Thread configuration operations",
                  with_app_command=True)
async def config_group(ctx: commands.Context):
    await ctx.invoke(config_get_cmd)


@config_group.command(name="get",
                      description="Show current thread configuration")
async def config_get_cmd(ctx: commands.Context):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    pretty = json.dumps(entry["config"], indent=JSON_INDENT)
    await ctx.reply(f"```json\n{pretty}\n```")


@config_group.command(name="set",
                      description="Set a configuration key")
async def config_set_cmd(ctx: commands.Context, key: str, *, value: str):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    parsed = _parse_json_arg(value)
    cfg = _deepcopy_cfg(entry["config"])
    cfg[key] = parsed
    ok = await update_custom_thread_config(str(ctx.channel.id), cfg)
    if ok:
        await ctx.reply(f"Set `{key}`: `{parsed}`")
    else:
        await ctx.reply("Failed to set.")


@config_group.command(name="add",
                      description="Append value(s) to a list key")
async def config_add_cmd(ctx: commands.Context, key: str, *, value: str):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(_parse_json_arg(value))
    ok, new_list = await mutate_custom_thread_config_list(str(ctx.channel.id), key, add=vals)
    if ok:
        await ctx.reply(f"Added to `{key}`: `{vals}`\nNow: `{new_list}`")
    else:
        await ctx.reply("Failed to add.")


@config_group.command(name="remove",
                      description="Remove value(s) from a list key")
async def config_remove_cmd(ctx: commands.Context, key: str, *, value: str):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    vals = _normalise_list_val(_parse_json_arg(value))
    ok, new_list = await mutate_custom_thread_config_list(str(ctx.channel.id), key, remove=vals)
    if ok:
        await ctx.reply(f"Removed from `{key}`: `{vals}`\nNow: `{new_list}`")
    else:
        await ctx.reply("Failed to remove.")


@config_group.command(name="clear",
                      description="Clear a list key")
async def config_clear_cmd(ctx: commands.Context, key: str):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    ok, _ = await mutate_custom_thread_config_list(str(ctx.channel.id), key, clear=True)
    if ok:
        await ctx.reply(f"Cleared `{key}`.")
    else:
        await ctx.reply("Failed to clear.")


@config_group.command(name="getraw",
                      description="Download raw JSON configuration for this thread")
@commands.check(authorised)
async def config_getraw_cmd(ctx: commands.Context):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    data = json.dumps(entry["config"], indent=JSON_INDENT).encode(UTF8_ENCODING)
    file = discord.File(io.BytesIO(data),
                        filename=f"thread_{ctx.channel.id}_config.json")
    await ctx.reply(file=file, mention_author=False)


@config_group.command(name="setraw",
                      description="Replace this thread's configuration from attached JSON (DANGEROUS)")
@commands.check(authorised)
async def config_setraw_cmd(ctx: commands.Context, attachment: discord.Attachment = None):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    if attachment is None:
        if ctx.message is None or not ctx.message.attachments:
            await ctx.reply("Attach a **JSON** file to `/config setraw`.", mention_author=False)
            return
        attachment = ctx.message.attachments[0]
    try:
        raw = await attachment.read()
        new_cfg = json.loads(raw)
    except Exception as e:
        await ctx.reply(f"Could not parse attachment as JSON: {e}", mention_author=False)
        return
    ok = await update_custom_thread_config(str(ctx.channel.id), new_cfg)
    if ok:
        await ctx.reply("Thread configuration **replaced**.", mention_author=False)
    else:
        await ctx.reply("Failed to replace configuration.", mention_author=False)


# ── /status (alias for /config get) ──────────────────────────────────────── #

@bot.hybrid_command(name="status",
                    description="Show current thread configuration",
                    with_app_command=True)
async def status_cmd(ctx: commands.Context):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    pretty = json.dumps(entry["config"], indent=JSON_INDENT)
    await ctx.reply(f"```json\n{pretty}\n```")
```

**Step 5: Create `src/commands/filter_commands.py`**

The deduplicated thread filter shortcuts:

```python
import discord
from discord.ext import commands

from src.bot_instance import bot
from src.commands.helpers import (
    FilterType,
    _require_custom_thread,
    mutate_filter,
)


@bot.hybrid_command(name="track",
                    description="Add to include filters (shortcut for /config add)",
                    with_app_command=True)
@discord.app_commands.describe(target="What to track (page, user, summary)", value="Pattern or username")
async def track_cmd(ctx: commands.Context, target: FilterType, *, value: str):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    await mutate_filter(ctx, "threads", str(ctx.channel.id), target, value, action="add_include")


@bot.hybrid_command(name="ignore",
                    description="Add to exclude filters (shortcut for /config add)",
                    with_app_command=True)
@discord.app_commands.describe(target="What to ignore (page, user, summary)", value="Pattern or username")
async def ignore_cmd(ctx: commands.Context, target: FilterType, *, value: str):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    await mutate_filter(ctx, "threads", str(ctx.channel.id), target, value, action="add_exclude")


@bot.hybrid_command(name="untrack",
                    description="Remove from include filters (shortcut for /config remove)",
                    with_app_command=True)
@discord.app_commands.describe(target="What to remove (page, user, summary)", value="Pattern or username")
async def untrack_cmd(ctx: commands.Context, target: FilterType, *, value: str):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    await mutate_filter(ctx, "threads", str(ctx.channel.id), target, value, action="remove_include")


@bot.hybrid_command(name="unignore",
                    description="Remove from exclude filters (shortcut for /config remove)",
                    with_app_command=True)
@discord.app_commands.describe(target="What to remove (page, user, summary)", value="Pattern or username")
async def unignore_cmd(ctx: commands.Context, target: FilterType, *, value: str):
    entry = await _require_custom_thread(ctx)
    if not entry:
        return
    await mutate_filter(ctx, "threads", str(ctx.channel.id), target, value, action="remove_exclude")
```

**Step 6: Create `src/commands/receiver_commands.py`**

Receiver commands, with filter operations now using `mutate_filter`:

```python
import json

import discord
from discord.ext import commands

from src.logging_setup import logger
from src.bot_instance import bot
from src.constants import JSON_INDENT
from src.config import (
    CONFIG,
    CONFIG_LOCK,
    WEBHOOKS,
    create_receiver,
    delete_receiver,
    set_receiver_active,
    get_receiver_entry,
    update_receiver_config,
)
from src.commands.helpers import (
    is_bot_owner,
    FilterType,
    _parse_json_arg,
    _deepcopy_cfg,
    mutate_filter,
)


@bot.hybrid_group(name="receiver",
                  invoke_without_command=True,
                  description="Manage webhook receivers (owner only)",
                  with_app_command=True)
@commands.check(is_bot_owner)
async def receiver_group(ctx: commands.Context):
    await ctx.reply(
        "**Receiver Commands (owner only):**\n"
        "* `/receiver list` - List all receivers\n"
        "* `/receiver add <key> <name>` - Create a new receiver\n"
        "* `/receiver remove <key>` - Delete a receiver\n"
        "* `/receiver activate <key>` / `/receiver deactivate <key>` - Toggle receiver\n"
        "* `/receiver config <key>` - Show receiver config\n"
        "* `/receiver track|ignore|untrack|unignore <key> page|user|summary <value>` - Manage filters"
    )


@receiver_group.command(name="list", description="List all receivers")
@commands.check(is_bot_owner)
async def receiver_list_cmd(ctx: commands.Context):
    async with CONFIG_LOCK:
        receivers = CONFIG.get("receivers", {})
    if not receivers:
        await ctx.reply("No receivers configured.")
        return
    lines = ["**Configured Receivers:**"]
    for key, entry in receivers.items():
        status_parts = []
        status_parts.append("active" if entry.get("active") else "inactive")
        if entry.get("errored"):
            status_parts.append("ERRORED")
        has_webhook = "webhook" if key in WEBHOOKS else "NO WEBHOOK"
        status = ", ".join(status_parts)
        lines.append(f"* `{key}`: {entry.get('name', '(unnamed)')} [{status}, {has_webhook}]")
    await ctx.reply("\n".join(lines))


@receiver_group.command(name="add", description="Create a new receiver")
@commands.check(is_bot_owner)
async def receiver_add_cmd(ctx: commands.Context, key: str, *, name: str):
    ok, msg = await create_receiver(key, name)
    await ctx.reply(msg)


@receiver_group.command(name="remove", description="Delete a receiver")
@commands.check(is_bot_owner)
async def receiver_remove_cmd(ctx: commands.Context, key: str):
    ok, msg = await delete_receiver(key)
    await ctx.reply(msg)


@receiver_group.command(name="activate", description="Activate a receiver")
@commands.check(is_bot_owner)
async def receiver_activate_cmd(ctx: commands.Context, key: str):
    ok, msg = await set_receiver_active(key, True)
    await ctx.reply(msg)


@receiver_group.command(name="deactivate", description="Deactivate a receiver")
@commands.check(is_bot_owner)
async def receiver_deactivate_cmd(ctx: commands.Context, key: str):
    ok, msg = await set_receiver_active(key, False)
    await ctx.reply(msg)


@receiver_group.command(name="config", description="Show receiver configuration")
@commands.check(is_bot_owner)
async def receiver_config_cmd(ctx: commands.Context, key: str):
    entry = await get_receiver_entry(key)
    if not entry:
        await ctx.reply(f"Receiver `{key}` not found.")
        return
    pretty = json.dumps(entry, indent=JSON_INDENT)
    await ctx.reply(f"**Receiver `{key}`:**\n```json\n{pretty}\n```")


@receiver_group.command(name="setconfig", description="Set a configuration key for a receiver")
@commands.check(is_bot_owner)
async def receiver_setconfig_cmd(ctx: commands.Context, key: str, config_key: str, *, value: str):
    entry = await get_receiver_entry(key)
    if not entry:
        await ctx.reply(f"Receiver `{key}` not found.")
        return
    parsed = _parse_json_arg(value)
    cfg = _deepcopy_cfg(entry["config"])
    cfg[config_key] = parsed
    ok = await update_receiver_config(key, cfg)
    if ok:
        await ctx.reply(f"Set `{config_key}` for receiver `{key}`: `{parsed}`")
    else:
        await ctx.reply("Failed to set configuration.")


@receiver_group.command(name="track", description="Add to receiver include filters")
@commands.check(is_bot_owner)
@discord.app_commands.describe(key="Receiver key", target="What to track (page, user, summary)", value="Pattern or username")
async def receiver_track_cmd(ctx: commands.Context, key: str, target: FilterType, *, value: str):
    await mutate_filter(ctx, "receivers", key, target, value, action="add_include", entity_label=f"Receiver `{key}`")


@receiver_group.command(name="ignore", description="Add to receiver exclude filters")
@commands.check(is_bot_owner)
@discord.app_commands.describe(key="Receiver key", target="What to ignore (page, user, summary)", value="Pattern or username")
async def receiver_ignore_cmd(ctx: commands.Context, key: str, target: FilterType, *, value: str):
    await mutate_filter(ctx, "receivers", key, target, value, action="add_exclude", entity_label=f"Receiver `{key}`")


@receiver_group.command(name="untrack", description="Remove from receiver include filters")
@commands.check(is_bot_owner)
@discord.app_commands.describe(key="Receiver key", target="What to remove (page, user, summary)", value="Pattern or username")
async def receiver_untrack_cmd(ctx: commands.Context, key: str, target: FilterType, *, value: str):
    await mutate_filter(ctx, "receivers", key, target, value, action="remove_include", entity_label=f"Receiver `{key}`")


@receiver_group.command(name="unignore", description="Remove from receiver exclude filters")
@commands.check(is_bot_owner)
@discord.app_commands.describe(key="Receiver key", target="What to remove (page, user, summary)", value="Pattern or username")
async def receiver_unignore_cmd(ctx: commands.Context, key: str, target: FilterType, *, value: str):
    await mutate_filter(ctx, "receivers", key, target, value, action="remove_exclude", entity_label=f"Receiver `{key}`")
```

**Step 7: Create placeholder `src/commands/help_commands.py`**

Placeholder that preserves current behavior — auto-generation comes in Task 6:

```python
from discord.ext import commands

from src.logging_setup import logger
from src.bot_instance import bot


@bot.hybrid_command(name="fezhelp",
                    description="Show available commands",
                    with_app_command=True)
async def fezhelp_cmd(ctx: commands.Context):
    """Display available commands. Auto-generated in Task 6."""
    logger.info(f"Help command from {ctx.author} in {ctx.channel}")

    chunks = [
        """**Available Commands:**

**Basic Commands:**
* `/fezhelp` - Show this help message
* `/status` - Show current thread configuration

**Thread Management (run in parent channel):**
* `/new thread <name>` - Create a new tracking thread
* `/new userthread <Username>` - Create a "User:Username" thread and auto-add to userIncludeList
* `/activate` - Activate current thread
* `/deactivate` - Deactivate current thread""",

        """**Tracking Shortcuts (run inside a thread):**
* `/track page|user|summary <value>` - Add to include filters
* `/ignore page|user|summary <value>` - Add to exclude filters
* `/untrack page|user|summary <value>` - Remove from include filters
* `/unignore page|user|summary <value>` - Remove from exclude filters

**Global Configuration:**
* `/globalconfig getraw` - Download full configuration as JSON
* `/globalconfig setraw [attachment]` - Replace configuration from attached JSON file (DANGEROUS)""",

        """**Advanced Thread Configuration:**
* `/config` - Show current thread configuration (same as `/status`)
* `/config set <key> <json>` - Set configuration value
* `/config add <key> <value>` - Add to list configuration
* `/config remove <key> <value>` - Remove from list configuration
* `/config clear <key>` - Clear list configuration
* `/config getraw` - Download raw JSON for current thread
* `/config setraw [attachment]` - Replace current-thread configuration from attached JSON (DANGEROUS)""",

        """**Receiver Commands (owner only):**
* `/receiver list` - List all receivers
* `/receiver add <key> <name>` - Create a new receiver
* `/receiver remove <key>` - Delete a receiver
* `/receiver activate <key>` / `/receiver deactivate <key>` - Toggle receiver
* `/receiver config <key>` - Show receiver configuration
* `/receiver track|ignore|untrack|unignore <key> page|user|summary <value>` - Manage filters"""
    ]

    for i, chunk in enumerate(chunks):
        if i == 0:
            await ctx.reply(chunk)
        else:
            await ctx.send(chunk)
```

**Step 8: Update `src/main.py` import**

Change line 11 from:
```python
import src.commands
```
To:
```python
import src.commands  # noqa: F401 — triggers command registration
```

(The import stays the same — `src/commands/__init__.py` handles the submodule imports.)

**Step 9: Delete old `src/commands.py`**

```bash
rm src/commands.py
```

**Step 10: Update `src/__init__.py` docstring**

Update the docstring to reflect the package change (commands is now a package, not a module).

**Step 11: Run all existing tests**

Run: `venv/bin/python -m pytest tests/ -v`
Expected: All tests PASS (config and model tests should still pass; commands haven't changed behavior)

**Step 12: Commit**

```bash
git add src/commands/ src/main.py src/__init__.py tests/
git rm src/commands.py
git commit -m "Split commands.py into focused modules with filter deduplication

Closes the 8-function duplication in track/ignore commands via shared
mutate_filter() helper. Commands split by domain: thread, config, filter,
receiver, help."
```

---

### Task 5: Write command helper tests

**Files:**
- Create: `tests/test_command_helpers.py`

**Step 1: Write tests for helpers**

```python
import pytest
from src.commands.helpers import (
    _parse_json_arg,
    _deepcopy_cfg,
    FilterType,
    _INCLUDE_KEY_MAP,
    _EXCLUDE_KEY_MAP,
    _ACTION_MAP,
)


class TestParseJsonArg:
    def test_valid_json_string(self):
        assert _parse_json_arg('"hello"') == "hello"

    def test_valid_json_list(self):
        assert _parse_json_arg('["a", "b"]') == ["a", "b"]

    def test_valid_json_bool(self):
        assert _parse_json_arg("true") is True

    def test_valid_json_number(self):
        assert _parse_json_arg("42") == 42

    def test_invalid_json_returns_raw_string(self):
        assert _parse_json_arg("not json") == "not json"

    def test_empty_string(self):
        assert _parse_json_arg("") == ""


class TestDeepcopyConfig:
    def test_deep_copies_dict(self):
        original = {"a": [1, 2, 3]}
        copy = _deepcopy_cfg(original)
        copy["a"].append(4)
        assert original["a"] == [1, 2, 3]

    def test_non_serializable_returns_original(self):
        obj = object()
        result = _deepcopy_cfg(obj)
        assert result is obj


class TestFilterTypeMaps:
    def test_all_filter_types_have_include_key(self):
        for ft in FilterType:
            assert ft in _INCLUDE_KEY_MAP

    def test_all_filter_types_have_exclude_key(self):
        for ft in FilterType:
            assert ft in _EXCLUDE_KEY_MAP

    def test_action_map_has_all_four_actions(self):
        assert set(_ACTION_MAP.keys()) == {"add_include", "remove_include", "add_exclude", "remove_exclude"}
```

**Step 2: Run tests**

Run: `venv/bin/python -m pytest tests/test_command_helpers.py -v`
Expected: All tests PASS

**Step 3: Commit**

```bash
git add tests/test_command_helpers.py
git commit -m "Add command helper tests"
```

---

### Task 6: Auto-generate help text

**Files:**
- Modify: `src/commands/help_commands.py`
- Create: `tests/test_help.py`

**Step 1: Write failing test**

Create `tests/test_help.py`:

```python
import pytest
from src.constants import DISCORD_MESSAGE_LIMIT


class TestBuildHelpSections:
    def test_all_chunks_under_discord_limit(self):
        from src.commands.help_commands import _build_help_sections
        sections = _build_help_sections()
        assert len(sections) > 0
        for i, chunk in enumerate(sections):
            assert len(chunk) <= DISCORD_MESSAGE_LIMIT, (
                f"Help chunk {i} is {len(chunk)} chars, exceeds {DISCORD_MESSAGE_LIMIT}"
            )

    def test_includes_core_commands(self):
        from src.commands.help_commands import _build_help_sections
        full_text = "\n".join(_build_help_sections())
        for cmd in ["fezhelp", "track", "config", "receiver", "new thread"]:
            assert cmd in full_text.lower(), f"Expected '{cmd}' in help text"

    def test_excludes_hidden_commands(self):
        from src.commands.help_commands import _build_help_sections
        full_text = "\n".join(_build_help_sections())
        assert "addcustom" not in full_text.lower()
        assert "!add" not in full_text
```

**Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_help.py -v`
Expected: FAIL (no `_build_help_sections` function)

**Step 3: Implement auto-generated help**

Replace `src/commands/help_commands.py` with:

```python
from discord.ext import commands

from src.logging_setup import logger
from src.bot_instance import bot
from src.constants import DISCORD_MESSAGE_LIMIT


def _build_help_sections() -> list:
    """
    Build help text from registered bot commands.
    Returns list of strings, each under DISCORD_MESSAGE_LIMIT chars.
    """
    # Gather commands grouped by their parent group (or "Basic" for top-level)
    groups: dict[str, list[str]] = {}

    for cmd in sorted(bot.commands, key=lambda c: c.qualified_name):
        if cmd.hidden:
            continue

        if isinstance(cmd, commands.Group):
            # Add the group's subcommands
            group_name = cmd.qualified_name
            for subcmd in sorted(cmd.commands, key=lambda c: c.qualified_name):
                if subcmd.hidden:
                    continue
                desc = subcmd.description or subcmd.short_doc or ""
                line = f"* `/{subcmd.qualified_name}` - {desc}"
                groups.setdefault(group_name, []).append(line)
        else:
            desc = cmd.description or cmd.short_doc or ""
            line = f"* `/{cmd.qualified_name}` - {desc}"
            groups.setdefault("Basic Commands", []).append(line)

    # Build sections
    sections = []
    for group_name, lines in groups.items():
        header = f"**{group_name.title() if group_name != 'Basic Commands' else group_name}:**"
        sections.append(header + "\n" + "\n".join(lines))

    # Chunk into messages under Discord limit
    chunks = []
    current = "**Available Commands:**\n\n"
    for section in sections:
        candidate = current + section + "\n\n"
        if len(candidate) > DISCORD_MESSAGE_LIMIT:
            if current.strip():
                chunks.append(current.strip())
            current = section + "\n\n"
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())

    return chunks


@bot.hybrid_command(name="fezhelp",
                    description="Show available commands",
                    with_app_command=True)
async def fezhelp_cmd(ctx: commands.Context):
    """Display available commands and their descriptions."""
    logger.info(f"Help command from {ctx.author} in {ctx.channel}")
    chunks = _build_help_sections()
    for i, chunk in enumerate(chunks):
        if i == 0:
            await ctx.reply(chunk)
        else:
            await ctx.send(chunk)
```

**Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_help.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/commands/help_commands.py tests/test_help.py
git commit -m "Auto-generate help text from registered commands

Closes #23 — help text is now built dynamically from command metadata
and chunked to stay under Discord's 2000 char limit."
```

---

### Task 7: Clean up and verify

**Files:**
- Possibly modify: various files for dead code removal

**Step 1: Run full test suite**

Run: `venv/bin/python -m pytest tests/ -v`
Expected: All tests PASS

**Step 2: Verify bot imports cleanly**

Run: `FEZ_COLLECTOR_DISCORD_TOKEN=test FEZ_COLLECTOR_CHANNEL_ID=1 venv/bin/python -c "import src.commands; print('OK')"`
Expected: `OK`

**Step 3: Check for any remaining references to old commands.py**

Search for imports of the old flat `src.commands` that might need updating.

**Step 4: Remove `authorised` / `is_bot_owner` duplication**

In the original `commands.py` there were two identical functions (`authorised` and `is_bot_owner`). In `helpers.py` we unified them — verify no other code references the old ones.

**Step 5: Run full test suite one final time**

Run: `venv/bin/python -m pytest tests/ -v`
Expected: All tests PASS

**Step 6: Commit any cleanup**

```bash
git add -A
git commit -m "Clean up dead code and verify full test suite passes"
```
