"""
验证方向性依赖的完整候选分组，环和共享外部输入不造成误删或错误阻断。
"""

from agent_remote_server.skill_manager.retention.dependencies import HistoryDependency
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.retention.groups import history_groups


def test_waiting_consumer_blocks_entire_cycle_but_not_its_independent_consumers() -> None:
    """
    等待节点阻断其输入环，独立消费者可单独退役，普通 parent 不在图中产生隐式义务。
    """
    old, directory, waiting, leaf, other = tuple(
        RetentionKey("checkpoint", name)
        for name in ("old", "directory", "waiting", "leaf", "other")
    )
    edges = (
        HistoryDependency(directory, old, "member"),
        HistoryDependency(old, directory, "backing"),
        HistoryDependency(waiting, directory, "backing"),
        HistoryDependency(leaf, waiting, "input"),
    )
    result = history_groups(
        (old, directory, waiting, leaf, other),
        frozenset((old, directory, waiting, leaf, other)),
        edges,
        frozenset((waiting,)),
    )
    assert set(result.blocked) == {old, directory, waiting}
    assert set(result.roots) == {leaf, other}
    assert {frozenset(group) for group in result.groups} == {
        frozenset((leaf,)),
        frozenset((other,)),
    }


def test_shared_unselected_input_does_not_join_independent_loss_groups() -> None:
    """
    两个消费者共享受保护的未选输入时仍能分别退役，原输入不被反向扩张选入。
    """
    first, second, original = tuple(
        RetentionKey("checkpoint", name) for name in ("first", "second", "original")
    )
    edges = (
        HistoryDependency(first, original, "input"),
        HistoryDependency(second, original, "input"),
    )
    result = history_groups((first, second), frozenset((first, second)), edges, frozenset())
    assert not result.blocked
    assert result.groups == ((first,), (second,))


def test_dependent_consumers_are_included_and_retired_evidence_is_not_loss() -> None:
    """
    闭包可跨越原始根但完整纳入同组，已退役审计证据不会被显示成新的恢复损失。
    """
    old, consumer, retired = tuple(
        RetentionKey("checkpoint", name) for name in ("old", "consumer", "retired")
    )
    result = history_groups(
        (old, retired),
        frozenset((old, consumer)),
        (HistoryDependency(consumer, old, "input"),),
        frozenset(),
    )
    assert set(result.roots) == {old, retired}
    assert result.groups == (tuple(sorted((old, consumer))),)
