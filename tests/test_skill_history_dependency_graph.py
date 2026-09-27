"""
验证完整恢复损失闭包的方向、循环、边界及资源失败，不把输入依赖错误展开为删除目标。
"""

import pytest

from agent_remote_server.skill_manager.retention.dependencies import (
    HistoryDependency,
    dependent_closure,
)
from agent_remote_server.skill_manager.retention.graph import RetentionKey


def test_consumer_closure_keeps_cycles_and_does_not_select_other_inputs() -> None:
    """
    反向展开消费者而不扩张其任意输入，完整环和多个字段证据保留且排序稳定。
    """
    item, directory, snapshot, other = (
        RetentionKey("checkpoint", value) for value in ("item", "directory", "snapshot", "other")
    )
    edges = (
        HistoryDependency(item, directory, "backing"),
        HistoryDependency(directory, item, "member"),
        HistoryDependency(snapshot, directory, "starting"),
        HistoryDependency(snapshot, directory, "saved"),
        HistoryDependency(snapshot, other, "other_input"),
    )
    retained = frozenset((item, directory, snapshot, other))
    result = dependent_closure((item,), edges, retained)
    assert result.required == tuple(sorted((item, directory, snapshot)))
    assert result.dependencies == tuple(sorted(edges))
    assert result == dependent_closure((item,), tuple(reversed(edges)), retained)
    assert dependent_closure((item,), edges, frozenset()).required == (item,)


@pytest.mark.parametrize("identity_limit,edge_limit", [(2, 10), (10, 1)])
def test_dependency_budget_refuses_complete_result(identity_limit: int, edge_limit: int) -> None:
    """
    超过节点或边预算不能返回第一批看似可删的身份。

    :param identity_limit (int): 本次节点预算
    :param edge_limit (int): 本次边预算
    """
    keys = tuple(RetentionKey("checkpoint", str(value)) for value in range(3))
    edges = tuple(HistoryDependency(keys[i + 1], keys[i], "member") for i in range(2))
    with pytest.raises(ValueError, match="limit exceeded"):
        dependent_closure(
            (keys[0],), edges, frozenset(keys), max_identities=identity_limit, max_edges=edge_limit
        )
