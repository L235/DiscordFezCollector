"""Tests for load_config, focusing on backfill of DEFAULT_CUSTOM_CONFIG keys."""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("FEZ_COLLECTOR_DISCORD_TOKEN", "test-token")
os.environ.setdefault("FEZ_COLLECTOR_CHANNEL_ID", "123456")

sys.path.append(str(Path(__file__).parent.parent))

from src.config import load_config, save_config, STATE_FILE, DEFAULT_CUSTOM_CONFIG
from src.constants import CONFIG_VERSION


def _write_state(data: dict):
    """Write a config dict to STATE_FILE for load_config to read."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w") as fp:
        json.dump(data, fp)


def _read_state() -> dict:
    with STATE_FILE.open() as fp:
        return json.load(fp)


# ── Backfill tests ────────────────────────────────────────────────────────── #


def test_backfill_adds_missing_keys_to_thread():
    """Pre-existing thread configs missing new keys get them backfilled."""
    _write_state({
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


def test_backfill_adds_missing_keys_to_receiver():
    """Receiver configs also get backfilled."""
    _write_state({
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


def test_backfill_does_not_share_list_objects():
    """Each entry must get its own list instances, not shared references."""
    _write_state({
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


def test_backfill_preserves_existing_values():
    """Keys already present in the config must not be overwritten."""
    _write_state({
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


def test_load_creates_default_config_when_missing(tmp_path, monkeypatch):
    """When STATE_FILE doesn't exist, load_config creates it with defaults."""
    fake_state = tmp_path / "state" / "config.json"
    monkeypatch.setattr("src.config.STATE_FILE", fake_state)
    cfg = load_config()
    assert cfg["version"] == CONFIG_VERSION
    assert cfg["threads"] == {}
    assert cfg["receivers"] == {}
    assert fake_state.exists()
