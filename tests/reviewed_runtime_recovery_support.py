"""
通过真实迁移入口构造审核请求，使用数据库快照检测只读边界。
"""

import sqlite3
from contextlib import closing
from typing import cast
from uuid import uuid4

from config_import_request_support import database_path
from fastapi.testclient import TestClient
from runtime_migration_support import RuntimeMigrationCase, begin_migration
from test_runtime_migration_explicit_recovery import fail_original
from test_tool_accounts_api import auth_header


def endpoint(case: RuntimeMigrationCase) -> str:
    """
    返回精确账户的新版恢复请求入口。

    :param case (RuntimeMigrationCase): 原迁移上下文
    :return str: 请求入口
    """
    return f"/api/v1/tool-accounts/{case.account_id}/runtime-migration/recovery-requests"


def inspect_url(case: RuntimeMigrationCase) -> str:
    """
    返回显式原任务安全观察入口。

    :param case (RuntimeMigrationCase): 原迁移上下文
    :return str: 观察入口
    """
    return f"/api/v1/tool-accounts/{case.account_id}/runtime-migration/recovery-inspection"


def reviewed_case(
    client: TestClient, action: str = "verify_target"
) -> tuple[RuntimeMigrationCase, dict[str, object]]:
    """
    通过真实失败任务及观察接口建立客户端确认内容。

    :param client (TestClient): 隔离客户端
    :param action (str): 明确恢复动作
    :return tuple[RuntimeMigrationCase, dict[str, object]]: 原任务及完整审核请求
    """
    case = begin_migration(client)
    fail_original(case)
    response = client.get(
        inspect_url(case),
        headers=auth_header(case.token),
        params={"original_task_id": case.task_id},
    )
    assert response.status_code == 200, response.text
    return case, {
        "schema_version": 1,
        "request_id": str(uuid4()),
        "action": action,
        "expected_original": cast(dict[str, object], response.json()["data"]["original"]),
    }


def change_rows(client: TestClient, statement: str, values: tuple[object, ...] = ()) -> None:
    """
    通过独立连接制造认证或保存状态变化。

    :param client (TestClient): 文件数据库客户端
    :param statement (str): 固定测试更新语句
    :param values (tuple[object, ...]): 合成参数
    """
    with closing(sqlite3.connect(database_path(client))) as connection, connection:
        connection.execute(statement, values)
