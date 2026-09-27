"""
保留精确 checkpoint 依赖校验入口，所有关系由统一保留历史图定义。
"""

from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.services.skills.retention.dependencies import (
    history_dependencies,
    require_history_dependencies,
)
from agent_remote_server.skill_manager.retention.graph import RetentionKey


def require_checkpoint_dependencies(index: RetentionIndex, selected: set[RetentionKey]) -> None:
    """
    检查尚未同批退役的 checkpoint 消费者，不把 parent 审计引用当作内容依赖。

    :param index (RetentionIndex): 同一用户锁内的完整引用集合
    :param selected (set[RetentionKey]): 已通过保护和等待期检查的实际退役身份
    """
    require_history_dependencies(
        tuple(edge for edge in history_dependencies(index) if edge.dependency.kind == "checkpoint"),
        selected,
    )
