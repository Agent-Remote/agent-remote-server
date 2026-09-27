"""
验证正式 CLI 安装、同步、SSH 会话停止发布和新会话状态继承。
"""

import asyncio
import json
import os
import secrets
import shutil
import socket
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import AsyncClient
from live_acceptance_support import build_cli
from skill_cli_lifecycle_support import (
    LifecycleCLI,
    finish_cli,
    prepare_device,
    prepare_mutagen,
    stop_cli,
    stop_mutagen,
)
from skill_cli_retention_outage_support import RetentionOutage, check_pending_retention
from skill_first_use_live_support import serve_first_use
from skill_lifecycle_live_support import LifecycleReports, lifecycle_app
from skill_ssh_export_live_support import (
    checked_command,
    export_agent,
    export_cli_home,
    export_node,
)
from skill_takeover_support import TakeoverHarness
from skill_takeover_support import takeover as takeover
from sqlalchemy import select
from test_skill_content_service import database as database
from test_skill_library import library as library
from test_skill_lifecycle_live import prepare_fixture

from agent_remote_server.models import AuthToken, Node, Session, SshKey, UserDevice
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.security.tokens import hash_token

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_REMOTE_RUN_SKILL_CLI_LIFECYCLE_TEST") != "1",
    reason="requires actual host CLI, Mutagen, OpenSSH and disposable systemd daemons",
)


def source_skill(root: Path) -> None:
    """
    复制确定性第三方来源，保留不可写原始模式以验证远端会话的可写物化。

    :param root (Path): 本次本地技能来源目录
    """
    shutil.copytree(Path(__file__).parent / "fixtures/cli-lifecycle/learning", root)
    for script in (root / "scripts").iterdir():
        script.chmod(0o555)
    (root / "SKILL.md").chmod(0o444)


async def wait_witness(path: Path, expected: str, process: asyncio.subprocess.Process) -> None:
    """
    等待真实同步回来的文件，进程提前结束时立即传播实际命令失败。

    :param path (Path): 本地工作区中的回传见证文件
    :param expected (str): 本次随机内容
    :param process (asyncio.subprocess.Process): 原始会话命令进程
    """
    async with asyncio.timeout(90):
        while not path.exists() or path.read_text() != expected:
            if process.returncode is not None:
                output = await finish_cli(process)
                raise AssertionError(
                    "CLI exited before synchronized runtime witness: " + output[-4000:]
                )
            await asyncio.sleep(0.1)


@pytest.mark.parametrize("outage", [False, True], ids=["normal", "upload-unavailable"])
async def test_cli_installs_stops_publishes_and_inherits(
    takeover: TakeoverHarness, tmp_path: Path, outage: bool
) -> None:
    """
    所有业务动作由正式 CLI 发起，Server 与 Node 不注入启动或发布回执。

    :param takeover (TakeoverHarness): 独立未接管账户
    :param tmp_path (Path): 本次私有工作目录
    :param outage (bool): 是否在首次停止时阻断上传并验证删除与清理保护
    """
    repos = Path(__file__).resolve().parents[2]
    cli_repo, node_repo = repos / "agent-remote-cli", repos / "agent-remote-node"
    binary = await build_cli(cli_repo)
    await checked_command(
        [
            "cargo",
            "build",
            "--locked",
            "--manifest-path",
            str(cli_repo / "Cargo.toml"),
            "--bin",
            "fclaude",
        ],
        timeout=180,
    )
    launcher = binary.with_name("fclaude")
    values = await prepare_fixture(takeover, tmp_path)
    device, key = uuid4(), uuid4()
    device_token = secrets.token_urlsafe(32)
    project, source, cli_root = tmp_path / "project", tmp_path / "learning", tmp_path / "cli"
    project.mkdir()
    source_skill(source)
    marker = "learned-" + secrets.token_hex(16)
    (project / "local-proof").write_text(marker)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="ar-cli-mutagen-", dir="/tmp") as mutagen_data:
        try:
            async with export_agent(tmp_path / "ssh") as environment:
                async with takeover.library.database.begin() as session:
                    node = await session.get(Node, takeover.node)
                    assert node is not None
                    node.ssh_host, node.ssh_port, node.ssh_user = (
                        "127.0.0.1",
                        port,
                        "ar-proof-worker",
                    )
                    node.wireguard_ip = None
                    session.add(
                        UserDevice(
                            id=device,
                            user_id=takeover.library.owner,
                            name="CLI 验收",
                            platform="linux",
                            status="active",
                        )
                    )
                    await session.flush()
                    session.add(
                        SshKey(
                            id=key,
                            user_device_id=device,
                            public_key=(tmp_path / "ssh/key.pub").read_text().strip(),
                            fingerprint=str(key),
                            status="active",
                        )
                    )
                    session.add(
                        AuthToken(
                            user_id=takeover.library.owner,
                            user_device_id=device,
                            token_type="device",
                            status="active",
                            token_hash=hash_token(takeover.settings.secret_key, device_token),
                            expires_at=datetime.now(UTC) + timedelta(hours=1),
                        )
                    )
                reports = LifecycleReports()
                application = lifecycle_app(takeover, reports)
                retention = RetentionOutage()
                retention.install(application)
                async with serve_first_use(takeover, False, application) as server:
                    export_cli_home(
                        cli_root, server.local_url, values["user_token"], str(device), str(key)
                    )
                    prepare_device(cli_root, server.local_url, str(device), device_token)
                    mutagen = await prepare_mutagen(cli_repo, cli_root)
                    environment |= {
                        "MUTAGEN_DATA_DIRECTORY": mutagen_data,
                        "AGENT_REMOTE_HOME": str(cli_root),
                        "TERM": "xterm-256color",
                    }
                    cli = LifecycleCLI(binary, cli_root, project, environment)
                    fclaude = LifecycleCLI(launcher, cli_root, project, environment)
                    first: asyncio.subprocess.Process | None = None
                    second: asyncio.subprocess.Process | None = None
                    node_root = tmp_path / "node"
                    node_values: dict[str, str | bool] = {
                        **values,
                        "url": server.worker_url,
                        "ssh_export": True,
                        "cli_lifecycle": True,
                    }
                    try:
                        async with export_node(
                            node_repo,
                            node_root,
                            node_values,
                            port,
                            cli_lifecycle=True,
                        ) as daemons:
                            await daemons.wait_ready()
                            installed = json.loads(
                                await cli.run(
                                    [
                                        "--json",
                                        "skill",
                                        "add",
                                        str(source),
                                        "--yes",
                                        "--timeout",
                                        "60",
                                    ]
                                )
                            )
                            assert installed["committed"] and installed["status"] == "ready"
                            selection = json.loads(
                                await cli.run(
                                    [
                                        "--json",
                                        "skill",
                                        "list",
                                        "--effective",
                                        "--account-id",
                                        str(takeover.account),
                                    ]
                                )
                            )
                            selected = selection["data"]["items"]
                            assert len(selected) == 1 and selected[0]["name"] == "learning"
                            assert selected[0]["effective"]["included"]
                            assert selected[0]["model_loaded"] is False
                            first = await fclaude.start(
                                [
                                    "--yes",
                                    "--account-id",
                                    str(takeover.account),
                                    "new",
                                    "--",
                                    "--cli-write",
                                    marker,
                                ]
                            )
                            await wait_witness(project / "writer-ready", marker, first)
                            async with takeover.library.database() as session:
                                original = (
                                    await session.scalars(
                                        select(Session).where(
                                            Session.tool_account_id == takeover.account
                                        )
                                    )
                                ).one()
                                first_id = str(original.id)
                                snapshot = (
                                    await session.scalars(
                                        select(SessionSkillSnapshot).where(
                                            SessionSkillSnapshot.session_id == original.id
                                        )
                                    )
                                ).one()
                                operation = str(snapshot.id)
                            if outage:
                                retention.active = True
                                await check_pending_retention(
                                    fclaude,
                                    retention,
                                    node_root,
                                    first_id,
                                    operation,
                                    server.local_url,
                                    device_token,
                                )
                                retention.active = False
                            stopped = await fclaude.run(["stop", first_id, "--timeout", "60"])
                            assert "published" in stopped and operation in stopped
                            await finish_cli(first)
                            (node_root / "control/restart").touch(mode=0o600)
                            async with asyncio.timeout(60):
                                while not (node_root / "control/restarted").exists():
                                    await asyncio.sleep(0.1)
                            second = await fclaude.start(
                                [
                                    "--yes",
                                    "--account-id",
                                    str(takeover.account),
                                    "new",
                                    "--",
                                    "--cli-read",
                                    marker,
                                ]
                            )
                            await finish_cli(second)
                            async with asyncio.timeout(30):
                                while not (project / "inherited.txt").exists():
                                    await asyncio.sleep(0.1)
                            assert (project / "inherited.txt").read_text() == marker
                            async with takeover.library.database() as session:
                                successor = (
                                    await session.scalars(
                                        select(Session).where(
                                            Session.tool_account_id == takeover.account,
                                            Session.id != original.id,
                                        )
                                    )
                                ).one()
                                successor_snapshot = (
                                    await session.scalars(
                                        select(SessionSkillSnapshot).where(
                                            SessionSkillSnapshot.session_id == successor.id
                                        )
                                    )
                                ).one()
                                successor_id, successor_operation = (
                                    str(successor.id),
                                    str(successor_snapshot.id),
                                )
                            saved = await fclaude.run(
                                ["stop-status", successor_operation, "--wait", "--timeout", "60"]
                            )
                            assert "published" in saved and successor_operation in saved
                            (node_root / "control/sessions.json").write_text(
                                json.dumps([first_id, successor_id])
                            )
                            (node_root / "control/done").touch(mode=0o600)
                            await daemons.finish()
                        await fclaude.run(["delete", first_id])
                        after = await fclaude.run(["stop-status", operation])
                        assert operation in after and "published" in after and "deleted" in after
                        async with AsyncClient(
                            base_url=server.local_url,
                            headers={"Authorization": "Bearer " + device_token},
                        ) as client:
                            assert (
                                await client.get(f"/api/v1/sessions/{first_id}")
                            ).status_code == 404
                        assert reports.heartbeats >= 2 and reports.reconciliations >= 2
                    finally:
                        for process in (first, second):
                            if process is not None:
                                await stop_cli(process)
                        await stop_mutagen(mutagen, environment)
        finally:
            if cli_root.exists():
                shutil.rmtree(cli_root)
