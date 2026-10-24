"""
提供使用文件数据库和独立摘要算法的导入请求测试辅助函数。
"""

import base64
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import cast
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_tool_accounts_api import bootstrap, create_and_register_node, create_tool_account


def setup_import(client: TestClient) -> tuple[str, str]:
    """
    创建拥有可用节点的独立账户，不提前派发任何配置任务。

    :param client (TestClient): 隔离 API 客户端
    :return tuple[str, str]: 用户访问令牌和账户身份
    """
    token = bootstrap(client)
    create_and_register_node(client, token)
    account = create_tool_account(client, token)
    return token, str(account["id"])


def file_input(
    path: str = "~/.claude/settings.json", content: bytes = b"{}\n"
) -> dict[str, object]:
    """
    创建无真实凭据的测试配置正文。

    :param path (str): 规范配置路径
    :param content (bytes): 合成文件字节
    :return dict[str, object]: 提交文件结构
    """
    return {"path": path, "content_base64": base64.b64encode(content).decode("ascii"), "mode": 384}


def sign(account_id: str, payload: dict[str, object]) -> dict[str, object]:
    """
    直接依据公开协议独立计算摘要，不调用服务端实现。

    :param account_id (str): 规范账户身份
    :param payload (dict[str, object]): 完整待签意图
    :return dict[str, object]: 原地补充预期摘要后的请求
    """
    entries = []
    for item in cast(list[dict[str, object]], payload["files"]):
        content = base64.b64decode(cast(str, item["content_base64"]), validate=True)
        entries.append(
            {
                "path": item["path"],
                "mode": item["mode"],
                "size": len(content),
                "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
            }
        )
    value = {
        key: payload[key]
        for key in [
            "schema_version",
            "tool_type",
            "source",
            "include",
            "exclude",
            "include_resume_history",
            "dry_run",
        ]
    }
    value["tool_account_id"] = account_id
    value["files"] = sorted(entries, key=lambda entry: cast(str, entry["path"]))
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    payload["expected_intent_fingerprint"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return payload


def request_payload(account_id: str) -> dict[str, object]:
    """
    构造具有新调用方键的完整规范请求。

    :param account_id (str): 目标账户身份
    :return dict[str, object]: 独立计算摘要的完整请求
    """
    return sign(
        account_id,
        {
            "schema_version": 1,
            "request_id": str(uuid4()),
            "tool_type": "claude",
            "source": "local_cli",
            "include": ["~/.claude/settings.json"],
            "exclude": [],
            "include_resume_history": False,
            "dry_run": False,
            "files": [file_input()],
        },
    )


def endpoint(account_id: str) -> str:
    """
    精确构造新受理路由。

    :param account_id (str): 原始账户身份
    :return str: 强版本导入 URL 路径
    """
    return f"/api/v1/tool-accounts/{account_id}/config-import-requests"


def database_path(client: TestClient) -> Path:
    """
    从显式测试配置取得独立 SQLite 文件。

    :param client (TestClient): 当前隔离客户端
    :return Path: 测试数据库位置
    """
    app = cast(FastAPI, client.app)
    return Path(app.state.settings.database_url.removeprefix("sqlite+aiosqlite:///"))


def snapshot(client: TestClient) -> str:
    """
    记录所有 SQL 行以识别恢复查询或失败请求的意外写入。

    :param client (TestClient): 当前隔离客户端
    :return str: 确定顺序的测试数据库内容
    """
    with closing(sqlite3.connect(database_path(client))) as connection:
        return "\n".join(connection.iterdump())


def counts(client: TestClient) -> tuple[int, int, int]:
    """
    分别计数原始凭据、导入任务和导入派发审计。

    :param client (TestClient): 当前隔离客户端
    :return tuple[int, int, int]: 三种原子记录数量
    """
    with closing(sqlite3.connect(database_path(client))) as connection:
        receipts = connection.execute(
            "SELECT count(*) FROM tool_account_config_import_requests"
        ).fetchone()[0]
        tasks = connection.execute(
            "SELECT count(*) FROM node_tasks WHERE task_type='import_tool_account_config'"
        ).fetchone()[0]
        audits = connection.execute(
            "SELECT count(*) FROM audit_logs WHERE action='tool_accounts.config_import_start'"
        ).fetchone()[0]
        return receipts, tasks, audits
