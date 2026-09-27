"""
验证技能管理 HTTP 入口的身份、流式上传和版本规则闭环。
"""

import asyncio
import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_identity_api import auth_header, bootstrap, create_schema
from test_skill_storage import file_entry

from agent_remote_server.config import Settings
from agent_remote_server.main import create_app
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.skill_manager.manifest import manifest_digest


@pytest.fixture
def skill_client(tmp_path: Path) -> Iterator[TestClient]:
    """
    为 HTTP 验收开启独立存储卷和测试 API 功能开关。

    :param tmp_path (Path): 测试卷目录
    :return Iterator[TestClient]: 独立 API 客户端
    """
    settings = Settings(
        secret_key="test-secret",
        log_level="CRITICAL",
        database_url="sqlite+aiosqlite:///:memory:",
        skill_manager_enabled=True,
        skill_storage_root=tmp_path / "objects",
    )
    app = create_app(settings)
    asyncio.run(create_schema(app))
    with TestClient(app) as client:
        yield client


def test_skill_library_requires_user_authentication(skill_client: TestClient) -> None:
    """
    未登录请求不能读取用户库。

    :param skill_client (TestClient): 测试客户端
    """
    assert skill_client.get("/api/v1/skills").status_code == 401


def upload_package(client: TestClient, token: str, content: bytes) -> tuple[str, str, str]:
    """
    仅通过公开 HTTP 协议完成安装包流式上传。

    :param client (TestClient): 测试客户端
    :param token (str): 已登录用户令牌
    :param content (bytes): 实际说明文件
    :return tuple[str, str, str]: 上传、树和文件身份
    """
    entry = file_entry(content)
    manifest = SkillTreeManifest(entries=(entry,))
    headers = auth_header(token)
    response = client.post(
        "/api/v1/skills/content/uploads",
        headers=headers,
        json={"idempotency_key": str(uuid4()), "manifest": manifest.model_dump(mode="json")},
    )
    assert response.status_code == 200, response.text
    upload_id = response.json()["data"]["id"]
    premature = client.post(f"/api/v1/skills/content/uploads/{upload_id}/complete", headers=headers)
    assert (
        premature.status_code == 409
        and premature.json()["errors"][0]["code"] == "CONTENT_INCOMPLETE"
    )
    result = client.put(
        f"/api/v1/skills/content/uploads/{upload_id}/files/{entry.sha256}",
        headers=headers,
        content=iter([content[:3], content[3:]]),
    )
    assert result.status_code == 200, result.text
    complete = client.post(f"/api/v1/skills/content/uploads/{upload_id}/complete", headers=headers)
    assert complete.status_code == 200 and complete.json()["data"][
        "tree_digest"
    ] == manifest_digest(manifest)
    assert (
        client.post(f"/api/v1/skills/content/uploads/{upload_id}/complete", headers=headers).json()
        == complete.json()
    )
    return upload_id, manifest_digest(manifest), entry.sha256


def test_http_upload_install_rule_and_operation_roundtrip(skill_client: TestClient) -> None:
    """
    用户通过 HTTP 上传、安装、修改规则和找回受理结果，字节保持不变。

    :param skill_client (TestClient): 测试客户端
    """
    token = bootstrap(skill_client)
    headers = auth_header(token)
    content = b"---\nname: learning\ndescription: Memory\n---\nRemember useful facts.\n"
    upload_id, tree, digest = upload_package(skill_client, token, content)
    download = skill_client.get(
        f"/api/v1/skills/content/trees/{tree}/files/{digest}", headers=headers
    )
    assert download.status_code == 200 and download.content == content
    assert download.headers["etag"] == f'"{digest}"'
    assert (
        skill_client.get(f"/api/v1/skills/content/uploads/{upload_id}", headers=headers).json()[
            "status"
        ]
        == "committed"
    )
    request = {
        "idempotency_key": "install",
        "expected_generation": 0,
        "items": [
            {
                "name": "learning",
                "source": {"kind": "local", "locator": hashlib.sha256(b"local").hexdigest()},
                "provenance": {"ref_kind": "local"},
                "tree_digest": tree,
            }
        ],
    }
    installed = skill_client.post("/api/v1/skills/installations", headers=headers, json=request)
    assert installed.status_code == 200, installed.text
    data = installed.json()
    assert data["status"] == "stored" and data["committed"] and data["schema_version"] == 1
    assert (
        skill_client.post("/api/v1/skills/installations", headers=headers, json=request).json()
        == data
    )
    assert (
        skill_client.get(
            f"/api/v1/skills/operations/{data['operation_id']}", headers=headers
        ).json()
        == data
    )
    listing = skill_client.get("/api/v1/skills", headers=headers).json()["data"]
    assert listing["generation"] == 1 and listing["items"][0]["name"] == "learning"
    assert (
        skill_client.get(
            "/api/v1/skills/operations", params={"key": "install"}, headers=headers
        ).json()
        == data
    )
    disabled = skill_client.post(
        "/api/v1/skills/rules",
        headers=headers,
        json={
            "command": "disable",
            "skill": "learning",
            "idempotency_key": "off",
            "expected_generation": 1,
        },
    )
    assert disabled.status_code == 200 and disabled.json()["data"]["generation"] == 2
    info = skill_client.get(
        "/api/v1/skills/installations/learning?tool=claude", headers=headers
    ).json()["data"]
    assert not info["effective"]["enabled"] and info["effective"]["enabled_source"] == "user"
    assert not info["model_loaded"] and info["project_discovery"] == "not_inspected"
    stale = skill_client.post(
        "/api/v1/skills/rules",
        headers=headers,
        json={
            "command": "enable",
            "skill": "learning",
            "idempotency_key": "stale",
            "expected_generation": 1,
        },
    )
    assert stale.status_code == 409 and stale.json()["errors"][0]["code"] == "GENERATION_CONFLICT"


def test_other_user_cannot_read_package_or_upload_plan(skill_client: TestClient) -> None:
    """
    API 每个内容入口都按当前登录用户过滤，摘要相同也不例外。

    :param skill_client (TestClient): 测试客户端
    """
    token = bootstrap(skill_client)
    upload_id, tree, digest = upload_package(skill_client, token, b"Private instructions")
    created = skill_client.post(
        "/api/v1/users",
        headers=auth_header(token),
        json={
            "username": "other",
            "password": "other-secret",
            "display_name": "Other",
            "role": "user",
        },
    )
    assert created.status_code == 200
    login = skill_client.post(
        "/api/v1/auth/login", json={"username": "other", "password": "other-secret"}
    )
    headers = auth_header(login.json()["data"]["access_token"])
    for path in (
        f"/content/uploads/{upload_id}",
        f"/content/trees/{tree}",
        f"/content/trees/{tree}/files/{digest}",
    ):
        response = skill_client.get("/api/v1/skills" + path, headers=headers)
        assert response.status_code == 404 and not response.json()["committed"]
    assert (
        skill_client.post(
            f"/api/v1/skills/content/uploads/{upload_id}/complete", headers=headers
        ).status_code
        == 404
    )
    assert (
        skill_client.put(
            f"/api/v1/skills/content/uploads/{upload_id}/files/{digest}",
            headers=headers,
            content=b"bad",
        ).status_code
        == 404
    )


def test_device_token_and_disabled_feature_cannot_manage_skills(skill_client: TestClient) -> None:
    """
    已有设备权限不被提升为用户技能管理权限，发布开关也不能绕过。

    :param skill_client (TestClient): 测试客户端
    """
    token = bootstrap(skill_client)
    registered = skill_client.post(
        "/api/v1/devices/register",
        headers=auth_header(token),
        json={
            "name": "test-device",
            "platform": "linux",
            "cli_version": "0.0.5-fix.7",
            "ssh_public_key": "ssh-ed25519 AAAATESTKEY skill-test",
            "wireguard_public_key": "test-key",
        },
    )
    assert registered.status_code == 200, registered.text
    device_token = registered.json()["data"]["device_token"]["access_token"]
    denied = skill_client.get("/api/v1/skills", headers=auth_header(device_token))
    assert denied.status_code == 403 and denied.json()["errors"][0]["code"] == "USER_TOKEN_REQUIRED"
    app = cast(FastAPI, skill_client.app)
    app.state.settings.skill_manager_enabled = False
    disabled = skill_client.get("/api/v1/skills", headers=auth_header(token))
    assert (
        disabled.status_code == 503
        and disabled.json()["errors"][0]["code"] == "SKILL_MANAGER_DISABLED"
    )


def test_invalid_upload_and_unlisted_digest_never_publish(skill_client: TestClient) -> None:
    """
    错误内容和超额字节被有界接收器拒绝，清单外文件不获暂存权限。

    :param skill_client (TestClient): 测试客户端
    """
    token = bootstrap(skill_client)
    headers = auth_header(token)
    entry = file_entry(b"expected")
    manifest = SkillTreeManifest(entries=(entry,))
    response = skill_client.post(
        "/api/v1/skills/content/uploads",
        headers=headers,
        json={"idempotency_key": "upload", "manifest": manifest.model_dump(mode="json")},
    )
    upload_id = response.json()["data"]["id"]
    path = f"/api/v1/skills/content/uploads/{upload_id}/files/"
    denied = skill_client.put(path + "a" * 64, headers=headers, content=b"not declared")
    assert (
        denied.status_code == 409 and denied.json()["errors"][0]["code"] == "CONTENT_NOT_DECLARED"
    )
    for data in (b"wrong", b"exceeds-the-manifest-size"):
        rejected = skill_client.put(path + entry.sha256, headers=headers, content=data)
        assert (
            rejected.status_code == 422
            and rejected.json()["errors"][0]["code"] == "CONTENT_INVALID"
        )
    assert (
        skill_client.get(
            f"/api/v1/skills/content/trees/{manifest_digest(manifest)}", headers=headers
        ).status_code
        == 404
    )
    malformed = skill_client.post(
        "/api/v1/skills/content/uploads",
        headers=headers,
        json={"idempotency_key": "bad", "user_id": str(uuid4()), "manifest": {}},
    )
    assert malformed.status_code == 422


def test_account_delete_preserves_existing_guards_and_removes_only_its_rules(
    skill_client: TestClient,
) -> None:
    """
    符合原删除条件的账户可以清理自有覆盖，不删除另一账户规则或包内容。

    :param skill_client (TestClient): 测试客户端
    """
    token = bootstrap(skill_client)
    headers = auth_header(token)
    accounts = []
    for label in ("A", "B"):
        created = skill_client.post(
            "/api/v1/tool-accounts",
            headers=headers,
            json={
                "tool_type": "claude",
                "display_name": label,
                "region_code": "global",
                "timezone": "UTC",
                "locale": "en-US",
            },
        )
        assert created.status_code == 200
        accounts.append(created.json()["data"]["id"])
    _, tree, digest = upload_package(skill_client, token, b"Account instructions")
    installed = skill_client.post(
        "/api/v1/skills/installations",
        headers=headers,
        json={
            "idempotency_key": "add",
            "expected_generation": 0,
            "scope": {"account_id": accounts[0]},
            "items": [
                {
                    "name": "learning",
                    "source": {"kind": "local", "locator": "a" * 64},
                    "provenance": {"ref_kind": "local"},
                    "tree_digest": tree,
                }
            ],
        },
    )
    assert installed.status_code == 200
    assert installed.json()["data"]["targets"][0]["deploy_on_first_use"]
    enabled = skill_client.post(
        "/api/v1/skills/rules",
        headers=headers,
        json={
            "command": "enable",
            "skill": "learning",
            "idempotency_key": "b",
            "expected_generation": 1,
            "scope": {"account_id": accounts[1]},
        },
    )
    assert enabled.status_code == 200
    assert (
        skill_client.delete(f"/api/v1/tool-accounts/{accounts[0]}", headers=headers).status_code
        == 409
    )
    assert (
        skill_client.post(
            f"/api/v1/tool-accounts/{accounts[0]}/disable", headers=headers
        ).status_code
        == 200
    )
    assert (
        skill_client.delete(f"/api/v1/tool-accounts/{accounts[0]}", headers=headers).status_code
        == 200
    )
    info = skill_client.get("/api/v1/skills/installations/learning", headers=headers).json()["data"]
    assert set(info["account_overrides"]) == {accounts[1]}
    assert skill_client.get("/api/v1/skills", headers=headers).json()["data"]["generation"] == 3
    assert (
        skill_client.get(
            f"/api/v1/skills/content/trees/{tree}/files/{digest}", headers=headers
        ).status_code
        == 200
    )


def test_legacy_session_cannot_silently_omit_enabled_library_skills(
    skill_client: TestClient,
) -> None:
    """
    未支持技能协议的旧会话启动必须失败，已有 session 保留原身份和状态。

    :param skill_client (TestClient): 测试客户端
    """
    from test_sessions_api import create_account, create_node, create_workspace, register_device

    token = bootstrap(skill_client)
    headers = auth_header(token)
    create_node(skill_client, token, name="legacy-node", weight=10)
    device_id, device_token = register_device(skill_client, token)
    workspace_id = create_workspace(skill_client, device_token, device_id, "sha256:skills")
    account_id = create_account(skill_client, token)
    launch = {
        "tool_type": "claude",
        "tool_account_id": account_id,
        "workspace_id": workspace_id,
        "project_key": "sha256:skills",
        "argv": [],
    }
    old = skill_client.post("/api/v1/sessions", headers=headers, json=launch)
    assert old.status_code == 200
    _, tree, _ = upload_package(skill_client, token, b"Managed instructions")
    installed = skill_client.post(
        "/api/v1/skills/installations",
        headers=headers,
        json={
            "idempotency_key": "managed",
            "expected_generation": 0,
            "items": [
                {
                    "name": "learning",
                    "source": {"kind": "local", "locator": "a" * 64},
                    "provenance": {"ref_kind": "local"},
                    "tree_digest": tree,
                }
            ],
        },
    )
    assert installed.status_code == 200 and installed.json()["committed"]
    assert installed.json()["data"]["targets"][0]["readiness"] == "unsupported"
    rejected = skill_client.post("/api/v1/sessions", headers=headers, json=launch)
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "SKILL_MANAGER_UNSUPPORTED"
    existing = skill_client.get(f"/api/v1/sessions/{old.json()['data']['id']}", headers=headers)
    assert existing.status_code == 200 and existing.json()["data"]["status"] == "starting"
    disabled = skill_client.post(
        "/api/v1/skills/rules",
        headers=headers,
        json={
            "command": "disable",
            "skill": "learning",
            "all_scopes": True,
            "idempotency_key": "disable",
            "expected_generation": 1,
        },
    )
    assert disabled.status_code == 200
    assert skill_client.post("/api/v1/sessions", headers=headers, json=launch).status_code == 200
