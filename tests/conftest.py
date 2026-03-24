import os

# Set dummy env vars BEFORE any src imports so config.py doesn't get empty values
os.environ.setdefault("FEZ_COLLECTOR_DISCORD_TOKEN", "test-token")
os.environ.setdefault("FEZ_COLLECTOR_CHANNEL_ID", "123456789")
os.environ.setdefault("FEZ_COLLECTOR_CHANNEL_IDS", "123456789")
os.environ.setdefault("FEZ_OWNER_ID", "987654321")
