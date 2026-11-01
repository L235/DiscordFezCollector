"""Compile routing configuration once per edit, not once per stream event."""
from src.logging_setup import logger
from src.models import CustomFilter


class RoutingPlan:
    def __init__(self, config, webhook_keys):
        self.filters = []
        for thread_id, entry in config.get('threads', {}).items():
            if entry.get('active', False):
                self.filters.append(('thread', int(thread_id), CustomFilter(entry['config'])))
        for key, entry in config.get('receivers', {}).items():
            if entry.get('active', False) and not entry.get('errored', False) and key in webhook_keys:
                self.filters.append(('receiver', key, CustomFilter(entry['config'])))
        self.users = frozenset(user for _, _, filt in self.filters for user in filt.user_include)
        self.pages = tuple(filt.page_include for _, _, filt in self.filters if filt.page_include)
        self.summaries = tuple(filt.sum_include for _, _, filt in self.filters if filt.sum_include)

    def route(self, change):
        user = change.get('user')
        if not user:
            return []
        title = change.get('title', '')
        comment = change.get('log_action_comment') or change.get('comment') or ''
        if (user not in self.users
                and not any(rx.search(title) for rx in self.pages)
                and not any(rx.search(comment) for rx in self.summaries)):
            return []
        destinations = []
        for kind, key, filt in self.filters:
            try:
                if filt.matches(change):
                    destinations.append((kind, key, filt.link_style))
            except Exception:
                logger.exception('Filter failed for %s %s', kind, key)
        return destinations


class RoutingPlanCache:
    def __init__(self):
        self.revision = None
        self.plan = None

    def get(self, config, webhook_keys, revision):
        if self.plan is None or self.revision != revision:
            plan = RoutingPlan(config, webhook_keys)
            self.plan = plan
            self.revision = revision
        return self.plan
