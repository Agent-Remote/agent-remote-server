"""
只接受当前后端完整且新鲜的技能报告，不从其他后端或旧心跳猜测支持。
"""

from datetime import UTC, datetime

from agent_remote_server.config import Settings
from agent_remote_server.models import Node


def supports_managed_skills(node: Node, backend: str, settings: Settings) -> bool:
    """
    严格整数版本与布尔能力必须完整匹配，报告有效期复用节点离线阈值。

    :param node (Node): 本次候选节点
    :param backend (str): 账户固定后端
    :param settings (Settings): 服务端部署策略
    :return bool: 是否允许下发受管准备任务
    """
    heartbeat = node.last_heartbeat_at
    if heartbeat is None:
        return False
    age = (datetime.now(UTC) - heartbeat.replace(tzinfo=heartbeat.tzinfo or UTC)).total_seconds()
    if age < 0 or age >= settings.node_offline_after_seconds:
        return False
    reports = node.runtime_capabilities.get("skill_manager")
    report = reports.get(backend) if isinstance(reports, dict) else None
    if not isinstance(report, dict):
        return False
    return valid_backend_report(report)


def valid_backend_report(report: object) -> bool:
    """
    不允许布尔值冒充整数版本，也不允许整数冒充能力开关。

    :param report (object): 未受信后端报告
    :return bool: 是否完整符合已支持协议
    """
    if not isinstance(report, dict):
        return False
    return all(
        type(report.get(field)) is int and report[field] == 1
        for field in ("protocol_version", "manifest_version")
    ) and all(
        report.get(field) is True for field in ("writable_copies", "finalization", "recovery")
    )


def normalize_skill_capabilities(value: object) -> dict[str, object]:
    """
    无效新报告清空该后端，避免 JSON 的 True 与 1 相等导致保留旧能力。

    :param value (object): 心跳中未受信技能报告
    :return dict[str, object]: 仅包含独立完整匹配的后端
    """
    if not isinstance(value, dict):
        return {}
    return {
        backend: _normalize_backend(value[backend])
        for backend in ("native", "docker_sandbox")
        if valid_backend_report(value.get(backend))
    }


def _normalize_backend(report: dict[str, object]) -> dict[str, object]:
    """
    可选部署版本无效时移除字段，避免 True 与整数一相等导致 ORM 保留旧授权。

    :param report (dict[str, object]): 已验证基本协议的后端报告
    :return dict[str, object]: 保留基本能力并严格归一化可选部署声明
    """
    result = dict(report)
    version = result.get("deployment_protocol_version")
    if type(version) is not int or version != 1:
        result.pop("deployment_protocol_version", None)
    return result
