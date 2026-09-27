"""
在线性图遍历中传播消费者阻断，并生成完整可执行历史依赖组。
"""

from collections import deque
from dataclasses import dataclass

from agent_remote_server.skill_manager.retention.dependencies import (
    HistoryDependency,
    dependent_closure,
)
from agent_remote_server.skill_manager.retention.graph import RetentionKey


@dataclass(frozen=True)
class HistoryGroups:
    """
    完整阻断集合、可执行原根与不可拆分组，不隐藏跨来源的连带恢复损失。
    """

    blocked: tuple[RetentionKey, ...]
    roots: tuple[RetentionKey, ...]
    groups: tuple[tuple[RetentionKey, ...], ...]


def history_groups(
    requested: tuple[RetentionKey, ...],
    retained: frozenset[RetentionKey],
    dependencies: tuple[HistoryDependency, ...],
    blocked: frozenset[RetentionKey],
) -> HistoryGroups:
    """
    阻断沿消费义务传向输入，未选中的共同输入不会错误合并可独立退役的消费者。

    :param requested (tuple[RetentionKey, ...]): 全部初始根
    :param retained (frozenset[RetentionKey]): 完整闭包中的保留身份
    :param dependencies (tuple[HistoryDependency, ...]): 全部相关输入边
    :param blocked (frozenset[RetentionKey]): 具有直接阻断的身份
    :return HistoryGroups: 稳定排序的传播阻断、可执行根与完整组
    """
    inputs: dict[RetentionKey, set[RetentionKey]] = {}
    for edge in dependencies:
        if edge.consumer in retained and edge.dependency in retained:
            inputs.setdefault(edge.consumer, set()).add(edge.dependency)
    rejected = set(blocked)
    pending = deque(sorted(blocked))
    while pending:
        for dependency in inputs.get(pending.popleft(), ()):
            if dependency not in rejected:
                rejected.add(dependency)
                pending.append(dependency)
    roots = tuple(sorted(set(requested) - rejected))
    selected = set(dependent_closure(roots, dependencies, retained).required)
    if selected & rejected:
        raise ValueError("history group selection includes a blocked consumer")
    selected.intersection_update(retained)
    adjacent: dict[RetentionKey, set[RetentionKey]] = {}
    for edge in dependencies:
        if edge.consumer in selected and edge.dependency in selected:
            adjacent.setdefault(edge.consumer, set()).add(edge.dependency)
            adjacent.setdefault(edge.dependency, set()).add(edge.consumer)
    groups = []
    for seed in sorted(selected):
        if seed not in selected:
            continue
        component = {seed}
        selected.remove(seed)
        pending = deque((seed,))
        while pending:
            for neighbor in adjacent.get(pending.popleft(), ()):
                if neighbor in selected:
                    selected.remove(neighbor)
                    component.add(neighbor)
                    pending.append(neighbor)
        groups.append(tuple(sorted(component)))
    return HistoryGroups(tuple(sorted(rejected)), roots, tuple(groups))
