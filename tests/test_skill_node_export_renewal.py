"""
验证长时间导出的独立续授权契约，不复活过期凭据或替换原始用户身份。
"""

import hashlib
from datetime import UTC, datetime, timedelta, tzinfo
from typing import ClassVar, Self
from uuid import UUID

import pytest
from httpx import AsyncClient
from skill_node_export_support import ExportHarness
from skill_node_export_support import export_client as export_client
from skill_node_export_support import export_state as export_state
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_finalization import stopped as stopped
from test_skill_node_export import authorize
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import AuthToken, NodeTask
from agent_remote_server.models.skill_snapshots import SkillFinalization
from agent_remote_server.models.skill_storage import SkillContentUpload
from agent_remote_server.models.skill_terminations import SkillSnapshotTermination
from agent_remote_server.repositories.skill_node_export import ExportRows, NodeExportRepository
from agent_remote_server.schemas.skill_node_export import NodeExportVerification
from agent_remote_server.services.skills import node_export
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.node_export import NodeExportService
from agent_remote_server.services.skills.node_export_tokens import NodeExportTokens


class ExportClock(datetime):
    """
    只推进导出服务的观察时刻，不等待真实十五分钟或改变认证依赖。
    """

    current: ClassVar[datetime]

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        """
        返回当前受控时刻并保持调用方的时区约定。

        :param tz (tzinfo | None): 调用方要求的时区
        :return Self: 本次测试选择的时刻
        """
        return cls.fromtimestamp(cls.current.timestamp(), tz)


async def test_node_renews_live_grant_without_mutating_content_or_key_tasks(
    export_state: ExportHarness, export_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    实际路由跨过原凭据截止时间仍保持同一身份，原凭据自身不被延长。

    :param export_state (ExportHarness): 原始身份与独立存储
    :param export_client (AsyncClient): 保留真实认证的客户端
    :param monkeypatch (pytest.MonkeyPatch): 仅替换服务观察时钟
    """
    auth = await authorize(export_state)
    tokens = NodeExportTokens(export_state.settings.secret_key)
    now = int(datetime.now(UTC).timestamp())
    original = tokens.verify(auth.grant, now)
    ExportClock.current = datetime.fromtimestamp(original.expires_at - 30, UTC)
    monkeypatch.setattr(node_export, "datetime", ExportClock)
    state = export_state.state
    async with state.database.begin() as session:
        before = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (NodeTask, SkillFinalization, SkillContentUpload)
        ]
        session.add(
            SkillSnapshotTermination(
                snapshot_id=state.snapshot, incoming_digest="a" * 64, unclean=True
            )
        )
    response = await export_client.post(
        f"/api/v1/node/skill-state-exports/{state.snapshot}/renew",
        headers={"Authorization": "Bearer " + export_state.node_token},
        json=export_state.request.model_dump(mode="json") | {"grant": auth.grant},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "authorized" and result["committed"] is False
    data = result["data"]
    assert data["previous_grant_digest"] == hashlib.sha256(auth.grant.encode()).hexdigest()
    successor = tokens.verify(data["grant"], original.expires_at + 1)
    assert successor.model_dump(exclude={"issued_at", "expires_at"}) == original.model_dump(
        exclude={"issued_at", "expires_at"}
    )
    assert successor.expires_at - successor.issued_at == 900
    assert successor.expires_at > original.expires_at
    permission = data["permission"]
    assert permission["binding"] == auth.binding.model_dump(mode="json")
    assert permission["incoming_digest"] == "a" * 64 and permission["unclean"] is True
    assert int(datetime.fromisoformat(permission["expires_at"]).timestamp()) == successor.expires_at
    assert permission["recheck_seconds"] == 10
    with pytest.raises(SkillContentError):
        tokens.verify(auth.grant, original.expires_at)
    ExportClock.current = datetime.fromtimestamp(original.expires_at + 1, UTC)
    async with state.database() as session:
        service = NodeExportService(session, export_state.settings)
        request = NodeExportVerification(**export_state.request.model_dump(), grant=data["grant"])
        assert (await service.verify(state.node, state.snapshot, request)).binding == auth.binding
        assert [
            await session.scalar(select(func.count()).select_from(model))
            for model in (NodeTask, SkillFinalization, SkillContentUpload)
        ] == before


async def test_renewal_cannot_outlive_original_user_token(
    export_state: ExportHarness,
) -> None:
    """
    后来缩短的原用户令牌期限仍约束续授权，不创建新的用户凭据。

    :param export_state (ExportHarness): 原始用户及快照
    """
    auth = await authorize(export_state)
    expiry = datetime.now(UTC) + timedelta(seconds=45)
    async with export_state.state.database.begin() as session:
        token = await session.get(AuthToken, export_state.token_id)
        assert token is not None
        token.expires_at = expiry
    async with export_state.state.database() as session:
        renewed = await NodeExportService(session, export_state.settings).renew(
            export_state.state.node,
            export_state.state.snapshot,
            NodeExportVerification(**export_state.request.model_dump(), grant=auth.grant),
        )
    assert int(renewed.permission.expires_at.timestamp()) == int(expiry.timestamp())
    grant = NodeExportTokens(export_state.settings.secret_key).verify(
        renewed.grant, int(datetime.now(UTC).timestamp())
    )
    assert grant.token_id == export_state.token_id
    assert grant.expires_at == int(expiry.timestamp())


async def test_expired_original_grant_cannot_renew(
    export_state: ExportHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    即使用户身份仍活跃，原授权失效后也必须重新由用户发起授权。

    :param export_state (ExportHarness): 原始用户及快照
    :param monkeypatch (pytest.MonkeyPatch): 服务观察时钟替换
    """
    auth = await authorize(export_state)
    ExportClock.current = auth.expires_at
    monkeypatch.setattr(node_export, "datetime", ExportClock)
    async with export_state.state.database() as session:
        with pytest.raises(SkillContentError, match="authorization denied"):
            await NodeExportService(session, export_state.settings).renew(
                export_state.state.node,
                export_state.state.snapshot,
                NodeExportVerification(**export_state.request.model_dump(), grant=auth.grant),
            )


@pytest.mark.parametrize("expires", ["grant", "user_token"])
async def test_authority_expiring_during_database_observation_cannot_renew(
    export_state: ExportHarness, monkeypatch: pytest.MonkeyPatch, expires: str
) -> None:
    """
    数据库观察结束时再次检查期限，不把请求开始时的有效性当作签发权限。

    :param export_state (ExportHarness): 原始授权和独立存储
    :param monkeypatch (pytest.MonkeyPatch): 服务时钟与数据库观察边界替换
    :param expires (str): 在观察期间失效的原权限
    """
    auth = await authorize(export_state)
    if expires == "grant":
        expiry = auth.expires_at
    else:
        expiry = datetime.now(UTC) + timedelta(seconds=20)
        async with export_state.state.database.begin() as session:
            token = await session.get(AuthToken, export_state.token_id)
            assert token is not None
            token.expires_at = expiry
    ExportClock.current = expiry - timedelta(seconds=5)
    monkeypatch.setattr(node_export, "datetime", ExportClock)
    snapshot = NodeExportRepository.snapshot

    async def observe(
        repository: NodeExportRepository, user_id: UUID, snapshot_id: UUID
    ) -> ExportRows | None:
        """
        保留真实查询结果，仅在原观察完成后推进时间至权限失效。

        :param repository (NodeExportRepository): 原始请求仓储
        :param user_id (UUID): 原授权用户
        :param snapshot_id (UUID): 原快照
        :return ExportRows | None: 未改写的原始数据库结果
        """
        rows = await snapshot(repository, user_id, snapshot_id)
        ExportClock.current = expiry + timedelta(seconds=1)
        return rows

    monkeypatch.setattr(NodeExportRepository, "snapshot", observe)
    async with export_state.state.database() as session:
        with pytest.raises(SkillContentError, match="authorization denied"):
            await NodeExportService(session, export_state.settings).renew(
                export_state.state.node,
                export_state.state.snapshot,
                NodeExportVerification(**export_state.request.model_dump(), grant=auth.grant),
            )
