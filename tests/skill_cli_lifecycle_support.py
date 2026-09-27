"""
为真实会话命令提供隔离的 Mutagen、设备身份和受控进程生命周期。
"""

import asyncio
import hashlib
import os
import platform
import shutil
import signal
import sqlite3
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from skill_first_use_live_support import private_fixture
from skill_ssh_export_live_support import checked_command

MUTAGEN_DIGESTS = {
    ("Darwin", "arm64"): "6f810416d9e5fc4fd5e18431146f8b3c5a2056ba5a24f76c1e66da86eb3257e2",
    ("Darwin", "x86_64"): "7d06f7d8fcfe90bc7e55cc834a2f2f20c2e0af9ea9bc35911fc4341ad56a9bbf",
    ("Linux", "aarch64"): "bcba735aebf8cbc11da9b3742118a665599ac697fa06bc5751cac8dcd540db8a",
    ("Linux", "x86_64"): "7735286c778cc438418209f24d03a64f3a0151c8065ef0fe079cfaf093af6f8f",
}


async def prepare_mutagen(repo: Path, root: Path) -> Path:
    """
    下载并校验固定版本的真实依赖，仅提取指定普通文件和正式代理脚本。

    :param repo (Path): CLI 源代码路径
    :param root (Path): 本次独立 CLI 配置目录
    :return Path: 本次使用的真实 Mutagen 可执行文件
    """
    system, machine = platform.system(), platform.machine()
    digest = MUTAGEN_DIGESTS[(system, machine)]
    arch = "amd64" if machine == "x86_64" else "arm64"
    asset = f"mutagen_{system.lower()}_{arch}_v0.18.1.tar.gz"
    archive = root / "mutagen.tar.gz"
    cached = os.environ.get("AGENT_REMOTE_TEST_MUTAGEN_ARCHIVE")
    if cached:
        shutil.copyfile(cached, archive)
    else:
        await checked_command(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--max-time",
                "120",
                "https://github.com/mutagen-io/mutagen/releases/download/v0.18.1/" + asset,
                "--output",
                str(archive),
            ],
            timeout=130,
        )
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest
    binary = root / "bin"
    binary.mkdir(mode=0o700)
    with tarfile.open(archive) as content:
        for name in ("mutagen", "mutagen-agents.tar.gz"):
            member = content.getmember(name)
            assert member.isfile() and member.size < 100 << 20
            source = content.extractfile(member)
            assert source is not None
            with source, (binary / name).open("xb") as output:
                shutil.copyfileobj(source, output)
            (binary / name).chmod(0o700 if name == "mutagen" else 0o600)
    archive.unlink()
    for name in ("ssh", "scp"):
        shutil.copyfile(repo / ("scripts/mutagen-" + name), binary / name)
        (binary / name).chmod(0o700)
    return binary / "mutagen"


def prepare_device(root: Path, url: str, device: str, token: str) -> None:
    """
    保存本次设备短期令牌和刷新时间，不读取宿主现有登录状态。

    :param root (Path): 专用配置目录
    :param url (str): 测试服务地址
    :param device (str): 本次登记设备
    :param token (str): 一次性设备令牌
    """
    __tracebackhide__ = True
    key = f"device-token:{url}:{device}"
    name = "".join(c if c.isascii() and (c.isalnum() or c in "-_.") else "_" for c in key)
    with open(root / "secrets" / (name + ".secret"), "x", opener=private_fixture) as output:
        output.write(token)
    with sqlite3.connect(root / "state.sqlite3") as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO kv (key,value) VALUES (?,?)",
            (
                f"device-token-refresh-at:{url}:{device}",
                "4102444800",
            ),
        )


async def start_cli(
    binary: Path, args: list[str], root: Path, project: Path, environment: dict[str, str]
) -> asyncio.subprocess.Process:
    """
    启动正式命令并保持标准输入打开，以便真实 SSH 终端独立运行。

    :param binary (Path): 本次构建的正式入口
    :param args (list[str]): 无凭据命令参数
    :param root (Path): 专用 CLI 配置根
    :param project (Path): 本次实际同步工作区
    :param environment (dict[str, str]): 独立 SSH agent 和 Mutagen 环境
    :return asyncio.subprocess.Process: 由调用方负责回收的进程
    """
    return await asyncio.create_subprocess_exec(
        str(binary),
        "--home",
        str(root),
        *args,
        cwd=project,
        env=environment | {"AGENT_REMOTE_SECRET_BACKEND": "file", "NO_COLOR": "1"},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )


async def finish_cli(
    process: asyncio.subprocess.Process, timeout: float = 90, *, expected_exit: int = 0
) -> str:
    """
    有界等待原进程结束，错误输出仅用于本次无私密工具正文的验收。

    :param process (asyncio.subprocess.Process): 原命令进程
    :param timeout (float): 等待上限秒数
    :param expected_exit (int): 本次命令契约要求的退出码
    :return str: 已成功命令的终端输出
    """
    reading = asyncio.create_task(process.communicate())
    try:
        try:
            async with asyncio.timeout(timeout):
                output, _ = await asyncio.shield(reading)
        except TimeoutError:
            await stop_cli(process)
            output, _ = await reading
            raise AssertionError(
                "CLI timed out: " + output.decode("utf-8", errors="replace")[-5000:]
            ) from None
        text = output.decode("utf-8", errors="replace")
        assert len(output) < 128 << 10
        assert process.returncode == expected_exit, text[-5000:]
        return text
    finally:
        await stop_cli(process)
        await asyncio.gather(reading, return_exceptions=True)


async def stop_cli(process: asyncio.subprocess.Process) -> None:
    """
    取消时回收原命令组，防止 SSH 子进程留在测试之外。

    :param process (asyncio.subprocess.Process): 本次创建的进程
    """
    if process.returncode is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


async def stop_mutagen(binary: Path, environment: dict[str, str]) -> None:
    """
    只停止本次短路径数据根对应的守护进程，允许明确的尚未启动状态。

    :param binary (Path): 本次固定版本依赖
    :param environment (dict[str, str]): 包含独立数据根的原始环境
    """
    __tracebackhide__ = True
    assert environment.get("MUTAGEN_DATA_DIRECTORY", "").startswith("/tmp/ar-cli-mutagen-")
    process = await asyncio.create_subprocess_exec(
        str(binary),
        "daemon",
        "stop",
        env=environment,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(30):
            _, error = await process.communicate()
        assert process.returncode == 0 or b"connection timed out (is the daemon running?)" in error
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@dataclass
class LifecycleCLI:
    """
    将正式命令绑定到同一独立身份和工作区，避免各阶段意外切换测试环境。
    """

    binary: Path
    root: Path
    project: Path
    environment: dict[str, str] = field(repr=False)

    async def start(self, args: list[str]) -> asyncio.subprocess.Process:
        """
        启动需要与后续停止命令并行的真实会话入口。

        :param args (list[str]): 本次命令参数
        :return asyncio.subprocess.Process: 必须回收的原命令句柄
        """
        return await start_cli(self.binary, args, self.root, self.project, self.environment)

    async def run(self, args: list[str], *, expected_exit: int = 0) -> str:
        """
        完成一个有界命令并校验指定的成功、拒绝或待保存退出码。

        :param args (list[str]): 本次命令参数
        :param expected_exit (int): 本次命令契约要求的退出码
        :return str: 已验证成功的终端输出
        """
        return await finish_cli(await self.start(args), expected_exit=expected_exit)
