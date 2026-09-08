"""组合 ego-browser 服务的公开入口。"""

from agent_remote_server.services.ego_browser.allowlist import (
    _EgoBrowserAllowlistOperations,
)


class EgoBrowserService(_EgoBrowserAllowlistOperations):
    """本地 ego-browser Bridge 的独立生命周期和 admission 服务。"""
