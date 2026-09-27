"""
区分离线配置受理与新鲜部署授权，复用同一严格后端协议检查。
"""

from agent_remote_server.config import Settings
from agent_remote_server.models import Node
from agent_remote_server.services.skills.runtime_capability import (
    supports_managed_skills,
    valid_backend_report,
)


def supports_deployment(
    node: Node, backend: str | None, tool: str, settings: Settings, *, fresh: bool
) -> bool:
    """
    已知兼容的离线节点可以等待，但实际下发仍要求当前健康和未过期心跳。

    :param node (Node): 从数据库读取的原节点
    :param backend (str | None): 账户固定后端
    :param tool (str): 账户工具类型
    :param settings (Settings): 明确部署策略
    :param fresh (bool): 是否要求本次实际执行权限
    :return bool: 是否满足对应阶段的协议要求
    """
    reports = node.runtime_capabilities.get("skill_manager")
    report = reports.get("native") if isinstance(reports, dict) else None
    available = node.runtime_capabilities.get("backends")
    return (
        settings.skill_manager_enabled
        and backend == "native"
        and node.status
        in ({"healthy", "degraded"} if fresh else {"healthy", "degraded", "offline"})
        and node.last_heartbeat_at is not None
        and tool in node.supported_tool_types
        and "native" in node.allowed_runtime_backends
        and isinstance(available, list)
        and "native" in available
        and valid_backend_report(report)
        and isinstance(report, dict)
        and type(report.get("deployment_protocol_version")) is int
        and report["deployment_protocol_version"] == 1
        and (not fresh or supports_managed_skills(node, "native", settings))
    )
