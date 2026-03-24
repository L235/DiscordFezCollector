"""Tests for load_config, focusing on backfill of DEFAULT_CUSTOM_CONFIG keys."""
import json
from pathlib import Path

import pytest

from src.config import load_config, DEFAULT_CUSTOM_CONFIG
from src.constants import CONFIG_VERSION


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Redirect STATE_FILE to a temp directory for every test."""
    fake_state = tmp_path / "state" / "config.json"
    monkeypatch.setattr("src.config.STATE_FILE", fake_state)
    return fake_state


def _write_state(state_file: Path, data: dict):
    """Write a config dict to the given state file for load_config to read."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with state_file.open("w") as fp:
        json.dump(data, fp)


# ── Backfill tests ────────────────────────────────────────────────────────── #


def test_backfill_adds_missing_keys_to_thread(isolated_state):
    """Pre-existing thread configs missing new keys get them backfilled."""
    _write_state(isolated_state, {
        "version": CONFIG_VERSION,
        "threads": {
            "111": {
                "name": "test",
                "active": True,
                "config": {"siteName": "en.wikipedia.org", "userIncludeList": ["Alice"]},
            }
        },
        "receivers": {},
    })
    cfg = load_config()
    thread_cfg = cfg["threads"]["111"]["config"]
    for key in DEFAULT_CUSTOM_CONFIG:
        assert key in thread_cfg, f"Missing backfilled key: {key}"
    # Explicitly set values must not be overwritten
    assert thread_cfg["siteName"] == "en.wikipedia.org"
    assert thread_cfg["userIncludeList"] == ["Alice"]
    # Backfilled defaults
    assert thread_cfg["linkStyle"] == "title"
    assert thread_cfg["pageIncludePatterns"] == []


def test_backfill_adds_missing_keys_to_receiver(isolated_state):
    """Receiver configs also get backfilled."""
    _write_state(isolated_state, {
        "version": CONFIG_VERSION,
        "threads": {},
        "receivers": {
            "my_hook": {
                "name": "hook",
                "active": False,
                "errored": False,
                "config": {"userIncludeList": ["Bot"]},
            }
        },
    })
    cfg = load_config()
    rcv_cfg = cfg["receivers"]["my_hook"]["config"]
    assert rcv_cfg["linkStyle"] == "title"
    assert rcv_cfg["userIncludeList"] == ["Bot"]


def test_backfill_does_not_share_list_objects(isolated_state):
    """Each entry must get its own list instances, not shared references."""
    _write_state(isolated_state, {
        "version": CONFIG_VERSION,
        "threads": {
            "1": {"name": "a", "active": True, "config": {"siteName": ""}},
            "2": {"name": "b", "active": True, "config": {"siteName": ""}},
        },
        "receivers": {},
    })
    cfg = load_config()
    list_a = cfg["threads"]["1"]["config"]["pageIncludePatterns"]
    list_b = cfg["threads"]["2"]["config"]["pageIncludePatterns"]
    assert list_a is not list_b, "Backfilled lists must not be the same object"
    # Mutating one must not affect the other
    list_a.append("test")
    assert "test" not in list_b


def test_backfill_preserves_existing_values(isolated_state):
    """Keys already present in the config must not be overwritten."""
    _write_state(isolated_state, {
        "version": CONFIG_VERSION,
        "threads": {
            "1": {
                "name": "t",
                "active": True,
                "config": {
                    "siteName": "meta.wikimedia.org",
                    "linkStyle": "action",
                    "pageIncludePatterns": ["Main_Page"],
                    "pageExcludePatterns": [],
                    "userExcludeList": [],
                    "userIncludeList": [],
                    "summaryIncludePatterns": [],
                    "summaryExcludePatterns": [],
                },
            }
        },
        "receivers": {},
    })
    cfg = load_config()
    thread_cfg = cfg["threads"]["1"]["config"]
    assert thread_cfg["linkStyle"] == "action"
    assert thread_cfg["pageIncludePatterns"] == ["Main_Page"]


def test_load_creates_default_config_when_missing(isolated_state):
    """When STATE_FILE doesn't exist, load_config creates it with defaults."""
    cfg = load_config()
    assert cfg["version"] == CONFIG_VERSION
    assert cfg["threads"] == {}
    assert cfg["receivers"] == {}
    assert isolated_state.exists()


def test_load_default_returns_independent_dicts(isolated_state):
    """The default config path must not share dicts with DEFAULT_CONFIG."""
    cfg = load_config()
    cfg["threads"]["999"] = {"name": "injected"}
    # Load again — should be clean
    cfg2 = load_config()
    # The file now exists (written by first call), so this tests the file path.
    # But the module-level DEFAULT_CONFIG must not have been mutated.
    from src.config import DEFAULT_CONFIG
    assert "999" not in DEFAULT_CONFIG.get("threads", {})
