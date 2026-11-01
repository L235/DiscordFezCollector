# Routing and delivery

The Wikimedia reader feeds one bounded ingestion queue. The consumer checks freshness, matches filters, formats each link style once, and submits messages to a separate FIFO queue for each Discord thread or webhook receiver. A slow destination's retry or rate-limit wait does not stop routing or deliveries to other destinations.

Routing filters and the global inclusion prefilter are compiled once per configuration revision. Every runtime config edit must hold `CONFIG_LOCK` and call `save_config(CONFIG)`, including raw config replacement. `save_config` advances the revision even when persistence fails because the in-memory configuration remains authoritative. Webhook environment configuration is fixed at process startup.

Each destination has one asynchronous worker and at most `DESTINATION_QUEUE_MAX_SIZE` pending messages (default 100, plus one in flight). A full destination queue drops the newest message without blocking other destinations; a warning reports the first and every hundredth overflow. Idle workers retire after five minutes and are recreated on demand. Existing Discord retry behavior remains in the sender. A permanent webhook failure disables that receiver; a transient failure drops that event and leaves the receiver enabled. Workers check that destinations are still enabled before sending queued messages.

Delivery remains best effort. Queues are in memory, with no replay or durable acknowledgment; queued and in-flight messages can be lost on shutdown. Messages accepted into each destination queue retain order, but destinations progress independently.

## Verification

Run `venv/bin/python -m pytest -q`. Tests cover routing semantics, config invalidation (including failed persistence), independent delivery, FIFO order, bounded overflow, failure recovery, idle cleanup, shutdown, and permanent/transient webhook failures through the real stream worker.

A local cProfile replay of 20,000 nonmatching events across 18 active filters took 2.316 seconds before this change and 0.326 seconds afterward. The previous code made 360,000 JSON fingerprint calls; the revised code builds one routing plan. This measures isolated routing work and is not a prediction of total production CPU savings.
