"""
为测试提供只读空数据库模板和共享外部服务的串行调度约束。
"""

import os
from pathlib import Path

import pytest
from sqlalchemy import MetaData, create_engine

from agent_remote_server.db import Base


def isolated_metadata(source: MetaData) -> MetaData:
    """
    复制结构以清除其他数据库 DDL 留下的约束建表规则。

    :param source (MetaData): 原始模型结构
    :return MetaData: 不受先前建表顺序影响的独立结构
    """
    metadata = MetaData()
    for table in source.tables.values():
        table.to_metadata(metadata)
    return metadata


@pytest.fixture(scope="session")
def sqlite_schema(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """
    每个测试进程只创建一次空 SQLite 结构，测试仅使用独立副本。

    :param tmp_path_factory (pytest.TempPathFactory): 会话临时目录工厂
    :return Path: 已关闭所有连接的空数据库模板
    """
    path = tmp_path_factory.mktemp("schema") / "empty.db"
    engine = create_engine(f"sqlite:///{path}")
    # PostgreSQL 的 AddConstraint 会修改原约束的建表规则，模板必须复制元数据。
    metadata = isolated_metadata(Base.metadata)
    try:
        metadata.create_all(engine)
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
