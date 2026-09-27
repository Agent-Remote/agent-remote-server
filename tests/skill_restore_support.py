"""
为协调恢复验收持有独立 PostgreSQL 容器、完整数据库归档及私有内容副本。
"""

import hashlib
import os
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Text, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_remote_server.db import Base


def run_fixture_command(
    arguments: list[str], *, environment: dict[str, str] | None = None
) -> bytes:
    """
    有界运行本测试固定命令，失败不把命令输出或测试内容复制到普通日志。

    :param arguments (list[str]): 不含用户凭据的固定工具参数
    :param environment (dict[str, str] | None): 可选子进程环境
    :return bytes: 调用方消费的命令输出
    """
    result = subprocess.run(arguments, env=environment, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError(
            f"restore fixture {Path(arguments[0]).name} failed ({result.returncode})"
        )
    return result.stdout


@dataclass(frozen=True)
class RestoreEnvironment:
    """
    只操作本次随机命名的数据库容器及测试目录。
    """

    container: str
    port: int
    root: Path

    def url(self, database: str) -> str:
        """
        返回隔离测试数据库地址。

        :param database (str): 本夹具固定数据库名
        :return str: 仅绑定回环地址的连接 URL
        """
        assert database in {"restore_source", "restore_target"}
        return f"postgresql+asyncpg://skill_test@127.0.0.1:{self.port}/{database}"

    def restore(self, source: Path, destination: Path) -> None:
        """
        在全部调用方事务关闭后备份两侧，销毁原测试数据库再恢复到新的空目标。

        :param source (Path): 本测试生成的原内容目录
        :param destination (Path): 新的空内容目录
        """
        assert source.parent == self.root and destination.parent == self.root
        archive = self.root / "database.pgdump"
        data = run_fixture_command(
            [
                "docker",
                "exec",
                self.container,
                "pg_dump",
                "-U",
                "skill_test",
                "-d",
                "restore_source",
                "-Fc",
            ]
        )
        archive.write_bytes(data)
        archive.chmod(0o600)
        run_fixture_command(["tar", "-C", str(source), "-cpf", str(self.root / "content.tar"), "."])
        (self.root / "content.tar").chmod(0o600)
        run_fixture_command(
            ["docker", "exec", self.container, "dropdb", "-U", "skill_test", "restore_source"]
        )
        source.rename(self.root / "source-unavailable")
        run_fixture_command(
            ["docker", "exec", self.container, "createdb", "-U", "skill_test", "restore_target"]
        )
        with archive.open("rb") as dump:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    self.container,
                    "pg_restore",
                    "-U",
                    "skill_test",
                    "-d",
                    "restore_target",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-acl",
                ],
                stdin=dump,
                capture_output=True,
                timeout=180,
            )
        if result.returncode:
            raise RuntimeError("isolated database restore failed")
        destination.mkdir(mode=0o700)
        run_fixture_command(["tar", "-C", str(destination), "-xpf", str(self.root / "content.tar")])


@pytest.fixture
def restore_environment(tmp_path: Path) -> Iterator[RestoreEnvironment]:
    """
    显式选择后才创建一次性容器，绝不接受已有数据库作为恢复目标。

    :param tmp_path (Path): 本次测试拥有的私有目录
    :return Iterator[RestoreEnvironment]: 生命周期受夹具管理的恢复环境
    """
    if os.environ.get("AGENT_REMOTE_RUN_SKILL_RESTORE_TEST") != "1":
        pytest.skip("requires opted-in disposable PostgreSQL and Linux Node materialization")
    container = f"skill-coordinated-restore-{uuid4().hex[:12]}"
    run_fixture_command(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            container,
            "--publish",
            "127.0.0.1::5432",
            "--env",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "--env",
            "POSTGRES_USER=skill_test",
            "--env",
            "POSTGRES_DB=restore_source",
            "postgres:16-alpine",
        ]
    )
    try:
        port = int(
            run_fixture_command(["docker", "port", container, "5432/tcp"])
            .decode()
            .strip()
            .rsplit(":", 1)[1]
        )
        environment = RestoreEnvironment(container, port, tmp_path)
        for _ in range(100):
            result = subprocess.run(
                ["docker", "exec", container, "pg_isready", "-U", "skill_test"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("isolated PostgreSQL did not become ready")
        run_fixture_command(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            environment={
                **os.environ,
                "DATABASE_URL": environment.url("restore_source"),
                "AGENT_REMOTE_SECRET_KEY": "isolated-restore-fixture",
            },
        )
        yield environment
    finally:
        subprocess.run(
            ["docker", "rm", "--force", container], capture_output=True, timeout=30, check=True
        )


async def database_fingerprint(
    database: async_sessionmaker[AsyncSession],
) -> dict[str, tuple[int, str]]:
    """
    对每张实际业务表的完整行排序并计算摘要，避免仅用行数证明引用恢复。

    :param database (async_sessionmaker[AsyncSession]): 无后台写入的独立事务工厂
    :return dict[str, tuple[int, str]]: 表名、完整行数及稳定摘要
    """
    result: dict[str, tuple[int, str]] = {}
    async with database() as session:
        for name, table in sorted(Base.metadata.tables.items()):
            row = func.to_jsonb(table.table_valued()).cast(Text)
            values = list(await session.scalars(select(row).order_by(row)))
            digest = hashlib.sha256()
            for value in values:
                encoded = value.encode()
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
            result[name] = (len(values), digest.hexdigest())
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        assert isinstance(revision, str)
        result["alembic_version"] = (1, revision)
    return result


def content_fingerprint(root: Path) -> dict[str, tuple[int, str]]:
    """
    比较完整内容卷中的所有目录、权限和对象字节，不跟随意外链接。

    :param root (Path): 本测试固定的内容卷目录
    :return dict[str, tuple[int, str]]: 相对路径对应的权限与字节摘要
    """
    result: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            digest = "directory"
        else:
            assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result[str(path.relative_to(root))] = (stat.S_IMODE(info.st_mode), digest)
    return result
