import os
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

# Importing config initializes persistence. Tests must never use real state or
# inherit production Redis credentials from the caller's environment.
scratch = Path(__file__).resolve().parents[1] / ".Codex" / "tmp"
scratch.mkdir(parents=True, exist_ok=True)
os.environ["FEZ_CONFIG_BACKEND"] = "file"
os.environ["FEZ_COLLECTOR_STATE"] = str(scratch / f"pytest-config-{uuid4().hex}.json")

# Set dummy env vars BEFORE any src imports so config.py doesn't get empty values
os.environ.setdefault("FEZ_COLLECTOR_DISCORD_TOKEN", "test-token")
os.environ.setdefault("FEZ_COLLECTOR_CHANNEL_ID", "123456789")
os.environ.setdefault("FEZ_OWNER_ID", "987654321")


# EventStreams constructs a Site at import time. These tests exercise routing,
# not Wikimedia authentication, and must not contact a live wiki during collection.
os.environ["PYWIKIBOT_NO_USER_CONFIG"] = "2"
os.environ["PYWIKIBOT_DIR"] = str(scratch / "pywikibot-tests")
Path(os.environ["PYWIKIBOT_DIR"]).mkdir(exist_ok=True)
_wiki_login = patch("pywikibot.site.APISite.login")


def pytest_configure():
    _wiki_login.start()


def pytest_unconfigure():
    _wiki_login.stop()
