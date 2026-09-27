"""
为长时间容量验收记录有界、无内容的事务阶段，失败清理之前保留可定位证据。
"""

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from skill_lifecycle_live_support import LifecycleReports
from skill_takeover_support import TakeoverHarness
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from agent_remote_server.models import Node
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_storage import SkillContentUpload, SkillStorageUsage
from agent_remote_server.models.skill_terminations import SkillSnapshotTermination


@asynccontextmanager
async def observe_pipeline(
    state: TakeoverHarness, reports: LifecycleReports
) -> AsyncIterator[None]:
    """
    每分钟独立读取最小事务元数据，结束时再次记录，不改变生产请求或资源预算。

    :param state (TakeoverHarness): 本次独立数据库与账户身份
    :param reports (LifecycleReports): 已有无凭据 HTTP 路由计数
    :return AsyncIterator[None]: 拥有并回收观察任务的上下文
    """

    async def observe() -> None:
        """
        仅在拥有者存活期间读取，不因瞬时数据库观察失败终止容量工作。
        """
        while True:
            await emit_pipeline_observation(state, reports)
            await asyncio.sleep(60)

    task = asyncio.create_task(observe())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await emit_pipeline_observation(state, reports)


async def emit_pipeline_observation(state: TakeoverHarness, reports: LifecycleReports) -> None:
    """
    只输出状态、计数及配额数字，不读取清单、令牌、对象正文或持久化身份。

    :param state (TakeoverHarness): 本次独立数据库与账户身份
    :param reports (LifecycleReports): 已完成请求的模板路由计数
    """
    routes = {
        key: count
        for key, count in sorted(reports.responses.items())[:128]
        if re.fullmatch(r"(?:GET|POST|PUT|DELETE) /[a-z0-9_/{}/-]{1,200} [1-5][0-9]{2}", key)
    }
    print("pipeline_http=" + json.dumps(routes, sort_keys=True), flush=True)
    try:
        async with asyncio.timeout(10), state.library.database() as session:
            finalizations = (
                await session.execute(
                    select(SkillFinalization.status, SkillFinalization.unclean, func.count())
                    .where(SkillFinalization.user_id == state.library.owner)
                    .group_by(SkillFinalization.status, SkillFinalization.unclean)
                )
            ).all()
            captures = (
                await session.execute(
                    select(SkillSnapshotTermination.capture_error, func.count())
                    .join(
                        SessionSkillSnapshot,
                        SessionSkillSnapshot.id == SkillSnapshotTermination.snapshot_id,
                    )
                    .where(SessionSkillSnapshot.user_id == state.library.owner)
                    .group_by(SkillSnapshotTermination.capture_error)
                )
            ).all()
            uploads = (
                await session.execute(
                    select(SkillContentUpload.status, func.count())
                    .where(SkillContentUpload.user_id == state.library.owner)
                    .group_by(SkillContentUpload.status)
                )
            ).all()
            usage = (
                await session.execute(
                    select(SkillStorageUsage.state_bytes, SkillStorageUsage.state_reserved).where(
                        SkillStorageUsage.user_id == state.library.owner
                    )
                )
            ).one_or_none()
            node = (
                await session.execute(
                    select(Node.status, Node.last_heartbeat_at, Node.runtime_capabilities).where(
                        Node.id == state.node
                    )
                )
            ).one_or_none()
        summary = {
            "finalizations": [
                [bounded_state(status), bool(unclean), int(count)]
                for status, unclean, count in finalizations
            ],
            "capture_errors": [[bounded_state(code), int(count)] for code, count in captures],
            "uploads": [[bounded_state(status), int(count)] for status, count in uploads],
            "state_bytes": int(usage[0]) if usage else 0,
            "state_reserved": int(usage[1]) if usage else 0,
            "node": readiness_observation(*node) if node else None,
        }
        print("pipeline_database=" + json.dumps(summary, sort_keys=True), flush=True)
    except (TimeoutError, SQLAlchemyError):
        print("pipeline_database=observation_unavailable", flush=True)


def readiness_observation(
    status: str, heartbeat: datetime | None, capabilities: dict[str, object]
) -> dict[str, object]:
    """
    只投影有限节点状态、报告年龄及能力布尔值，不回显探测错误或原始元数据。

    :param status (str): 原节点状态
    :param heartbeat (datetime | None): 最后成功心跳时间
    :param capabilities (dict[str, object]): 原节点能力报告
    :return dict[str, object]: 无身份和内容的准入诊断
    """
    backends = capabilities.get("backends")
    errors = capabilities.get("probe_errors")
    skills = capabilities.get("skill_manager")
    age = None
    if heartbeat is not None:
        stamp = heartbeat if heartbeat.tzinfo else heartbeat.replace(tzinfo=UTC)
        age = max(0, int((datetime.now(UTC) - stamp).total_seconds()))
    return {
        "status": status if status in {"healthy", "degraded", "offline"} else "unknown",
        "heartbeat_age_seconds": age,
        "native_reported": isinstance(backends, list) and "native" in backends,
        "native_skill_reported": isinstance(skills, dict)
        and isinstance(skills.get("native"), dict),
        "probe_error_count": len(errors) if isinstance(errors, list) else None,
    }


def bounded_state(value: str | None) -> str:
    """
    只允许固定状态词进入测试日志，未知或损坏值不回显。

    :param value (str | None): 数据库中的状态或捕获错误码
    :return str: 无内容的有限状态标签
    """
    if value is None:
        return "none"
    if value in {
        "upload_pending",
        "persisted",
        "persisted_unclean",
        "published",
        "conflicted",
        "detached",
        "superseded",
        "staged",
        "committed",
        "complete",
        "completed",
        "expired",
        "quota_exceeded",
        "insufficient_storage",
        "portability_error",
        "capture_failed",
    }:
        return value
    return "unknown"
