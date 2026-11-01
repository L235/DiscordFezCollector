"""A slow or failing destination must not stop independent destinations."""
import asyncio

import pytest


@pytest.mark.asyncio
async def test_slow_destination_does_not_block_other_destinations():
    from src.delivery import DestinationDispatcher
    slow_started, release_slow, fast_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    delivered = []
    async def deliver(key, message):
        if key == 'slow':
            slow_started.set()
            await release_slow.wait()
        delivered.append((key, message))
        if key == 'fast':
            fast_done.set()
    dispatcher = DestinationDispatcher(deliver, max_queue=2)
    try:
        assert dispatcher.submit('slow', 'one')
        await asyncio.wait_for(slow_started.wait(), 1)
        assert dispatcher.submit('fast', 'two')
        await asyncio.wait_for(fast_done.wait(), 1)
        assert delivered == [('fast', 'two')]
    finally:
        release_slow.set()
        await dispatcher.close()


@pytest.mark.asyncio
async def test_full_destination_queue_drops_newest_and_preserves_order():
    from src.delivery import DestinationDispatcher
    started, release, done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    delivered = []
    async def deliver(key, message):
        started.set()
        await release.wait()
        delivered.append(message)
        if len(delivered) == 2:
            done.set()
    dispatcher = DestinationDispatcher(deliver, max_queue=1)
    try:
        assert dispatcher.submit('one', 'first')
        await asyncio.wait_for(started.wait(), 1)
        assert dispatcher.submit('one', 'second')
        assert dispatcher.submit('one', 'third') is False
        release.set()
        await asyncio.wait_for(done.wait(), 1)
        assert delivered == ['first', 'second']
    finally:
        await dispatcher.close()


@pytest.mark.asyncio
async def test_failed_delivery_does_not_kill_destination_worker():
    from src.delivery import DestinationDispatcher
    done = asyncio.Event()
    delivered = []
    async def deliver(key, message):
        if message == 'bad':
            raise RuntimeError('temporary failure')
        delivered.append(message)
        done.set()
    dispatcher = DestinationDispatcher(deliver, max_queue=2)
    try:
        assert dispatcher.submit('one', 'bad')
        assert dispatcher.submit('one', 'good')
        await asyncio.wait_for(done.wait(), 1)
        assert delivered == ['good']
    finally:
        await dispatcher.close()


@pytest.mark.asyncio
async def test_close_cancels_inflight_work_and_rejects_new_messages():
    from src.delivery import DestinationDispatcher
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def deliver(key, message):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    dispatcher = DestinationDispatcher(deliver, max_queue=1)
    dispatcher.submit('one', 'first')
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(dispatcher.close(), 1)
    assert cancelled.is_set()
    assert dispatcher.submit('one', 'late') is False


@pytest.mark.asyncio
async def test_idle_workers_release_resources_and_can_restart():
    from src.delivery import DestinationDispatcher
    delivered = []
    async def deliver(key, message):
        delivered.append(message)
    dispatcher = DestinationDispatcher(deliver, idle_timeout=0.01)
    try:
        dispatcher.submit('one', 'first')
        await asyncio.wait_for(dispatcher.workers['one'].task, 1)
        assert not dispatcher.workers
        dispatcher.submit('one', 'second')
        await asyncio.wait_for(dispatcher.workers['one'].task, 1)
        assert delivered == ['first', 'second']
        assert not dispatcher.workers
    finally:
        await dispatcher.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('permanent', [False, True])
async def test_stream_worker_keeps_other_receivers_running_after_failure(tmp_path, monkeypatch, permanent):
    import time
    from types import SimpleNamespace
    import src.config as config
    import src.event_streams as streams
    from src.routing import RoutingPlanCache
    from src.discord_utils import TransientWebhookError, WebhookError

    cfg = {'threads': {}, 'receivers': {
        key: {'active': True, 'errored': False, 'config': {'userIncludeList': ['Alice']}}
        for key in ['failing', 'healthy']
    }}
    monkeypatch.setattr(config, 'CONFIG', cfg)
    monkeypatch.setattr(config, 'STATE_FILE', tmp_path / 'state.json')
    monkeypatch.setattr(streams, 'CONFIG', cfg)
    monkeypatch.setattr(streams, 'WEBHOOKS', {'failing': 'test-failing', 'healthy': 'test-healthy'})
    monkeypatch.setattr(streams, 'ROUTING_CACHE', RoutingPlanCache())
    # The only mocked input is the external blocking Wikimedia reader.
    queue_type = asyncio.Queue
    incoming = queue_type()
    monkeypatch.setattr(streams.asyncio, 'Queue', lambda maxsize=0: incoming if maxsize == streams.EventStreamConfig.QUEUE_MAX_SIZE else queue_type(maxsize))
    monkeypatch.setattr(streams.threading, 'Thread', lambda **kwargs: SimpleNamespace(start=lambda: None))
    delivered, attempts = [], []
    done = asyncio.Event()
    async def send(url, content, key):
        attempts.append(key)
        if key == 'failing' and attempts.count(key) == 1:
            error = WebhookError if permanent else TransientWebhookError
            raise error('simulated external failure')
        delivered.append(key)
        if delivered.count('healthy') == 2:
            done.set()
        return True
    monkeypatch.setattr(streams, 'send_webhook_with_backoff', send)
    event = {'timestamp': time.time(), 'user': 'Alice', 'title': 'Article', 'type': 'new', 'server_name': 'en.wikipedia.org'}
    incoming.put_nowait(event)
    incoming.put_nowait(event.copy())
    worker = asyncio.create_task(streams.stream_worker(None))
    try:
        await asyncio.wait_for(done.wait(), 1)
        assert delivered.count('healthy') == 2
        if permanent:
            assert attempts.count('failing') == 1
            assert cfg['receivers']['failing']['active'] is False
        else:
            assert attempts.count('failing') == 2
            assert delivered.count('failing') == 1
            assert cfg['receivers']['failing']['active'] is True
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
