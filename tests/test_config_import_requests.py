"""
验证严格配置导入的原子受理、精确恢复、并发互斥及无内容错误边界。
"""

import asyncio
import base64
import copy
import shutil
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from config_import_request_support import (
    counts,
    endpoint,
    request_payload,
    setup_import,
    sign,
    snapshot,
)
from fastapi.testclient import TestClient
from httpx2 import Response
from test_tool_accounts_api import auth_header, create_tool_account

from agent_remote_server.config import Settings
from agent_remote_server.main import create_app
from agent_remote_server.models import ToolAccountConfigImportRequest
from agent_remote_server.repositories.config_import_requests import ConfigImportRequestRepository


@pytest.fixture
def client(tmp_path: Path, sqlite_schema: Path) -> Iterator[TestClient]:
    """
    使用复制的独立文件数据库，真实并发请求不能共享一个 SQLite 连接。

    :param tmp_path (Path): 当前测试临时目录
    :param sqlite_schema (Path): 已复制元数据创建的空数据库模板
    :return Iterator[TestClient]: 隔离测试客户端
    """
    path = tmp_path / "imports.sqlite"
    shutil.copyfile(sqlite_schema, path)
    settings = Settings(
        secret_key="test-secret", log_level="CRITICAL", database_url=f"sqlite+aiosqlite:///{path}"
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_accept_recover_replay_do_not_follow_latest_or_duplicate_tasks(client: TestClient) -> None:
    """
    丢失响应可恢复原任务，新请求成为最近任务后也不能替换原凭据。

    :param client (TestClient): 当前隔离客户端
    """
    token, account = setup_import(client)
    payload = request_payload(account)
    response = client.post(endpoint(account), headers=auth_header(token), json=payload)
    assert response.status_code == 201, response.text
    receipt = response.json()["data"]
    assert receipt["intent_fingerprint"] == payload["expected_intent_fingerprint"]
    assert receipt["accepted"] == payload["include"] and receipt["rejected"] == []
    assert receipt["file_count"] == len(receipt["manifest"]) == 1
    assert receipt["task_id"].startswith(f"import_tool_account_config:{account}:")
    assert "content_base64" not in response.text
    second = request_payload(account)
    assert (
        client.post(endpoint(account), headers=auth_header(token), json=second).status_code == 201
    )
    before = snapshot(client)
    recovered = client.get(
        endpoint(account) + f"/{payload['request_id']}", headers=auth_header(token)
    )
    assert recovered.status_code == 200 and recovered.json()["data"] == receipt
    replayed = client.post(endpoint(account), headers=auth_header(token), json=payload)
    assert replayed.status_code == 200 and replayed.json()["data"] == receipt
    assert snapshot(client) == before
    assert counts(client) == (2, 2, 2)
    status = client.get(
        f"/api/v1/tool-accounts/{account}/config-imports/{receipt['task_id']}",
        headers=auth_header(token),
    )
    assert status.status_code == 200 and status.json()["data"]["status"] == "pending"


def test_same_key_different_intent_or_account_conflicts_without_writes(client: TestClient) -> None:
    """
    同一用户键绑定全部内容与原账户，修改模式、路径顺序或目标都不能重用。

    :param client (TestClient): 当前隔离客户端
    """
    token, account = setup_import(client)
    other = str(create_tool_account(client, token, name="Other")["id"])
    payload = request_payload(account)
    assert (
        client.post(endpoint(account), headers=auth_header(token), json=payload).status_code == 201
    )
    changed = copy.deepcopy(payload)
    cast(list[dict[str, object]], changed["files"])[0]["mode"] = 0o640
    sign(account, changed)
    before = snapshot(client)
    for target, body in [(account, changed), (other, sign(other, copy.deepcopy(payload)))]:
        response = client.post(endpoint(target), headers=auth_header(token), json=body)
        assert (
            response.status_code == 409
            and response.json()["error"]["code"] == "CONFIG_IMPORT_REQUEST_CONFLICT"
        )
    assert snapshot(client) == before
    assert (
        client.get(
            endpoint(other) + f"/{payload['request_id']}", headers=auth_header(token)
        ).status_code
        == 404
    )
    unknown = str(uuid4())
    assert (
        client.get(endpoint(account) + f"/{unknown}", headers=auth_header(token)).status_code == 404
    )
    assert snapshot(client) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("schema_version", "1"),
        ("dry_run", 0),
        ("dry_run", True),
        ("include_resume_history", 0),
        ("request_id", None),
        ("request_id", "not-a-uuid"),
        ("request_id", "A" * 36),
        ("include", None),
        ("files", []),
        ("source", "unknown"),
        ("expected_intent_fingerprint", None),
        ("schema_version", 2),
    ],
)
def test_strict_fields_reject_before_any_journal_or_task(
    client: TestClient, field: str, value: object
) -> None:
    """
    版本和标志不能宽松转换，空值及缺失文件不能产生半完成接受。

    :param client (TestClient): 当前隔离客户端
    :param field (str): 被替换的字段
    :param value (object): 不符合协议的合成值
    """
    token, account = setup_import(client)
    payload = request_payload(account)
    payload[field] = value
    before = snapshot(client)
    response = client.post(endpoint(account), headers=auth_header(token), json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "COMMON_VALIDATION_ERROR"
    assert snapshot(client) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "missing",
        "mode_bool",
        "mode_string",
        "bad_content",
        "content_object",
        "duplicate",
        "noncanonical",
        "rejected",
        "wrong_digest",
    ],
)
def test_invalid_inputs_are_content_free_and_atomic(
    client: TestClient, mutation: str, caplog: pytest.LogCaptureFixture
) -> None:
    """
    非法正文及多余字段不能通过框架错误、日志或审计回显合成秘密。

    :param client (TestClient): 当前隔离客户端
    :param mutation (str): 当前破坏协议的方式
    :param caplog (pytest.LogCaptureFixture): 捕获测试日志
    """
    token, account = setup_import(client)
    secret = "synthetic-config-import-secret-never-echo"
    encoded = base64.b64encode(secret.encode()).decode()
    payload = request_payload(account)
    files = cast(list[dict[str, object]], payload["files"])
    files[0]["content_base64"] = encoded
    sign(account, payload)
    if mutation == "unknown":
        payload["unknown_secret"] = secret
    elif mutation == "missing":
        del payload["include_resume_history"]
    elif mutation == "mode_bool":
        files[0]["mode"] = True
    elif mutation == "mode_string":
        files[0]["mode"] = "384"
    elif mutation == "bad_content":
        files[0]["content_base64"] = secret
    elif mutation == "content_object":
        files[0]["content_base64"] = {"secret": secret}
    elif mutation == "duplicate":
        files.append(dict(files[0]))
        sign(account, payload)
    elif mutation == "noncanonical":
        files[0]["path"] = "~/.claude/../settings.json"
    elif mutation == "rejected":
        payload["include"] = ["~/.claude/settings.json", "~/.claude.json"]
        sign(account, payload)
    else:
        payload["expected_intent_fingerprint"] = "sha256:" + "0" * 64
    before = snapshot(client)
    response = client.post(endpoint(account), headers=auth_header(token), json=payload)
    assert response.status_code == 422, response.text
    assert secret not in response.text and encoded not in response.text
    assert secret not in caplog.text and encoded not in caplog.text
    assert snapshot(client) == before
    assert counts(client) == (0, 0, 0)


def test_receipt_persistence_failure_rolls_back_task_profile_and_audit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    在任务已入事务但凭据未保存时故障，不能提前提交任何节点任务。

    :param client (TestClient): 当前隔离客户端
    :param monkeypatch (pytest.MonkeyPatch): 注入确定性持久化故障
    """
    token, account = setup_import(client)
    payload = request_payload(account)
    before = snapshot(client)

    async def fail(
        self: ConfigImportRequestRepository, record: ToolAccountConfigImportRequest
    ) -> None:
        """
        确认任务已暂存后在保存凭据的位置失败。

        :param record (ToolAccountConfigImportRequest): 即将发布的无内容凭据
        """
        assert await self.task(record.task_id) is not None
        raise RuntimeError("injected receipt persistence fault")

    with monkeypatch.context() as patch:
        patch.setattr(ConfigImportRequestRepository, "add", fail)
        with pytest.raises(RuntimeError, match="injected receipt persistence fault"):
            client.post(endpoint(account), headers=auth_header(token), json=payload)
    assert snapshot(client) == before and counts(client) == (0, 0, 0)
    assert (
        client.get(
            endpoint(account) + f"/{payload['request_id']}", headers=auth_header(token)
        ).status_code
        == 404
    )
    assert (
        client.post(endpoint(account), headers=auth_header(token), json=payload).status_code == 201
    )
    assert counts(client) == (1, 1, 1)


@pytest.mark.parametrize("different", [False, True])
def test_concurrent_exact_key_creates_one_task_receipt_and_audit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, different: bool
) -> None:
    """
    两请求都观察到键尚未受理后并发提交，仍只能原子发布一个任务及审计。

    :param client (TestClient): 当前隔离客户端
    :param monkeypatch (pytest.MonkeyPatch): 在数据库初次读取后同步两个请求
    :param different (bool): 第二个请求是否携带不同的有效意图
    """
    token, account = setup_import(client)
    payload = request_payload(account)
    other = copy.deepcopy(payload)
    if different:
        cast(list[dict[str, object]], other["files"])[0]["mode"] = 0o640
        sign(account, other)
    start = threading.Barrier(2, timeout=10)
    initial_reads = asyncio.Barrier(2)
    original = ConfigImportRequestRepository.request
    calls = 0

    async def synchronized_read(
        self: ConfigImportRequestRepository, user_id: UUID, request_id: UUID
    ) -> ToolAccountConfigImportRequest | None:
        """
        让两次初始查询都看到不存在，避免用串行重放冒充并发证据。

        :param user_id (UUID): 本次请求用户
        :param request_id (UUID): 原调用方键
        :return ToolAccountConfigImportRequest | None: 原数据库读取结果
        """
        nonlocal calls
        record = await original(self, user_id, request_id)
        calls += 1
        if calls <= 2:
            assert record is None
            await asyncio.wait_for(initial_reads.wait(), timeout=10)
        return record

    monkeypatch.setattr(ConfigImportRequestRepository, "request", synchronized_read)

    def submit(body: dict[str, object]) -> Response:
        """
        同步到同一边界后争抢原始调用方键。

        :param body (dict[str, object]): 独立请求正文
        :return Response: 真实 HTTP 接受、精确重放或冲突响应
        """
        start.wait()
        return client.post(endpoint(account), headers=auth_header(token), json=body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, payload), pool.submit(submit, other)]
        responses = [future.result(timeout=30) for future in futures]
    assert sorted(response.status_code for response in responses) == (
        [201, 409] if different else [200, 201]
    )
    if not different:
        assert responses[0].json()["data"] == responses[1].json()["data"]
    assert counts(client) == (1, 1, 1)


def test_recovery_authorizes_owner_and_is_readonly_even_for_stale_device(
    client: TestClient,
) -> None:
    """
    恢复查询不触碰过期活动时间，且撤销设备、其他用户和账户归属漂移均重新拒绝。

    :param client (TestClient): 当前隔离客户端
    """
    import sqlite3

    from config_import_request_support import database_path

    token, account = setup_import(client)
    payload = request_payload(account)
    accepted = client.post(endpoint(account), headers=auth_header(token), json=payload)
    assert accepted.status_code == 201
    registration = client.post(
        "/api/v1/devices/register",
        headers=auth_header(token),
        json={
            "name": "import-recovery",
            "platform": "linux",
            "cli_version": "test",
            "ssh_public_key": "ssh-ed25519 AAAAIMPORTFIXTURE test@example.invalid",
        },
    )
    assert registration.status_code == 200
    device_token = registration.json()["data"]["device_token"]["access_token"]
    device_id = registration.json()["data"]["device"]["id"].replace("-", "")
    with closing(sqlite3.connect(database_path(client))) as connection, connection:
        connection.execute(
            "UPDATE user_devices SET last_seen_at='2000-01-01 00:00:00' WHERE id=?", (device_id,)
        )
    before = snapshot(client)
    recovered = client.get(
        endpoint(account) + f"/{payload['request_id']}", headers=auth_header(device_token)
    )
    assert recovered.status_code == 200 and recovered.json()["data"] == accepted.json()["data"]
    assert snapshot(client) == before
    task_id = accepted.json()["data"]["task_id"]
    status = client.get(
        f"/api/v1/tool-accounts/{account}/config-imports/{task_id}",
        headers=auth_header(device_token),
    )
    assert status.status_code == 200
    assert snapshot(client) == before
    with closing(sqlite3.connect(database_path(client))) as connection, connection:
        connection.execute("UPDATE user_devices SET status='revoked' WHERE id=?", (device_id,))
    before = snapshot(client)
    assert (
        client.get(
            endpoint(account) + f"/{payload['request_id']}", headers=auth_header(device_token)
        ).status_code
        == 403
    )
    assert snapshot(client) == before
    new_user = client.post(
        "/api/v1/users",
        headers=auth_header(token),
        json={
            "username": "other-import-user",
            "display_name": "Other",
            "password": "synthetic-test-password",
            "role": "user",
        },
    )
    assert new_user.status_code == 200
    other_id = new_user.json()["data"]["id"]
    other_token = client.post(
        "/api/v1/auth/login",
        json={"username": "other-import-user", "password": "synthetic-test-password"},
    ).json()["data"]["access_token"]
    before = snapshot(client)
    assert (
        client.get(
            endpoint(account) + f"/{payload['request_id']}", headers=auth_header(other_token)
        ).status_code
        == 404
    )
    assert (
        client.post(endpoint(account), headers=auth_header(other_token), json=payload).status_code
        == 404
    )
    assert snapshot(client) == before
    with closing(sqlite3.connect(database_path(client))) as connection, connection:
        connection.execute(
            "UPDATE tool_accounts SET user_id=? WHERE id=?",
            (other_id.replace("-", ""), account.replace("-", "")),
        )
    before = snapshot(client)
    assert (
        client.get(
            endpoint(account) + f"/{payload['request_id']}", headers=auth_header(token)
        ).status_code
        == 404
    )
    assert (
        client.post(endpoint(account), headers=auth_header(token), json=payload).status_code == 404
    )
    assert snapshot(client) == before


def test_receipt_and_audit_do_not_duplicate_configuration_contents(client: TestClient) -> None:
    """
    原正文只进入已有任务执行载荷，额外日志表和受理清单仅存内容身份。

    :param client (TestClient): 当前隔离客户端
    """
    import json
    import sqlite3

    from config_import_request_support import database_path, file_input

    token, account = setup_import(client)
    secret = b"synthetic-content-private-not-journaled"
    payload = request_payload(account)
    payload["files"] = [file_input(content=secret)]
    sign(account, payload)
    response = client.post(endpoint(account), headers=auth_header(token), json=payload)
    assert response.status_code == 201
    encoded = base64.b64encode(secret).decode()
    with closing(sqlite3.connect(database_path(client))) as connection, connection:
        receipt = connection.execute(
            "SELECT receipt_json FROM tool_account_config_import_requests"
        ).fetchone()[0]
        audit = str(connection.execute("SELECT details FROM audit_logs").fetchall())
        task = json.loads(
            connection.execute(
                "SELECT payload FROM node_tasks WHERE task_type='import_tool_account_config'"
            ).fetchone()[0]
        )
    assert encoded not in receipt and secret.decode() not in receipt
    assert encoded not in audit and secret.decode() not in audit
    assert encoded not in response.text and secret.decode() not in response.text
    assert task["files"][0]["content_base64"] == encoded


@pytest.mark.parametrize(
    "limit", ["path", "include", "exclude", "files", "encoded", "decoded", "total", "mode"]
)
def test_request_resource_limits_reject_without_mutation(client: TestClient, limit: str) -> None:
    """
    请求和文件的显式上限必须在发布任何受理或节点任务之前生效。

    :param client (TestClient): 当前隔离客户端
    :param limit (str): 当前需要验证的边界
    """
    from config_import_request_support import file_input

    token, account = setup_import(client)
    payload = request_payload(account)
    if limit in {"include", "exclude"}:
        payload[limit] = ["~/.claude/agents"] * 4097
    elif limit == "files":
        payload["files"] = [file_input(content=b"")] * 4097
    elif limit == "path":
        payload["include"] = ["~/" + "a" * 2047]
    elif limit == "encoded":
        payload["files"] = [
            {"path": "~/.claude/settings.json", "mode": 384, "content_base64": "A" * 1398105}
        ]
    elif limit == "mode":
        cast(list[dict[str, object]], payload["files"])[0]["mode"] = 0o4755
    elif limit == "decoded":
        payload["files"] = [file_input(content=b"a" * (1024 * 1024 + 1))]
        sign(account, payload)
    else:
        payload["include"] = ["~/.claude/agents"]
        payload["files"] = [
            file_input(f"~/.claude/agents/{index}.md", b"a" * (1024 * 1024)) for index in range(9)
        ]
        sign(account, payload)
    before = snapshot(client)
    assert (
        client.post(endpoint(account), headers=auth_header(token), json=payload).status_code == 422
    )
    assert snapshot(client) == before


def test_warnings_are_fixed_codes_and_rejected_selection_never_partially_imports(
    client: TestClient,
) -> None:
    """
    警告仅使用固定协议码，混入被拒绝路径不能静默导入其余文件。

    :param client (TestClient): 当前隔离客户端
    """
    from config_import_request_support import file_input

    token, account = setup_import(client)
    payload = request_payload(account)
    payload["include"] = ["~/.claude/hooks/test.sh", "~/.claude/custom.md"]
    payload["files"] = [
        file_input("~/.claude/hooks/test.sh", b"fixture"),
        file_input("~/.claude/custom.md", b"fixture"),
    ]
    sign(account, payload)
    response = client.post(endpoint(account), headers=auth_header(token), json=payload)
    assert response.status_code == 201
    assert response.json()["data"]["warnings"] == [
        "CONFIG_IMPORT_EXECUTABLE_REVIEW_REQUIRED",
        "CONFIG_IMPORT_CUSTOM_MARKDOWN_REVIEW_REQUIRED",
    ]
    blocked = copy.deepcopy(payload)
    blocked["request_id"] = str(uuid4())
    blocked["exclude"] = ["~/.claude/custom.md"]
    sign(account, blocked)
    before = snapshot(client)
    assert (
        client.post(endpoint(account), headers=auth_header(token), json=blocked).status_code == 422
    )
    assert snapshot(client) == before


def test_chunked_request_is_bounded_before_json_parsing(client: TestClient) -> None:
    """
    没有长度头的请求流也必须在 JSON 解析前停止，不能回显合成正文。

    :param client (TestClient): 当前隔离客户端
    """
    from agent_remote_server.api.config_import_requests import MAX_IMPORT_REQUEST_BYTES

    token, account = setup_import(client)
    before = snapshot(client)
    marker = b"synthetic-chunked-import-never-echo"

    def chunks() -> Iterator[bytes]:
        """
        逐块超出 HTTP 层的总量上限，不提供完整内存正文。

        :return Iterator[bytes]: 有界单块组成的超量请求流
        """
        yield b'{"files":[{"content_base64":"' + marker
        for _ in range(MAX_IMPORT_REQUEST_BYTES // (64 * 1024) + 1):
            yield b"a" * (64 * 1024)
        yield b'"}]}'

    response = client.post(
        endpoint(account),
        headers={**auth_header(token), "Content-Type": "application/json"},
        content=chunks(),
    )
    assert "content-length" not in response.request.headers
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "CONFIG_IMPORT_TOO_LARGE"
    assert marker.decode() not in response.text
    assert snapshot(client) == before
