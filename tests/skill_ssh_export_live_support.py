"""
管理真实 SSH 导出验收的私有密钥、正式 CLI 和一次性容器进程。
"""

import asyncio
import json
import os
import signal
import sqlite3
import tempfile
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from live_acceptance_support import LiveDaemonProcess, prepare_cli_user
from skill_first_use_live_support import private_fixture


async def checked_command(
    args: list[str], env: dict[str, str] | None = None, *, timeout: float = 30
) -> None:
    """
    执行无凭据参数的短命令，失败时不回显外部正文。

    :param args (list[str]): 原始命令参数
    :param env (dict[str, str] | None): 独立运行环境
    :param timeout (float): 子进程等待上限秒数
    """
    __tracebackhide__ = True
    process = await asyncio.create_subprocess_exec(
        *args, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        async with asyncio.timeout(timeout):
            assert await process.wait() == 0, "disposable SSH setup command failed"
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@asynccontextmanager
async def export_agent(root: Path) -> AsyncIterator[dict[str, str]]:
    """
    独立启动 SSH agent 并仅加载本次生成的私钥，不使用宿主身份。

    :param root (Path): 私有密钥目录
    :return AsyncIterator[dict[str, str]]: 仅指向本次 agent 的子进程环境
    """
    root.mkdir(mode=0o700)
    try:
        await checked_command(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / "key")]
        )
        with tempfile.TemporaryDirectory(prefix="ar-export-agent-", dir="/tmp") as agent_root:
            socket_path = Path(agent_root) / "agent.sock"
            agent = await asyncio.create_subprocess_exec(
                "ssh-agent",
                "-D",
                "-a",
                str(socket_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            environment = os.environ | {"SSH_AUTH_SOCK": str(socket_path)}
            try:
                async with asyncio.timeout(10):
                    while not socket_path.exists():
                        assert agent.returncode is None, "disposable SSH agent exited"
                        await asyncio.sleep(0.01)
                await checked_command(["ssh-add", str(root / "key")], environment)
                yield environment
            finally:
                if agent.returncode is None:
                    agent.terminate()
                await agent.wait()
    finally:
        (root / "key").unlink(missing_ok=True)
        (root / "key.pub").unlink(missing_ok=True)


def export_cli_home(root: Path, url: str, token: str, device: str, key: str) -> None:
    """
    仅预设本次设备注册身份，技能命令和 SSH 均使用正式 CLI。

    :param root (Path): 私有 CLI 根目录
    :param url (str): 本次 Server 地址
    :param token (str): 一次性用户令牌
    :param device (str): 已登记设备
    :param key (str): 已登记公钥身份
    """
    prepare_cli_user(root, url, token)
    with (root / "config.toml").open("a", encoding="utf-8") as output:
        output.write(f'active_device_id = "{device}"\n')
    with sqlite3.connect(root / "state.sqlite3") as database:
        database.execute(
            "CREATE TABLE devices (id TEXT PRIMARY KEY, server_url TEXT NOT NULL, "
            "name TEXT NOT NULL, platform TEXT NOT NULL, status TEXT NOT NULL, "
            "ssh_key_id TEXT, wireguard_peer_id TEXT, created_at TEXT, last_seen_at TEXT, "
            "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        database.execute(
            "INSERT INTO devices (id,server_url,name,platform,status,ssh_key_id) "
            "VALUES (?,?,?,?,?,?)",
            (device, url, "disposable-export", "linux", "active", key),
        )
    (root / "state.sqlite3").chmod(0o600)


async def export_cli(
    binary: Path,
    root: Path,
    environment: dict[str, str],
    snapshot: str,
    account: str,
    output: Path,
    *,
    timeout: float = 90,
) -> tuple[int, dict[str, object]]:
    """
    执行正式导出命令并解析唯一封套，不打印凭据或未校验的远端正文。

    :param binary (Path): 当前源代码构建的 CLI
    :param root (Path): 独立 CLI 身份目录
    :param environment (dict[str, str]): 本次 SSH agent 环境
    :param snapshot (str): 原始快照身份
    :param account (str): 原始账户身份
    :param output (Path): 尚不存在的目标目录
    :param timeout (float): 本次进程等待上限，容量用例需覆盖正式传输期限
    :return tuple[int, dict[str, object]]: 退出状态及命令封套
    """
    process = await asyncio.create_subprocess_exec(
        str(binary),
        "--home",
        str(root),
        "--json",
        "skill",
        "state",
        "export",
        "--snapshot",
        snapshot,
        "--scope",
        "account-directory",
        "--account-id",
        account,
        "--output",
        str(output),
        env=environment | {"AGENT_REMOTE_SECRET_BACKEND": "file"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(timeout):
            raw, diagnostic = await process.communicate()
        assert len(raw) <= 64 << 10 and len(diagnostic) <= 64 << 10
        value = json.loads(raw)
        assert isinstance(value, dict)
        return process.returncode or 0, value
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@asynccontextmanager
async def export_node(
    repo: Path, root: Path, values: dict[str, str | bool], port: int, *, cli_lifecycle: bool = False
) -> AsyncIterator[LiveDaemonProcess]:
    """
    保留容器运行直到主测试完成真实 CLI 导出及撤销验证。

    :param repo (Path): Node 源代码目录
    :param root (Path): 本次私有进程协调目录
    :param values (dict[str, str | bool]): 一次性身份和连接
    :param port (int): 本机专用 SSH 端口
    :param cli_lifecycle (bool): 是否使用真实 CLI 会话生命周期协调模式
    :return AsyncIterator[LiveDaemonProcess]: 可等待原进程终态的句柄
    """
    root.mkdir(mode=0o700)
    control = root / "control"
    control.mkdir(mode=0o700)
    fixture = root / "fixture.json"
    with open(fixture, "x", encoding="utf-8", opener=private_fixture) as output:
        json.dump(values, output)
    script = repo / "tests/linux_skill_ssh_export_test.sh"
    # Bash 会继续读取运行中的脚本文件；固定源码避免编辑工作区改变清理阶段。
    process = await asyncio.create_subprocess_exec(
        "bash",
        "-c",
        script.read_text(encoding="utf-8"),
        str(script),
        cwd=repo,
        env=os.environ
        | {
            "AGENT_REMOTE_TEST_SKILL_SSH_EXPORT_FIXTURE": str(fixture),
            "AGENT_REMOTE_TEST_SKILL_SSH_EXPORT_CONTROL": str(control),
            "AGENT_REMOTE_TEST_SKILL_SSH_EXPORT_PORT": str(port),
            "AGENT_REMOTE_TEST_SKILL_SSH_EXPORT_MODE": "cli-lifecycle"
            if cli_lifecycle
            else "export",
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    ready = asyncio.Event()
    reader = asyncio.create_task(export_output(process, values, ready, cli_lifecycle))
    try:
        yield LiveDaemonProcess(process, reader, ready)
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 40)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        await asyncio.gather(reader, return_exceptions=True)
        fixture.unlink(missing_ok=True)


async def export_output(
    process: asyncio.subprocess.Process,
    values: dict[str, str | bool],
    ready: asyncio.Event,
    cli_lifecycle: bool = False,
) -> None:
    """
    排空有界协调输出，只有真实测试完成标记能证明验收成功。

    :param process (asyncio.subprocess.Process): 原容器脚本进程
    :param values (dict[str, str | bool]): 不得回显的输入
    :param ready (asyncio.Event): 原始冻结数据准备事件
    :param cli_lifecycle (bool): 是否等待真实 CLI 生命周期标记
    """
    __tracebackhide__ = True
    assert process.stdout is not None
    lines: deque[str] = deque(maxlen=80)
    passed = False
    marker = "CLI_LIFECYCLE_READY" if cli_lifecycle else "SSH_EXPORT_READY"
    test_name = "TestNativeCLILifecycle" if cli_lifecycle else "TestNativeSSHExport"
    capacity = (
        values.get("export_capacity") or values.get("export_bytes") or values.get("export_long")
    )
    async with asyncio.timeout(1920 if capacity else 600):
        while raw := await process.stdout.readline():
            text = raw.decode("utf-8", errors="replace")
            if marker in text:
                ready.set()
            if "--- PASS: " + test_name in text:
                passed = True
            for value in values.values():
                if isinstance(value, str) and value:
                    text = text.replace(value, "<fixture>")
            lines.append(text[-2000:])
        await process.wait()
    assert process.returncode == 0 and passed, "".join(lines)[-10000:]
