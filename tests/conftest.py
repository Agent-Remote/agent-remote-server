"""
为测试提供只读空数据库模板和共享外部服务的串行调度约束。
"""

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from agent_remote_server.db import Base


@pytest.fixture(scope="session")
def sqlite_schema(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """
    每个测试进程只创建一次空 SQLite 结构，测试仅使用独立副本。

    :param tmp_path_factory (pytest.TempPathFactory): 会话临时目录工厂
    :return Path: 已关闭所有连接的空数据库模板
    """
    path = tmp_path_factory.mktemp("schema") / "empty.db"
    engine = create_engine(f"sqlite:///{path}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    return path


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """
    使用同一外部数据库或 Redis 的用例必须交给同一个工作进程。

    :param items (list[pytest.Item]): 已收集的测试用例
    """
    shared_modules = {
        "test_device_relay_store",
        "test_ego_browser_relay_database",
        "test_device_relay_revocation",
        "test_ego_browser_relay",
        "test_cli_session_postgres",
        "test_port_forward_integration",
        "test_ego_browser_concurrency",
    }
    for item in items:
        if os.environ.get("SKILL_TEST_DATABASE_URL") or item.path.stem in shared_modules:
            item.add_marker(pytest.mark.xdist_group("external_services"))
