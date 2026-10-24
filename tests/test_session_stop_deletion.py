"""
验证停止失败不能成为删除依据，批量预检不释放任何候选历史。
"""

import asyncio
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from legacy_session_result_support import (
    LegacySessionCase,
    business_rows,
    current,
    report,
    start_case,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_config_import_requests import client as client
from test_sessions_api import auth_header

from agent_remote_server.models import NodeTask, NodeTaskResult, Session
from agent_remote_server.services import sessions as session_services


def stop_case(client: TestClient, *, cleanup: bool = False) -> tuple[LegacySessionCase, str]:
    """
    从真实运行会话建立显式停止或自动清理任务。

    :param client (TestClient): 隔离客户端
    :param cleanup (bool): 是否模拟自然退出触发清理
    :return tuple[LegacySessionCase, str]: 原会话和停止任务
    """
    case = start_case(client)
    assert report(case, case.task_id, "complete", case.result) == 200
    if cleanup:
        response = client.post(
            "/api/v1/node-api/reconcile",
            headers=auth_header(case.node_token),
            json={
                "node_id": case.node_id,
                "sections": ["runtime_sessions"],
                "snapshot": {
                    "sessions": [
                        {
                            "session_id": case.session_id,
                            "runtime_backend": current(case)["runtime_backend"],
                            "active": False,
                            "exit_reason": "process_exited",
                        }
                    ]
                },
            },
        )
        task_id = f"cleanup_tool_session:{case.session_id}"
    else:
        response = client.post(
            f"/api/v1/sessions/{case.session_id}/stop", headers=auth_header(case.token)
        )
        task_id = f"stop_tool_session:{case.session_id}"
    assert response.status_code == 200, response.text
    assert (
        client.post("/api/v1/node-api/tasks/poll", headers=auth_header(case.node_token)).status_code
        == 200
    )
    return case, task_id


@pytest.mark.parametrize("cleanup", [False, True])
def test_stop_failure_preserves_unresolved_lifecycle(client: TestClient, cleanup: bool) -> None:
    """
    失败收据保持原样且不能将停止未证明变成可删除终态。

    :param client (TestClient): 隔离客户端
    :param cleanup (bool): 自动清理与显式停止分支
    """
    case, task = stop_case(client, cleanup=cleanup)
    failure: dict[str, object] = {
        "code": "SESSION_STOP_UNPROVEN",
        "message": "Original stop is unproven.",
    }
    assert report(case, task, "fail", failure) == 200
    assert current(case)["status"] == ("interrupted" if cleanup else "stopping")
    before = business_rows(client)
    assert report(case, task, "fail", failure) == 200
    assert business_rows(client) == before
    assert (
        client.delete(
            f"/api/v1/sessions/{case.session_id}", headers=auth_header(case.token)
        ).status_code
        == 409
    )
    assert (
        report(case, task, "complete", {"status": "stopped", "session_id": case.session_id}) == 409
    )


async def alter_stop(case: LegacySessionCase, task_id: str, variant: str) -> str:
    """
    准备历史不一致停止记录和一个本可删除的同用户会话。

    :param case (LegacySessionCase): 原会话上下文
    :param task_id (str): 原停止任务
    :param variant (str): 历史状态或损坏类型
    :return str: 同批次正常失败会话标识
    """
    app = cast(FastAPI, case.client.app)
    async with app.state.session_factory() as session:
        runtime = await session.get(Session, UUID(case.session_id))
        task = await session.scalar(select(NodeTask).where(NodeTask.task_id == task_id))
        assert runtime is not None and task is not None
        runtime.status = "failed"
        task.status = (
            variant
            if variant in {"pending", "leased", "running", "failed", "cancelled", "expired"}
            else "succeeded"
        )
        if variant not in {
            "pending",
            "leased",
            "running",
            "failed",
            "cancelled",
            "expired",
            "missing",
        }:
            receipt = NodeTaskResult(
                node_task_id=task.id,
                task_id=task_id,
                status="succeeded",
                result={"status": "stopped", "session_id": case.session_id},
            )
            if variant == "wrong_row":
                original = await session.scalar(
                    select(NodeTask).where(NodeTask.task_id == case.task_id)
                )
                assert original is not None
                receipt.node_task_id = original.id
            elif variant == "wrong_status":
                receipt.status = "failed"
            elif variant == "error":
                receipt.error = {"code": "FAILED"}
            elif variant == "wrong_session":
                receipt.result = {"status": "stopped", "session_id": str(uuid4())}
            elif variant == "wrong_backend":
                receipt.result = {
                    "status": "stopped",
                    "session_id": case.session_id,
                    "runtime_backend": "invalid",
                }
            elif variant == "not_stopped":
                receipt.result = {"status": "running", "session_id": case.session_id}
            elif variant == "payload_backend":
                task.payload = {**task.payload, "runtime_backend": "invalid"}
            elif variant == "wrong_logical_receipt":
                receipt.task_id = "unrelated-task"
            elif variant == "wrong_type":
                task.task_type = "create_tool_session"
            elif variant == "wrong_binding":
                task.payload = {**task.payload, "session_id": str(uuid4())}
            elif variant == "duplicate":
                session.add(
                    NodeTaskResult(
                        node_task_id=task.id,
                        task_id=task_id,
                        status="succeeded",
                        result=receipt.result,
                    )
                )
            session.add(receipt)
        other = Session(
            tool_type=runtime.tool_type,
            user_id=runtime.user_id,
            tool_account_id=runtime.tool_account_id,
            workspace_id=runtime.workspace_id,
            node_id=runtime.node_id,
            project_key="other-failed",
            status="failed",
            tmux_session_name="other-failed",
            runtime_backend=runtime.runtime_backend,
        )
        session.add(other)
        await session.commit()
        return str(other.id)


@pytest.mark.parametrize("cleanup", [False, True])
@pytest.mark.parametrize(
    "variant",
    [
        "pending",
        "leased",
        "running",
        "failed",
        "cancelled",
        "expired",
        "missing",
        "wrong_row",
        "wrong_status",
        "error",
        "wrong_session",
        "wrong_backend",
        "not_stopped",
        "payload_backend",
        "wrong_logical_receipt",
        "wrong_type",
        "wrong_binding",
        "duplicate",
    ],
)
def test_unsettled_stop_blocks_single_and_atomic_bulk_delete(
    client: TestClient, cleanup: bool, variant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    任一停止不完整时单项与整批删除均无业务副作用。

    :param client (TestClient): 隔离客户端
    :param cleanup (bool): 是否通过清理任务关联
    :param variant (str): 历史停止的不一致状态
    :param monkeypatch (pytest.MonkeyPatch): 验证所有停止预检早于引用释放
    """
    case, task = stop_case(client, cleanup=cleanup)
    other = asyncio.run(alter_stop(case, task, variant))

    async def forbidden_release(session: AsyncSession, runtime: Session) -> None:
        """
        任一候选停止未解决时不能提前执行引用释放。

        :param session (AsyncSession): 删除事务
        :param runtime (Session): 候选原会话
        """
        raise AssertionError("reference release before all stop checks")

    monkeypatch.setattr(session_services, "release_retained_session_reference", forbidden_release)
    before = business_rows(client)
    for path in (f"/api/v1/sessions/{case.session_id}", "/api/v1/sessions"):
        response = client.delete(path, headers=auth_header(case.token))
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "SESSION_STOP_UNRESOLVED"
        assert business_rows(client) == before
    assert (
        client.get(f"/api/v1/sessions/{other}", headers=auth_header(case.token)).status_code == 200
    )


@pytest.mark.parametrize("cleanup", [False, True])
def test_original_success_receipt_preserves_legacy_delete(
    client: TestClient, cleanup: bool
) -> None:
    """
    真实原成功回执维持历史删除兼容且删除后仍可精确重放。

    :param client (TestClient): 隔离客户端
    :param cleanup (bool): 自动清理与显式停止分支
    """
    case, task = stop_case(client, cleanup=cleanup)
    result: dict[str, object] = {"status": "stopped", "session_id": case.session_id}
    assert report(case, task, "complete", result) == 200
    assert (
        client.delete(
            f"/api/v1/sessions/{case.session_id}", headers=auth_header(case.token)
        ).status_code
        == 200
    )
    assert report(case, task, "complete", result) == 200


def test_create_failure_without_stop_remains_deletable(client: TestClient) -> None:
    """
    没有停止任务的创建失败仍按既有历史规则删除。

    :param client (TestClient): 隔离客户端
    """
    case = start_case(client)
    assert report(case, case.task_id, "fail", {"code": "START_FAILED"}) == 200
    assert (
        client.delete(
            f"/api/v1/sessions/{case.session_id}", headers=auth_header(case.token)
        ).status_code
        == 200
    )
