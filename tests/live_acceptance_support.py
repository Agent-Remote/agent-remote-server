"""
复用真实命令验收的进程观察和私有 CLI 测试身份，不创建业务回执。
"""

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

from skill_first_use_live_support import private_fixture


@dataclass
class LiveDaemonProcess:
    """
    拥有容器脚本和输出读取任务，准备事件只来自原进程的明确标记。
    """

    process: asyncio.subprocess.Process
    complete: asyncio.Task[None]
    ready: asyncio.Event

    async def wait_ready(self, timeout: float = 300) -> None:
        """
        等待正式进程准备阶段或立即传播进程失败，不靠固定延迟猜测状态。

        :param timeout (float): 本次验收准备阶段的最长秒数
        """
        waiter = asyncio.create_task(self.ready.wait())
        try:
            done, _ = await asyncio.wait(
                {waiter, self.complete}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if self.complete in done:
                await self.complete
            if not self.ready.is_set():
                raise AssertionError("disposable daemons did not become ready")
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    async def finish(self) -> None:
        """
        等待原测试进程终态并保留其真实验证结果。
        """
        await asyncio.wait_for(asyncio.shield(self.complete), 240)


async def build_cli(repo: Path, *, optimized: bool = False) -> Path:
    """
    编译当前 CLI 源码，避免使用已安装或旧工作区二进制。

    :param repo (Path): 相邻 CLI 仓库
    :param optimized (bool): 容量验收使用发布流程相同的本地优化构建
    :return Path: 本次编译的正式入口
    """
    process = await asyncio.create_subprocess_exec(
        "cargo",
        "build",
        "--locked",
        "--bin",
        "agent-remote",
        *(["--release"] if optimized else []),
        cwd=repo,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(600 if optimized else 180):
            _, output = await process.communicate()
        assert process.returncode == 0, output.decode("utf-8", errors="replace")[-4000:]
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()
    return repo / "target" / ("release" if optimized else "debug") / "agent-remote"


def prepare_cli_user(root: Path, url: str, token: str) -> None:
    """
    写入独立短期测试用户凭据，不读取宿主钥匙串或既有 CLI 配置。

    :param root (Path): 专用 CLI 主目录
    :param url (str): 本机测试服务地址
    :param token (str): 一次性测试用户令牌
    """
    __tracebackhide__ = True
    root.mkdir(mode=0o700)
    secrets = root / "secrets"
    secrets.mkdir(mode=0o700)
    name = "".join(
        character if character.isascii() and (character.isalnum() or character in "-_.") else "_"
        for character in "user-token:" + url
    )
    expiry = int(time.time()) + 3600
    credential = {
        "version": 1,
        "token": {
            "access_token": token,
            "expires_in": 3600,
            "refresh_token": None,
            "refresh_expires_in": None,
        },
        "refresh_at": expiry,
        "expires_at": expiry,
        "session_expires_at": expiry,
    }
    for path, data in [
        (root / "config.toml", "server_url = " + json.dumps(url) + "\n"),
        (secrets / (name + ".secret"), json.dumps(credential)),
    ]:
        with open(path, "x", encoding="utf-8", opener=private_fixture) as output:
            output.write(data)
