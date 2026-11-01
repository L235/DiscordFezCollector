"""Small, bounded Upstash REST adapter for one persistent configuration document."""
import json
import os
from urllib.parse import urlsplit

import requests


class ConfigPersistenceError(RuntimeError):
    """Safe to show to an operator; never includes credentials or config values."""


def validate_config(value):
    if not isinstance(value, dict) or not isinstance(value.get("threads"), dict):
        raise ConfigPersistenceError("Invalid configuration: expected a threads object.")
    if not isinstance(value.get("receivers", {}), dict):
        raise ConfigPersistenceError("Invalid configuration: expected a receivers object.")
    for entries in (value["threads"], value.get("receivers", {})):
        for entry in entries.values():
            if not isinstance(entry, dict) or not isinstance(entry.get("config", {}), dict):
                raise ConfigPersistenceError("Invalid configuration entry.")
    return value


class UpstashConfigStore:
    def __init__(self, url, token, key, *, session=None):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ConfigPersistenceError("UPSTASH_REDIS_REST_URL must be an HTTPS endpoint.")
        if not token or not key:
            raise ConfigPersistenceError("Redis token and configuration key are required.")
        self.url = url.rstrip("/")
        self.key = key
        self.session = session or requests.Session()
        self.headers = {"Authorization": f"Bearer {token}"}

    def execute(self, command):
        try:
            response = self.session.post(self.url, headers=self.headers, json=command,
                                         timeout=(3, 5), allow_redirects=False)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or "error" in payload or "result" not in payload:
                raise ConfigPersistenceError("Redis rejected the configuration operation.")
            sync_token = response.headers.get("Upstash-Sync-Token")
            if sync_token:
                self.headers["Upstash-Sync-Token"] = sync_token
            return payload["result"]
        except ConfigPersistenceError:
            raise
        except Exception:
            # HTTP exceptions can contain URLs; never surface transport details.
            raise ConfigPersistenceError("Redis configuration storage is unavailable.") from None

    def load(self):
        raw = self.execute(["GET", self.key])
        if raw is None:
            raise ConfigPersistenceError("Redis configuration is missing; import the existing state before starting.")
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            raise ConfigPersistenceError("Redis configuration is not valid JSON.") from None
        return validate_config(value)

    def save(self, value):
        validate_config(value)
        # SET is atomic; XX prevents silently recreating a deleted key. No TTL.
        result = self.execute(["SET", self.key, json.dumps(value, ensure_ascii=False), "XX"])
        if result != "OK":
            raise ConfigPersistenceError("Redis configuration is missing; refusing to recreate it.")


def store_from_env():
    backend = os.getenv("FEZ_CONFIG_BACKEND", "file")
    if backend == "file":
        return None
    if backend != "redis":
        raise ConfigPersistenceError("FEZ_CONFIG_BACKEND must be file or redis.")
    return UpstashConfigStore(os.getenv("UPSTASH_REDIS_REST_URL", ""),
                              os.getenv("UPSTASH_REDIS_REST_TOKEN", ""),
                              os.getenv("FEZ_REDIS_KEY", "discordfezcollector:production:config"))
