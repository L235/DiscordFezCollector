import os
from pathlib import Path
from uuid import uuid4

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
