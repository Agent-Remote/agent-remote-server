"""
验证有界闭包、共享理由及额度分类与底层文件的区别。
"""

import pytest

from agent_remote_server.skill_manager.retention.graph import RetentionGraph


def test_shared_inputs_and_cycles_preserve_every_protection_reason() -> None:
    """
    循环和菱形引用不丢保护理由，也不会导致无限遍历。
    """
    graph = RetentionGraph()
    graph.root("checkpoint", "a", "current_branch")
    graph.root("checkpoint", "b", "active_snapshot")
    graph.edge("checkpoint", "a", "state_tree", "shared")
    graph.edge("checkpoint", "b", "state_tree", "shared")
    graph.edge("state_tree", "shared", "checkpoint", "a")
    graph.edge("state_tree", "shared", "state_object", "bytes")
    graph.edge("checkpoint", "unused", "state_tree", "unused")
    result = graph.protect()
    assert result.reasons("state_object", "bytes") == {"current_branch", "active_snapshot"}
    assert not result.reasons("state_tree", "unused")
    assert graph.protect() == result


@pytest.mark.parametrize("resource", ["nodes", "edges"])
def test_graph_limits_fail_instead_of_returning_partial_deletion_analysis(resource: str) -> None:
    """
    保活图超限整体拒绝，重复边不错误耗尽预算。

    :param resource (str): 要验证的资源维度
    """
    graph = RetentionGraph(max_nodes=2 if resource == "nodes" else 10, max_edges=1)
    graph.edge("checkpoint", "a", "checkpoint", "b")
    graph.edge("checkpoint", "a", "checkpoint", "b")
    with pytest.raises(ValueError, match="limit exceeded"):
        graph.edge("checkpoint", "b", "checkpoint", "c")
    with pytest.raises(ValueError, match="incomplete"):
        graph.protect()


def test_category_release_never_implies_shared_blob_deletion() -> None:
    """
    即使运行分类无根，包分类仍保活同摘要底层文件。
    """
    graph = RetentionGraph()
    graph.root("revision", "r", "pin")
    graph.edge("revision", "r", "package_tree", "tree")
    graph.edge("package_tree", "tree", "package_object", "same")
    graph.edge("package_object", "same", "blob", "same")
    graph.edge("state_tree", "unused", "state_object", "same")
    graph.edge("state_object", "same", "blob", "same")
    result = graph.protect()
    assert result.reasons("blob", "same") == {"pin"}
    assert not result.reasons("state_object", "same")
