# Configuration persistence

The bot supports a local JSON file (the default) or one persistent Upstash Redis
key. Event routing reads the in-memory configuration; Redis is contacted at
startup and when settings change, not for each Wikimedia event.

## Runtime settings

| Variable | Meaning |
| --- | --- |
| `FEZ_CONFIG_BACKEND` | `file` (default) or `redis`. Unknown values stop startup. |
| `FEZ_COLLECTOR_STATE` | JSON path in file mode; ignored in Redis mode. |
| `UPSTASH_REDIS_REST_URL` | HTTPS REST endpoint for Redis mode. |
| `UPSTASH_REDIS_REST_TOKEN` | Secret REST credential; keep out of source control. |
| `FEZ_REDIS_KEY` | Defaults to `discordfezcollector:production:config`. |

Use a persistent database with eviction disabled and no TTL on the config key.
Run only one bot instance: updates are serialized inside that process, but there
is no cross-process compare-and-swap. A dedicated ACL credential needs only
`GET` and `SET` access to the exact config key. If the selected plan does not
support scoped credentials, decide whether to use a separate database before
sharing an unrestricted token with another application.

In Redis mode, missing, invalid, or unreachable state stops startup. There is no
fallback to a stale file or empty defaults. Saves atomically replace the document
with `SET ... XX`, without expiration, and never recreate a missing key. Network
requests have 3-second connect and 5-second read timeouts, run off the event loop,
and do not retry automatically. An ambiguous timeout can mean Redis accepted a
write without confirming it; the command reports failure and the operator can
retry the intended setting. No successful save is acknowledged before Redis
confirms it. Automatic disabling of a broken destination still applies in memory
if storage fails, to keep the stream running; a warning records that limitation,
and a later successful edit saves the current configuration. Unconfirmed saves
remain marked dirty, so repeating an unchanged setting retries persistence.
Single-field edits apply to the latest state inside the edit lock. Slash commands
are deferred before their callbacks wait on locks or I/O, and both text and slash
commands report storage failures after unwrapping Discord's invocation errors.

## Migration

1. Keep the old deployment and volume available for rollback. Stop the bot before
   the final snapshot so no settings can change during the handoff.
2. Back up every state file on the volume. Use the active
   `FEZ_COLLECTOR_STATE` file for import; historical files are backups only.
3. Set the Redis variables above in a trusted local shell. Use `venv/` for Python.
   The import command does not start the bot, connect to Discord, or overwrite an
   existing different Redis value:

   ```sh
   venv/bin/python -m src.migrate_config import /path/to/final-state.json
   ```

   Import uses `SET ... NX` and reads back the JSON to confirm equality. Repeating
   the same import is safe; different existing state causes an error for manual
   reconciliation. Never expire or evict this key.
4. Set the same variables on Railway and deploy the new code as one instance.
   Keep the volume attached during verification. Confirm the log says
   `Loaded configuration from Redis`, the expected threads and receivers exist,
   and normal event processing resumes. Restart and verify the state still loads.
5. Export a fresh Redis backup. Once operation and recoverability are verified,
   retire the Railway volume to end its recurring storage charge. Detaching alone
   may leave a billable volume; confirm the resource is actually removed.

## Rollback

Stop the Redis-backed bot first. Preserve edits made since cutover by exporting
the current Redis document to a **new** local file:

```sh
venv/bin/python -m src.migrate_config export /path/to/redis-rollback.json
```

Exports have owner-only permissions and refuse to overwrite existing files.
Restore this JSON to the Railway volume, set `FEZ_CONFIG_BACKEND=file` and
`FEZ_COLLECTOR_STATE` to that file, then deploy/start one instance. If Redis is
unavailable, use the final pre-cutover snapshot, understanding that later edits
will not be present. After the old volume is retired, file-mode rollback requires
a new persistent volume. Keep the Redis key until rollback has been verified.
