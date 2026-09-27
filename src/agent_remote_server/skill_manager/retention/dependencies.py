"""
有界展开仍承诺可恢复内容的消费者闭包，允许完整目录和 backing 形成依赖环。
"""

from collections import deque
from dataclasses import dataclass

from agent_remote_server.skill_manager.retention.graph import RetentionKey

MAX_RETIREMENT_IDENTITIES = 1_000_000
MAX_HISTORY_EDGES = 4_000_000


@dataclass(frozen=True, order=True)
class HistoryDependency:
    """
    消费历史对精确输入的保留义务；字段名用于审阅，不依据名称推断来源。
    """

    consumer: RetentionKey
    dependency: RetentionKey
    relation: str


@dataclass(frozen=True)
class HistoryClosure:
    """
    原始选择、全部连带消费者及其完整输入边；展示其他输入不把它们自动选为退役目标。
    """

    required: tuple[RetentionKey, ...]
    dependencies: tuple[HistoryDependency, ...]


def dependent_closure(
    requested: tuple[RetentionKey, ...],
    dependencies: tuple[HistoryDependency, ...],
    retained: frozenset[RetentionKey],
    *,
    max_identities: int = MAX_RETIREMENT_IDENTITIES,
    max_edges: int = MAX_HISTORY_EDGES,
) -> HistoryClosure:
    """
    从待失去内容的身份反向展开全部仍保留的消费者，环和共享引用只遍历一次。

    :param requested (tuple[RetentionKey, ...]): 精确初始选择
    :param dependencies (tuple[HistoryDependency, ...]): 完整保留历史依赖
    :param retained (frozenset[RetentionKey]): 尚承诺可恢复的历史及虚拟身份
    :param max_identities (int): 全部相关身份安全预算
    :param max_edges (int): 完整输入边安全预算
    :return HistoryClosure: 稳定排序的完整相关闭包，超限不会返回截断集合
    """
    if len(dependencies) > max_edges:
        raise ValueError("history dependency edge limit exceeded")
    consumers: dict[RetentionKey, list[HistoryDependency]] = {}
    for edge in dependencies:
        consumers.setdefault(edge.dependency, []).append(edge)
    required = set(requested)
    if len(required) > max_identities:
        raise ValueError("history dependency identity limit exceeded")
    pending = deque(sorted(required & retained))
    while pending:
        identity = pending.popleft()
        for edge in consumers.get(identity, ()):
            if edge.consumer not in retained:
                continue
            if edge.consumer not in required:
                if len(required) >= max_identities:
                    raise ValueError("history dependency identity limit exceeded")
                required.add(edge.consumer)
                pending.append(edge.consumer)
    related = {
        edge for edge in dependencies if edge.consumer in required and edge.consumer in retained
    }
    return HistoryClosure(tuple(sorted(required)), tuple(sorted(related)))
