"""
在真实 CLI 停止链路中阻断上传，验证未保留内容仍阻止删除和特权清理。
"""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from httpx import AsyncClient
from skill_cli_lifecycle_support import LifecycleCLI
from starlette.middleware.base import RequestResponseEndpoint


@dataclass
class RetentionOutage:
    """
    仅记录受控上传中断的次数，不替换任何任务或持久化回执。
    """

    active: bool = False
    rejected_uploads: int = 0

    def install(self, app: FastAPI) -> None:
        """
        将失败注入限定为节点收尾上传入口，保留 CLI 查询和停止确认的真实处理。

        :param app (FastAPI): 本次隔离的正式路由应用
        """

        @app.middleware("http")
        async def refuse_upload(request: Request, call_next: RequestResponseEndpoint) -> Response:
            """
            在指定时间拒绝上传受理，恢复后仍使用原始生产处理。

            :param request (Request): 实际 HTTP 请求
            :param call_next (RequestResponseEndpoint): 原始路由处理
            :return Response: 原响应或明确的暂时不可用错误
            """
            if (
                self.active
                and request.method == "POST"
                and request.url.path.startswith("/api/v1/node/skill-snapshots/")
                and request.url.path.endswith("/finalization")
            ):
                self.rejected_uploads += 1
                return JSONResponse({"detail": "disposable upload outage"}, status_code=503)
            return await call_next(request)


async def check_pending_retention(
    cli: LifecycleCLI,
    outage: RetentionOutage,
    node_root: Path,
    session: str,
    snapshot: str,
    server_url: str,
    token: str,
) -> None:
    """
    验证正式停止返回待保存，同时单个删除、批量删除和 Helper 清理均保留唯一输入。

    :param cli (LifecycleCLI): 正式 fclaude 命令与隔离身份
    :param outage (RetentionOutage): 当前阻断的真实上传计数
    :param node_root (Path): 仅供原始 Node 验收进程读取的协调目录
    :param session (str): 正式创建的原会话标识
    :param snapshot (str): 该会话的独立收尾操作标识
    :param server_url (str): 本次隔离 Server 地址
    :param token (str): 原设备短期令牌
    """
    __tracebackhide__ = True
    stopped = await cli.run(["stop", session, "--timeout", "5"], expected_exit=3)
    assert snapshot in stopped and "published" not in stopped, (
        "pending stop output lost original operation"
    )
    async with AsyncClient(
        base_url=server_url, headers={"Authorization": "Bearer " + token}, timeout=20
    ) as client:
        async with asyncio.timeout(30):
            while True:
                response = await client.get(f"/api/v1/sessions/skill-finalizations/{snapshot}")
                assert response.status_code == 200
                view = response.json()["data"]
                if view["process_stopped"] and outage.rejected_uploads > 0:
                    break
                await asyncio.sleep(0.1)
        assert view["status"] == "local_durable", f"unexpected pending state: {view}"
        assert not view["content_retained"] and view["checkpoint_id"] is None
        assert view["unclean"] is False
        control = node_root / "control"
        (control / "pending-retention.json").write_text(json.dumps({"session_id": session}))
        single, bulk = await asyncio.gather(
            cli.run(["delete", session], expected_exit=1),
            cli.run(["delete", "--all"], expected_exit=1),
        )
        assert "STATE_PENDING" in single and "STATE_PENDING" in bulk, (single, bulk)
        async with asyncio.timeout(30):
            while not (control / "pending-retention-checked").exists():
                await asyncio.sleep(0.1)
        response = await client.get(f"/api/v1/sessions/{session}")
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "stopped", response.text
        response = await client.get(f"/api/v1/sessions/skill-finalizations/{snapshot}")
        assert response.status_code == 200
        after = response.json()["data"]
        assert after["operation_id"] == snapshot and not after["content_retained"], after
        assert after["checkpoint_id"] is None and after["status"] == "local_durable", after
