"""Runtime tuning knobs, all overridable via environment variables.

Settings, not domain models: retry/backoff policies and pipeline limits.
"""

import os


# Timeout and retry configuration (for EventStreams)
class RetryConfig:
    INITIAL_BACKOFF_SECS = int(os.getenv("RETRY_INITIAL_BACKOFF_SECS", "5"))
    MAX_BACKOFF_SECS = int(os.getenv("RETRY_MAX_BACKOFF_SECS", "300"))
    GRACE_PERIOD_SECS = int(os.getenv("RETRY_GRACE_PERIOD_SECS", "5"))


# Discord API retry configuration (for 5xx server errors)
class DiscordRetryConfig:
    MAX_ATTEMPTS = int(os.getenv("DISCORD_RETRY_MAX_ATTEMPTS",
                       os.getenv("DISCORD_RATE_LIMIT_MAX_ATTEMPTS", "5")))
    INITIAL_BACKOFF_SECS = float(os.getenv("DISCORD_RETRY_INITIAL_BACKOFF_SECS",
                                 os.getenv("DISCORD_RATE_LIMIT_INITIAL_BACKOFF_SECS", "2")))
    MAX_BACKOFF_SECS = float(os.getenv("DISCORD_RETRY_MAX_BACKOFF_SECS",
                             os.getenv("DISCORD_RATE_LIMIT_MAX_BACKOFF_SECS", "60")))


# EventStream configuration
class EventStreamConfig:
    TIMEOUT_SECS = int(os.getenv("EVENTSTREAM_TIMEOUT_SECS", "600"))  # 10 minutes default
    CHECK_INTERVAL_SECS = int(os.getenv("EVENTSTREAM_CHECK_INTERVAL_SECS", "60"))
    QUEUE_MAX_SIZE = int(os.getenv("EVENTSTREAM_QUEUE_MAX_SIZE", "2000"))
    QUEUE_PUT_TIMEOUT_SECS = float(os.getenv("EVENTSTREAM_QUEUE_PUT_TIMEOUT_SECS", "30.0"))
    QUEUE_RESULT_TIMEOUT_SECS = float(os.getenv("EVENTSTREAM_QUEUE_RESULT_TIMEOUT_SECS", "35.0"))
    ERROR_BODY_LIMIT = int(os.getenv("EVENTSTREAM_ERROR_BODY_LIMIT", "2000"))
