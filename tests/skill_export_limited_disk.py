"""
用独立小容量磁盘镜像验证正式 CLI 导出耗尽目标空间后删除暂存并保留源。
"""

import asyncio
import os
import sys
from contextlib import suppress
from pathlib import Path

from skill_ssh_export_live_support import checked_command, export_cli


async def exhaust_export_destination(
    binary: Path,
    cli_root: Path,
    environment: dict[str, str],
    snapshot: str,
    account: str,
    root: Path,
) -> None:
    """
    挂载本次独占的八 MiB HFS 镜像，在真实传输中观察空间耗尽并验证无成功输出。

    :param binary (Path): 正式 CLI 二进制
    :param cli_root (Path): 独立客户端身份目录
    :param environment (dict[str, str]): 本次 SSH agent 环境
    :param snapshot (str): 原始快照身份
    :param account (str): 原始账户身份
    :param root (Path): 本次独占临时镜像目录
    """
    assert sys.platform == "darwin", "this opt-in uses a disposable macOS disk image"
    root.mkdir(mode=0o700)
    image, mount = root / "destination.dmg", root / "mounted"
    mount.mkdir(mode=0o700)
    await checked_command(
        [
            "hdiutil",
            "create",
            "-size",
            "8m",
            "-fs",
            "HFS+",
            "-volname",
            "SkillExportTest",
            str(image),
        ]
    )
    await checked_command(["hdiutil", "attach", "-nobrowse", "-mountpoint", str(mount), str(image)])
    try:
        destination = mount / "exports"
        destination.mkdir(mode=0o700)
        stats = os.statvfs(destination)
        initial = stats.f_bavail * stats.f_frsize
        assert 1 << 20 < initial < 10 << 20
        minimum = initial
        largest_pending = 0

        async def observe() -> None:
            """
            只读取剩余空间与本次暂存对象长度，不读取文件内容。
            """
            nonlocal minimum, largest_pending
            while True:
                stats = os.statvfs(destination)
                minimum = min(minimum, stats.f_bavail * stats.f_frsize)
                for pending in destination.glob(".skill-export-*/bundle/object.pending"):
                    with suppress(FileNotFoundError):
                        largest_pending = max(largest_pending, pending.stat().st_size)
                await asyncio.sleep(0.005)

        observer = asyncio.create_task(observe())
        try:
            code, result = await export_cli(
                binary, cli_root, environment, snapshot, account, destination / "bundle"
            )
        finally:
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
        assert code == 1 and result["committed"] is False
        assert minimum < 1 << 20 and largest_pending > 1 << 20
        assert not list(destination.iterdir()), "failed export left output or private staging"
        print(
            f"ssh_export_destination_full=verified initial_bytes={initial} "
            f"minimum_free_bytes={minimum} largest_pending_bytes={largest_pending}",
            flush=True,
        )
    finally:
        await checked_command(["hdiutil", "detach", str(mount)])
        image.unlink()
