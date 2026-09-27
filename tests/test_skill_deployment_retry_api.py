"""
验证公开重试入口提交精确尝试并通过独立只读回执恢复。
"""

import asyncio
from typing import cast
from uuid import UUID, uuid4

import pytest
from deployment_attempt_support import observe
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_identity_api import auth_header, bootstrap
from test_skill_api import skill_client as skill_client
from test_skill_deployment_attempts import failed, history
from test_skill_library import LibraryHarness

from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.services.skills.retention.clocks import retention_mutation


def harness(client: TestClient, token: str) -> LibraryHarness:
    """
    共用 HTTP 应用的真实数据库和用户，构造已有失败观察。

    :param client (TestClient): 独立 HTTP 应用
    :param token (str): 原用户凭据
    :return LibraryHarness: 同用户持久库
    """
    app = cast(FastAPI, client.app)
    owner = UUID(client.get("/api/v1/users/me", headers=auth_header(token)).json()["data"]["id"])
    return LibraryHarness(
        app.state.session_factory, app.state.settings.skill_storage_root.parent, owner
    )


def test_retry_http_commit_recovery_and_later_success(skill_client: TestClient) -> None:
    """
    响应丢失后按重试键恢复，后来成功也不追加第三次尝试。

    :param skill_client (TestClient): 认证 HTTP 客户端
    """
    token = bootstrap(skill_client)
    library = harness(skill_client, token)
    operation_id, account, request = asyncio.run(failed(library))
    path = f"/api/v1/skills/operations/{operation_id}/retries"
    headers = auth_header(token)
    query = {"key": request.idempotency_key}
    missing = skill_client.get(path, headers=headers, params=query)
    assert missing.status_code == 404
    assert len(asyncio.run(history(library, operation_id))) == 1
    response = skill_client.post(path, headers=headers, json=request.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["operation_id"] == str(operation_id)
    assert result["status"] == "preparing" and result["committed"]
    assert result["data"]["generation"] == request.expected_generation
    assert result["data"]["targets"][0]["attempt_number"] == 2
    assert skill_client.get(path, headers=headers, params=query).json() == result
    assert (
        skill_client.post(path, headers=headers, json=request.model_dump(mode="json")).json()
        == result
    )
    assert len(asyncio.run(history(library, operation_id))) == 2
    changed = request.model_dump(mode="json") | {"expected_generation": 999}
    rejected = skill_client.post(path, headers=headers, json=changed)
    assert rejected.status_code == 409
    assert rejected.json()["errors"][0]["code"] == "IDEMPOTENCY_CONFLICT"

    async def succeed() -> None:
        """
        提交独立执行结果，检查旧回执不会再次提交。
        """
        async with library.database.begin() as session, retention_mutation(session, library.owner):
            operation = await session.get(SkillOperation, operation_id)
            assert operation is not None
            await observe(session, operation, "ready", account_id=account)

    asyncio.run(succeed())
    assert skill_client.get(path, headers=headers, params=query).json()["status"] == "ready"
    assert (
        skill_client.post(path, headers=headers, json=request.model_dump(mode="json")).json()[
            "status"
        ]
        == "ready"
    )
    assert len(asyncio.run(history(library, operation_id))) == 2
    assert (
        skill_client.get("/api/v1/skills/operations", headers=headers, params=query).status_code
        == 404
    )
    other = f"/api/v1/skills/operations/{uuid4()}/retries"
    assert skill_client.get(other, headers=headers, params=query).status_code == 404


@pytest.mark.parametrize("boundary", ["anonymous", "other_user", "device", "disabled"])
def test_retry_http_authority_is_checked_before_mutation(
    skill_client: TestClient, boundary: str
) -> None:
    """
    查询与提交都遵守活跃用户和功能开关，拒绝时不追加记录。

    :param skill_client (TestClient): 独立应用
    :param boundary (str): 要破坏的授权边界
    """
    token = bootstrap(skill_client)
    library = harness(skill_client, token)
    operation_id, _, request = asyncio.run(failed(library))
    headers = auth_header(token)
    expected = 401
    if boundary == "anonymous":
        headers = {}
    elif boundary == "other_user":
        skill_client.post(
            "/api/v1/users",
            headers=headers,
            json={
                "username": "other",
                "password": "other-secret",
                "display_name": "Other",
                "role": "user",
            },
        ).raise_for_status()
        response = skill_client.post(
            "/api/v1/auth/login", json={"username": "other", "password": "other-secret"}
        )
        headers = auth_header(response.json()["data"]["access_token"])
        expected = 404
    elif boundary == "device":
        from sqlalchemy import select

        from agent_remote_server.models import AuthToken

        async def device_token() -> None:
            """
            只修改测试凭据种类，验证技能依赖拒绝设备身份。
            """
            async with library.database.begin() as session:
                saved = await session.scalar(
                    select(AuthToken).where(AuthToken.user_id == library.owner)
                )
                assert saved is not None
                saved.token_type = "device"

        asyncio.run(device_token())
        expected = 403
    else:
        cast(FastAPI, skill_client.app).state.settings.skill_manager_enabled = False
        expected = 503
    path = f"/api/v1/skills/operations/{operation_id}/retries"
    assert (
        skill_client.post(path, headers=headers, json=request.model_dump(mode="json")).status_code
        == expected
    )
    assert (
        skill_client.get(path, headers=headers, params={"key": request.idempotency_key}).status_code
        == expected
    )
    assert len(asyncio.run(history(library, operation_id))) == 1


def test_retry_http_rejects_incomplete_selection_and_extra_inputs(skill_client: TestClient) -> None:
    """
    重复、过期或夹带来源的请求原子拒绝，不能把库键当作重试回执。

    :param skill_client (TestClient): 独立应用
    """
    token = bootstrap(skill_client)
    library = harness(skill_client, token)
    operation_id, _, request = asyncio.run(failed(library))
    path = f"/api/v1/skills/operations/{operation_id}/retries"
    headers = auth_header(token)
    original = request.model_dump(mode="json")
    for changed in (
        original | {"targets": []},
        original | {"targets": original["targets"] * 2},
        original | {"source": "https://example.test/changed"},
    ):
        assert skill_client.post(path, headers=headers, json=changed).status_code == 422
    changed = original | {
        "targets": [*original["targets"], {"account_id": str(uuid4()), "attempt_id": str(uuid4())}]
    }
    assert skill_client.post(path, headers=headers, json=changed).status_code == 409
    assert len(asyncio.run(history(library, operation_id))) == 1
    assert (
        skill_client.get(path, headers=headers, params={"key": request.idempotency_key}).status_code
        == 404
    )
