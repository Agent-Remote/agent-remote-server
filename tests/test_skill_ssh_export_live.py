"""
通过真实 CLI、Server、SSH 网关和重启后的 Helper 验证未上传快照的完整导出。
"""

import asyncio
import hashlib
import json
import os
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from httpx import AsyncClient
from live_acceptance_support import build_cli
from skill_export_limited_disk import exhaust_export_destination
from skill_export_slow_link import export_link
from skill_first_use_live_support import serve_first_use
from skill_lifecycle_live_support import LifecycleReports, lifecycle_app
from skill_ssh_export_live_support import export_agent, export_cli, export_cli_home, export_node
from skill_takeover_support import TakeoverHarness
from skill_takeover_support import takeover as takeover
from sqlalchemy import func, select
from starlette.middleware.base import RequestResponseEndpoint
from test_skill_content_service import database as database
from test_skill_library import library as library
from test_skill_lifecycle_live import prepare_fixture

from agent_remote_server.models import Node, NodeTask, SshKey, UserDevice
from agent_remote_server.models.skill_snapshots import SkillFinalization
from agent_remote_server.models.skill_terminations import SkillSnapshotTermination
from agent_remote_server.schemas.skill_library import SkillAddRequest

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_REMOTE_RUN_SKILL_SSH_EXPORT_TEST") != "1",
    reason="requires disposable systemd, real SSH and current CLI acceptance",
)


@dataclass
class ExportReports:
    """
    记录真实上传拒绝与授权复查，不保存请求正文或临时凭据。
    """

    uploads: int = 0
    verifications: int = 0
    revoke_at: int | None = None
    revoked: bool = False
    recent_verifications: list[tuple[int, int, float]] = field(default_factory=list)
    first_verified_at: float | None = None
    last_renewed_at: float | None = None


def export_app(state: TakeoverHarness, key: UUID, reports: ExportReports) -> FastAPI:
    """
    仅阻断快照上传并在指定复查前撤销公钥，其他路径运行生产处理。

    :param state (TakeoverHarness): 本次独立账户和存储
    :param key (UUID): 原设备的临时公钥
    :param reports (ExportReports): 无内容观察
    :return FastAPI: 使用真实授权的隔离应用
    """
    app = lifecycle_app(state, LifecycleReports())

    @app.middleware("http")
    async def faults(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """
        阻止云端保存以保留本地唯一副本，按真实授权次数触发撤销。

        :param request (Request): 原始 HTTP 请求
        :param call_next (RequestResponseEndpoint): 生产处理
        :return Response: 真实结果或明确的上传故障
        """
        path = request.url.path
        started = time.monotonic()
        verification = 0
        if (
            request.method == "POST"
            and path.startswith("/api/v1/node/skill-snapshots/")
            and path.endswith("/finalization")
        ):
            reports.uploads += 1
            return JSONResponse({"detail": "disposable upload outage"}, status_code=503)
        if (
            request.method == "POST"
            and path.startswith("/api/v1/node/skill-state-exports/")
            and path.endswith(("/verify", "/renew"))
        ):
            reports.verifications += 1
            verification = reports.verifications
            if reports.revoke_at == reports.verifications:
                async with state.library.database.begin() as session:
                    stored = await session.get(SshKey, key)
                    assert stored is not None
                    stored.status = "revoked"
                reports.revoked = True
        response = await call_next(request)
        if verification and response.status_code == 200:
            if path.endswith("/verify") and reports.first_verified_at is None:
                reports.first_verified_at = time.monotonic()
            if path.endswith("/renew"):
                reports.last_renewed_at = time.monotonic()
        if verification:
            reports.recent_verifications.append(
                (verification, response.status_code, round(time.monotonic() - started, 3))
            )
            del reports.recent_verifications[:-16]
        return response

    return app


@pytest.mark.parametrize("quota", [False, True], ids=["frozen", "runtime-quota"])
async def test_cli_exports_unuploaded_native_snapshot_over_real_ssh(
    takeover: TakeoverHarness, tmp_path: Path, quota: bool
) -> None:
    """
    正式守护进程生成原始快照，真实 CLI 导出完整内容并在传输中撤销时拒绝发布。

    :param takeover (TakeoverHarness): 独立未接管账户
    :param tmp_path (Path): 私有验收目录
    :param quota (bool): 是否通过实际运行额度阻止本地冻结
    """
    git_root = Path(__file__).resolve().parents[2]
    node_repo = Path(
        os.environ.get("AGENT_REMOTE_TEST_NODE_REPO", str(git_root / "agent-remote-node"))
    )
    cli_repo = Path(
        os.environ.get("AGENT_REMOTE_TEST_CLI_REPO", str(git_root / "agent-remote-cli"))
    )
    entries_capacity = os.environ.get("AGENT_REMOTE_RUN_SKILL_SSH_EXPORT_CAPACITY") == "1"
    bytes_capacity = os.environ.get("AGENT_REMOTE_RUN_SKILL_SSH_EXPORT_BYTES") == "1"
    oversize = os.environ.get("AGENT_REMOTE_RUN_SKILL_SSH_EXPORT_OVERSIZE") == "1"
    over_entries = os.environ.get("AGENT_REMOTE_RUN_SKILL_SSH_EXPORT_OVER_ENTRIES") == "1"
    assert not over_entries or (entries_capacity and quota and not bytes_capacity)
    assert not oversize or (bytes_capacity and quota), (
        "oversize requires stopped-work byte capacity"
    )
    capacity = entries_capacity or bytes_capacity
    long_transfer = os.environ.get("AGENT_REMOTE_RUN_SKILL_SSH_EXPORT_LONG") == "1"
    destination_full = os.environ.get("AGENT_REMOTE_RUN_SKILL_SSH_EXPORT_DESTINATION_FULL") == "1"
    assert not destination_full or (quota and not capacity and not long_transfer)
    assert not long_transfer or not capacity, "long-link acceptance uses its own small tree"
    binary = await build_cli(cli_repo, optimized=capacity)
    if capacity:
        print("ssh_export_cli_profile=release", flush=True)
    values = await prepare_fixture(takeover, tmp_path)
    device_id, key_id = uuid4(), uuid4()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    async with (
        export_agent(tmp_path / "ssh") as environment,
        export_link(port, long_transfer) as cli_port,
    ):
        public_key = (tmp_path / "ssh/key.pub").read_text().strip()
        async with takeover.library.database.begin() as session:
            node = await session.get(Node, takeover.node)
            assert node is not None
            node.ssh_host, node.ssh_port, node.ssh_user = "127.0.0.1", cli_port, "ar-proof-worker"
            node.wireguard_ip = None
            session.add(
                UserDevice(
                    id=device_id,
                    user_id=takeover.library.owner,
                    name="临时 SSH 导出",
                    platform="linux",
                    status="active",
                )
            )
            await session.flush()
            session.add(
                SshKey(
                    id=key_id,
                    user_device_id=device_id,
                    public_key=public_key,
                    fingerprint=str(key_id),
                    status="active",
                )
            )
        candidate = await takeover.library.candidate(
            name="learning",
            content=(
                b"---\nname: learning\ndescription: synthetic SSH export fixture\n"
                b"---\nOriginal instructions\n"
            ),
        )
        reports = ExportReports()
        async with (
            serve_first_use(takeover, False, export_app(takeover, key_id, reports)) as server,
            AsyncClient(
                base_url=server.local_url,
                headers={"Authorization": "Bearer " + values["user_token"]},
                timeout=30,
            ) as client,
        ):
            response = await client.post(
                "/api/v1/skills/installations",
                json=SkillAddRequest(
                    idempotency_key=str(uuid4()), expected_generation=0, items=(candidate,)
                ).model_dump(mode="json"),
            )
            assert response.status_code == 200
            operation = response.json()
            assert operation["committed"] and operation["status"] == "preparing"
            cli_root = tmp_path / "cli"
            export_cli_home(
                cli_root, server.local_url, values["user_token"], str(device_id), str(key_id)
            )
            node_root = tmp_path / "node"
            async with export_node(
                node_repo,
                node_root,
                values
                | {
                    "url": server.worker_url,
                    "operation_id": operation["operation_id"],
                    "ssh_export": True,
                    "export_quota": quota,
                    "export_capacity": entries_capacity,
                    "export_bytes": bytes_capacity,
                    "export_oversize": oversize,
                    "export_long": long_transfer,
                    "export_destination_full": destination_full,
                    "export_over_entries": over_entries,
                },
                port,
            ) as daemons:
                await daemons.wait_ready(timeout=1200 if capacity else 300)
                expected = json.loads((node_root / "control/expected.json").read_bytes())
                if entries_capacity:
                    assert len(expected["manifest"]["entries"]) == 100_000 + int(over_entries)
                    assert expected["file_objects"] == (99_988 if bytes_capacity else 99_997) + int(
                        over_entries
                    )
                elif bytes_capacity:
                    assert len(expected["manifest"]["entries"]) == 36
                    assert expected["file_objects"] == 24
                if bytes_capacity:
                    files = [
                        entry
                        for entry in expected["manifest"]["entries"]
                        if entry["kind"] == "file"
                    ]
                    assert sum(entry["size"] for entry in files) == (10 << 30) + (
                        (1 << 20) if oversize else 0
                    )
                    assert len({entry["sha256"] for entry in files}) == len(files)
                snapshot = expected["binding"]["snapshot_id"]
                for _ in range(120):
                    status = await client.get(f"/api/v1/sessions/skill-finalizations/{snapshot}")
                    assert status.status_code == 200, status.text
                    view = status.json()["data"]
                    if view["process_stopped"]:
                        break
                    await asyncio.sleep(0.25)
                assert view["process_stopped"] and not view["content_retained"]
                assert view["status"] == ("capture_pending" if quota else "local_durable")
                assert view["capture_error"] == ("quota_exceeded" if quota else None)
                assert view["checkpoint_id"] is None and view["finalization_id"] is None
                if not quota:
                    assert reports.uploads > 0
                output = tmp_path / "bundle"
                if destination_full:
                    assert (
                        sum(entry["size"] for entry in expected["manifest"]["entries"]) > 32 << 20
                    )
                    await exhaust_export_destination(
                        binary,
                        cli_root,
                        environment,
                        snapshot,
                        str(takeover.account),
                        tmp_path / "limited-destination",
                    )
                started = time.monotonic()
                code, result = await export_cli(
                    binary,
                    cli_root,
                    environment,
                    snapshot,
                    str(takeover.account),
                    output,
                    timeout=1200 if long_transfer else (960 if capacity else 90),
                )
                print(
                    f"ssh_export_attempt=initial exit={code} "
                    f"seconds={time.monotonic() - started:.3f} "
                    f"verifications={reports.verifications} "
                    f"recent={reports.recent_verifications!r}",
                    flush=True,
                )
                assert code == 0, (result, reports.recent_verifications)
                if long_transfer:
                    assert reports.first_verified_at is not None
                    assert reports.last_renewed_at is not None
                    continued = reports.last_renewed_at - reports.first_verified_at
                    assert continued > 900, "gateway did not remain live past original expiry"
                    print(f"ssh_export_live_continuation_seconds={continued:.3f}", flush=True)
                assert result["committed"] is False
                exported = cast(dict[str, object], result["data"])
                assert exported["tree_digest"] == expected["tree_digest"]
                assert exported["unclean"] is False
                assert reports.verifications >= 3
                await asyncio.to_thread(verify_bundle, output, expected["manifest"], quota)
                if capacity:
                    print(
                        f"ssh_export_capacity_entries={len(expected['manifest']['entries'])} "
                        f"byte_capacity={bytes_capacity} oversize={oversize} quota={quota} "
                        f"seconds={time.monotonic() - started:.3f}",
                        flush=True,
                    )
                metadata = json.loads((output / "checkpoint.json").read_bytes())
                assert metadata["binding"] == exported["binding"]
                assert (
                    metadata["recovery_digest" if quota else "tree_digest"]
                    == expected["tree_digest"]
                )
                assert metadata["format"] == (
                    "agent-remote-skill-node-recovery-v1"
                    if quota
                    else "agent-remote-skill-node-snapshot-v1"
                )
                assert "grant" not in metadata
                failed_output = tmp_path / "revoked-bundle"
                reports.revoke_at = reports.verifications + 3
                started = time.monotonic()
                code, result = await export_cli(
                    binary,
                    cli_root,
                    environment,
                    snapshot,
                    str(takeover.account),
                    failed_output,
                    timeout=960 if capacity else 90,
                )
                print(
                    f"ssh_export_attempt=revoked exit={code} "
                    f"seconds={time.monotonic() - started:.3f} "
                    f"verifications={reports.verifications} "
                    f"recent={reports.recent_verifications!r}",
                    flush=True,
                )
                assert code == 1 and reports.revoked, (result, reports.recent_verifications)
                assert not failed_output.exists()
                assert not list(tmp_path.glob(".skill-export-*"))
                async with takeover.library.database() as session:
                    assert (
                        await session.scalar(select(func.count()).select_from(SkillFinalization))
                        == 0
                    )
                    receipt = await session.get(SkillSnapshotTermination, UUID(snapshot))
                    assert receipt is not None and not receipt.unclean
                    assert receipt.incoming_digest == (None if quota else expected["tree_digest"])
                    assert receipt.capture_error == ("quota_exceeded" if quota else None)
                    key_tasks = list(
                        await session.scalars(
                            select(NodeTask).where(NodeTask.task_type == "sync_ssh_keys")
                        )
                    )
                    assert len(key_tasks) == 1 and key_tasks[0].status == "succeeded"
                (node_root / "control/done").touch(mode=0o600)
                await daemons.finish()


def verify_bundle(output: Path, manifest: dict[str, object], recovery: bool) -> None:
    """
    对比完整原清单和每个对象，明确验证二进制、权限、空目录及链接。

    :param output (Path): 正式 CLI 发布的目录
    :param manifest (dict[str, object]): Helper 的完整原始冻结清单
    :param recovery (bool): 是否采用独立流式恢复格式
    """
    entries = cast(list[dict[str, object]], manifest["entries"])
    if recovery:
        assert not (output / "manifest.json").exists()
        observed = [
            json.loads(line) for line in (output / "recovery.jsonl").read_bytes().splitlines()
        ]
        assert observed == entries
        recovery_hash = hashlib.sha256(b"agent-remote-skill-recovery-tree-v1\0")
        for entry in observed:
            for name in (
                "path",
                "kind",
                "mode",
                "size",
                "sha256",
                "target",
                "content_kind",
                "dependency",
            ):
                recovery_hash.update(str(entry[name]).encode() + b"\0")
            index = hashlib.sha256(str(entry["path"]).encode()).hexdigest()
            assert json.loads((output / "entries" / index).read_bytes()) == entry
        assert len(list((output / "entries").iterdir())) == len(entries)
        metadata = json.loads((output / "checkpoint.json").read_bytes())
        assert metadata["recovery_digest"] == recovery_hash.hexdigest()
        assert metadata["entries"] == len(entries)
        assert metadata["file_objects"] == sum(entry["kind"] == "file" for entry in entries)
        assert metadata["file_bytes"] == sum(cast(int, entry["size"]) for entry in entries)
    else:
        original = json.loads((output / "manifest.json").read_bytes())
        assert original["version"] == 1
        assert original["entries"] == entries
    by_path = {str(entry["path"]): entry for entry in entries}
    digests = set()
    for entry in entries:
        if entry["kind"] == "file":
            digest = str(entry["sha256"])
            with (output / "objects" / digest).open("rb") as content:
                assert os.fstat(content.fileno()).st_size == entry["size"]
                assert hashlib.file_digest(content, "sha256").hexdigest() == digest
            digests.add(digest)
    assert {path.name for path in (output / "objects").iterdir()} == digests
    assert (
        output / "objects" / str(by_path["learning/state.db"]["sha256"])
    ).read_bytes() == b"\0\xff\x01binary state\0"
    assert (
        output / "objects" / str(by_path["learning/memory.txt"]["sha256"])
    ).read_bytes() == b"retained SSH export learning\n"
    assert by_path["learning/memory.txt"]["mode"] == 0o750
    assert by_path["learning/empty"]["kind"] == "directory"
    assert by_path["learning/link"]["kind"] == "symlink"
    assert by_path["learning/link"]["target"] == "memory.txt"
    assert (output / "objects" / str(by_path["learning/large.db"]["sha256"])).read_bytes() == bytes(
        8192
    )
    assert "root-state" in by_path
