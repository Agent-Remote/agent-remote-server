"""
验证固定集合删除原键、完整上下文回执和并发集合变化保护。
"""

import asyncio
import hashlib
import json
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from legacy_session_result_support import LegacySessionCase, start_case
from test_config_import_requests import client as client_fixture  # noqa: F401
from test_sessions_api import auth_header

from agent_remote_server.models import Session


@pytest.fixture
def client(client_fixture: TestClient) -> TestClient:  # noqa: F811
    """
    暴露本文件使用的隔离客户端。

    :param client_fixture (TestClient): 共享隔离客户端
    :return TestClient: 当前测试客户端
    """
    return client_fixture


def _digest(value: object) -> str:
    """
    计算服务端使用的规范 JSON 摘要。

    :param value (object): 已规范化的删除意图
    :return str: sha256 摘要
    """
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _payload(session_ids: list[str], *, request_id: str | None = None) -> dict[str, object]:
    """
    构造固定集合删除请求。

    :param session_ids (list[str]): 已排序的完整会话集合
    :param request_id (str | None): 可选调用方原键
    :return dict[str, object]: 删除请求 JSON
    """
    ids = sorted(session_ids)
    selection = {
        "tool_type": "claude",
        "statuses": ["failed", "interrupted", "stopped"],
        "project_key": None,
    }
    canonical = {
        "schema_version": 1,
        "tool_type": "claude",
        "session_ids": ids,
        "selection": selection,
    }
    return {
        **canonical,
        "request_id": request_id or str(uuid4()),
        "selection_digest": _digest(canonical),
        "intent_fingerprint": _digest({**canonical, "reason": "user_requested"}),
        "reason": "user_requested",
    }


async def _seed_terminal_sessions(case: LegacySessionCase, count: int) -> list[str]:
    """
    将原始会话置于可删除状态并增加同一用户的终态会话。

    :param case (LegacySessionCase): 旧协议测试上下文
    :param count (int): 新增终态会话数量
    :return list[str]: 完整会话 ID 集合
    """
    session_id = UUID(case.session_id)
    app = cast(FastAPI, case.client.app)
    async with app.state.session_factory() as session:
        original = await session.get(Session, session_id)
        assert original is not None
        original.status = "stopped"
        values = [original]
        for index in range(count):
            value = Session(
                tool_type=original.tool_type,
                user_id=original.user_id,
                tool_account_id=original.tool_account_id,
                workspace_id=original.workspace_id,
                node_id=original.node_id,
                project_key=f"delete-fixed-{index}",
                status="failed",
                tmux_session_name=f"delete-fixed-{index}",
                runtime_backend=original.runtime_backend,
            )
            session.add(value)
            values.append(value)
        await session.commit()
        return [str(value.id) for value in values]


def test_fixed_collection_delete_replays_receipt_with_context(
    client: TestClient,
) -> None:
    """
    删除只作用于用户确认集合，同键重放和删除后查询返回稳定回执。

    :param client (TestClient): 隔离客户端
    """
    case = start_case(client)
    session_ids = asyncio.run(_seed_terminal_sessions(case, 1))
    request = _payload(session_ids)
    first = client.post(
        "/api/v1/sessions/delete-requests",
        headers=auth_header(case.token),
        json=request,
    )
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert data["reason"] == "user_requested"
    assert data["session_ids"] == sorted(session_ids)
    assert data["deleted_session_ids"] == sorted(session_ids)
    assert data["session_deleted"] is True
    assert data["delete_task_record_id"]
    assert {item["session_id"] for item in data["session_contexts"]} == set(session_ids)
    for session_id in session_ids:
        assert (
            client.get(
                f"/api/v1/sessions/{session_id}", headers=auth_header(case.token)
            ).status_code
            == 404
        )
    replay = client.post(
        "/api/v1/sessions/delete-requests",
        headers=auth_header(case.token),
        json=request,
    )
    assert replay.status_code == 200
    assert replay.json()["data"] == data
    observed = client.get(
        f"/api/v1/sessions/delete-requests/{request['request_id']}",
        headers=auth_header(case.token),
    )
    assert observed.status_code == 200
    assert observed.json()["data"] == data


def test_fixed_collection_delete_rejects_concurrent_candidate_addition(
    client: TestClient,
) -> None:
    """
    用户确认后候选集合新增会话时返回 mismatch，不动态扩大删除范围。

    :param client (TestClient): 隔离客户端
    """
    case = start_case(client)
    session_ids = asyncio.run(_seed_terminal_sessions(case, 0))
    request = _payload(session_ids)
    asyncio.run(_seed_terminal_sessions(case, 1))
    rejected = client.post(
        "/api/v1/sessions/delete-requests",
        headers=auth_header(case.token),
        json=request,
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "SESSION_DELETE_COLLECTION_MISMATCH"
    assert rejected.json()["error"]["details"]["collection_mismatch"] is True
    assert (
        client.get(
            f"/api/v1/sessions/{session_ids[0]}", headers=auth_header(case.token)
        ).status_code
        == 200
    )
