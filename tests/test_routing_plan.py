"""Routing optimizations must preserve destinations and see configuration edits."""
import copy

import pytest


def settings():
    return {
        'threads': {
            '101': {'active': True, 'config': {'userIncludeList': ['Alice'], 'linkStyle': 'action'}},
            '102': {'active': False, 'config': {'userIncludeList': ['Alice']}},
        },
        'receivers': {
            'good': {'active': True, 'config': {'pageIncludePatterns': ['^Target'], 'userExcludeList': ['Blocked']}},
            'broken': {'active': True, 'errored': True, 'config': {'userIncludeList': ['Alice']}},
            'unconfigured': {'active': True, 'config': {'userIncludeList': ['Alice']}},
        },
    }


def event(user='Alice', title='Target article', **fields):
    return {'user': user, 'title': title, 'comment': '', 'server_name': 'en.wikipedia.org', **fields}


def test_routes_only_enabled_configured_destinations():
    from src.routing import RoutingPlanCache
    plan = RoutingPlanCache().get(settings(), {'good', 'broken'}, 0)
    assert plan.route(event()) == [('thread', 101, 'action'), ('receiver', 'good', 'title')]
    assert plan.route(event(user='Blocked')) == []
    assert plan.route(event(user='Nobody', title='Unrelated')) == []


def test_site_and_exclusion_filters_survive_global_prefilter():
    from src.routing import RoutingPlanCache
    cfg = settings()
    cfg['threads']['101']['config'].update({'siteName': 'EN.WIKIPEDIA.ORG', 'pageExcludePatterns': ['Secret']})
    plan = RoutingPlanCache().get(cfg, set(), 0)
    assert plan.route(event(title='Normal')) == [('thread', 101, 'action')]
    assert plan.route(event(title='Secret')) == []
    assert plan.route(event(server_name='de.wikipedia.org')) == []


def test_log_action_comment_is_used_for_summary_filters():
    from src.routing import RoutingPlanCache
    cfg = {'threads': {'101': {'active': True, 'config': {'summaryIncludePatterns': ['blocked']}}}}
    plan = RoutingPlanCache().get(cfg, set(), 0)
    assert plan.route(event(user='Other', log_action_comment='Blocked test user')) == [('thread', 101, 'title')]
    assert plan.route(event(user='Other', comment='unrelated')) == []


def test_cached_plan_is_not_recompiled_between_configuration_edits():
    from src.routing import RoutingPlanCache
    cfg = settings()
    cache = RoutingPlanCache()
    plan = cache.get(cfg, {'good'}, 0)
    # A cache hit must not scan or serialize the routing configuration again.
    class UnreadableConfig(dict):
        def get(self, *args):
            raise AssertionError('Repeated configuration scan')
    assert cache.get(UnreadableConfig(), {'good'}, 0).route(event()) == plan.route(event())
    changed = copy.deepcopy(cfg)
    changed['threads']['101']['config']['userIncludeList'] = ['Bob']
    assert cache.get(changed, {'good'}, 1).route(event(title='Unrelated')) == []
    assert cache.get(changed, {'good'}, 1).route(event(user='Bob', title='Unrelated')) == [('thread', 101, 'action')]


@pytest.mark.asyncio
async def test_live_plan_refreshes_after_list_edit_and_failed_disk_write(tmp_path, monkeypatch):
    import src.config as config
    import src.event_streams as streams
    from src.routing import RoutingPlanCache
    cfg = settings()
    monkeypatch.setattr(config, 'CONFIG', cfg)
    monkeypatch.setattr(streams, 'CONFIG', cfg)
    monkeypatch.setattr(streams, 'WEBHOOKS', {})
    monkeypatch.setattr(streams, 'ROUTING_CACHE', RoutingPlanCache())
    monkeypatch.setattr(config, 'STATE_FILE', tmp_path / 'missing-directory' / 'config.json')
    assert (await streams.get_routing_plan()).route(event(user='Bob', title='Other')) == []
    await config.mutate_custom_thread_config_list('101', 'userIncludeList', add=['Bob'])
    assert (await streams.get_routing_plan()).route(event(user='Bob', title='Other')) == [('thread', 101, 'action')]
    await config.set_custom_thread_active('101', False)
    assert (await streams.get_routing_plan()).route(event(user='Bob', title='Other')) == []


@pytest.mark.asyncio
async def test_live_plan_refreshes_after_raw_configuration_replacement(tmp_path, monkeypatch):
    import src.config as config
    import src.event_streams as streams
    from src.routing import RoutingPlanCache
    cfg = settings()
    monkeypatch.setattr(config, 'CONFIG', cfg)
    monkeypatch.setattr(streams, 'CONFIG', cfg)
    monkeypatch.setattr(streams, 'WEBHOOKS', {})
    monkeypatch.setattr(streams, 'ROUTING_CACHE', RoutingPlanCache())
    monkeypatch.setattr(config, 'STATE_FILE', tmp_path / 'config.json')
    assert (await streams.get_routing_plan()).route(event(title='Other')) == [('thread', 101, 'action')]
    cfg.clear()
    cfg.update({'threads': {}, 'receivers': {}})
    config.save_config(cfg)
    assert (await streams.get_routing_plan()).route(event()) == []
