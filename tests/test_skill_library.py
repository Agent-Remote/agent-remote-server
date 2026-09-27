"""
验证用户库的原子安装、来源历史、字段继承、纪元和并发代数契约。
"""

import asyncio
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_skill_content_service import database as database
from test_skill_content_service import service as content_service
from test_skill_content_service import user
from test_skill_storage import file_entry

from agent_remote_server.models import ToolAccount
from agent_remote_server.models.skill_library import (
    SkillAccountOverride,
    SkillInstallationEpoch,
    SkillOperation,
    SkillSourceObservation,
)
from agent_remote_server.schemas.skill_library import (
    SkillAddRequest,
    SkillInstallItem,
    SkillLibraryRequest,
    SkillProvenance,
    SkillRemoveRequest,
    SkillRollbackRequest,
    SkillRuleRequest,
    SkillScope,
    SkillSource,
    SkillUpdateRequest,
)
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_results import (
    SkillInstallationView,
    SkillMutationData,
    SkillResult,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.library import SkillLibraryService
from agent_remote_server.services.tool_registry import ToolRegistry, ToolRuntimeTemplate
from agent_remote_server.skill_manager.manifest import manifest_digest
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


@dataclass
class LibraryHarness:
    """
    每个请求使用独立事务，模拟重启后只依赖数据库和内容卷。
    """

    database: async_sessionmaker[AsyncSession]
    root: Path
    owner: UUID

    def service(self, session: AsyncSession) -> SkillLibraryService:
        """
        构造不保存内存权威状态的服务。

        :param session (AsyncSession): 请求事务
        :return SkillLibraryService: 用户库服务
        """
        return SkillLibraryService(session, PrivateObjectStore(self.root / "objects"))

    async def candidate(
        self,
        name: str = "learning",
        version: str = "one",
        *,
        origin: str = "local",
        content: bytes | None = None,
    ) -> SkillInstallItem:
        """
        通过完整上传协议准备真实字节，来源身份不依赖版本内容。

        :param name (str): 工具名称
        :param version (str): 内容版本
        :param origin (str): 本地来源种子或 Git 来源类型
        :param content (bytes | None): 可选格式测试字节
        :return SkillInstallItem: 当前用户已上传的候选
        """
        data = (
            content
            if content is not None
            else f"---\nname: {name}\ndescription: 学习\n---\n{version}\n".encode()
        )
        manifest = SkillTreeManifest(entries=(file_entry(data),))
        async with self.database.begin() as session:
            svc = content_service(session, self.root)
            upload = await svc.begin(self.owner, str(uuid4()), manifest, "package")
            await svc.put_file(self.owner, upload.id, manifest.entries[0].sha256, io.BytesIO(data))
            await svc.complete(self.owner, upload.id)
        source = SkillSource(
            kind="local", locator=hashlib.sha256(f"{origin}:{name}".encode()).hexdigest()
        )
        provenance = SkillProvenance(ref_kind="local")
        if origin == "git":
            source = SkillSource(
                kind="git", locator="https://github.com/example/skills.git", subpath=name
            )
            provenance = SkillProvenance(
                ref_kind="branch", ref="main", commit=hashlib.sha1(version.encode()).hexdigest()
            )
        return SkillInstallItem(
            name=name, source=source, provenance=provenance, tree_digest=manifest_digest(manifest)
        )

    async def execute(self, request: SkillLibraryRequest) -> SkillResult[SkillMutationData]:
        """
        执行并提交单次请求，失败自动回滚。

        :param request (SkillLibraryRequest): 用户库命令
        :return SkillResult[SkillMutationData]: 持久化受理结果
        """
        async with self.database.begin() as session:
            return await self.service(session).execute(self.owner, request)

    async def generation(self) -> int:
        """
        模拟 CLI 重新取得用户库代数。

        :return int: 当前代数
        """
        async with self.database.begin() as session:
            return (await self.service(session).list_skills(self.owner)).generation

    async def info(
        self, skill: str = "learning", account: UUID | None = None
    ) -> SkillInstallationView:
        """
        读取技能及可选账户字段解析。

        :param skill (str): 名称或稳定标识
        :param account (UUID | None): 可选目标账户
        :return SkillInstallationView: 当前持久化详情
        """
        async with self.database.begin() as session:
            view = await self.service(session).info(
                self.owner,
                skill,
                scope=SkillScope(account_id=account) if account is not None else None,
            )
            assert isinstance(view, SkillInstallationView)
            return view

    async def add(
        self, *items: SkillInstallItem, scope: SkillScope | None = None
    ) -> SkillResult[SkillMutationData]:
        """
        执行明确范围的批量安装。

        :param items (SkillInstallItem): 全部候选条目
        :param scope (SkillScope | None): 可选显式范围
        :return SkillResult[SkillMutationData]: 原子安装结果
        """
        return await self.execute(
            SkillAddRequest(
                idempotency_key=str(uuid4()),
                expected_generation=await self.generation(),
                items=items,
                scope=scope or SkillScope(),
                scope_explicit=scope is not None,
            )
        )

    async def account(self, tool: str = "claude") -> UUID:
        """
        新账户只属于本用户，不拷贝其他账户状态。

        :param tool (str): 测试工具类型
        :return UUID: 新账户标识
        """
        identity = uuid4()
        async with self.database.begin() as session:
            session.add(
                ToolAccount(
                    id=identity,
                    user_id=self.owner,
                    tool_type=tool,
                    display_name="测试账户",
                    status="binding_requested",
                    region_code="global",
                    timezone="UTC",
                    locale="en-US",
                    preferred_node_tags=[],
                )
            )
        return identity


@pytest.fixture
async def library(database: async_sessionmaker[AsyncSession], tmp_path: Path) -> LibraryHarness:
    """
    为每个场景准备独立用户身份。

    :param database (async_sessionmaker[AsyncSession]): 数据库事务工厂
    :param tmp_path (Path): 临时内容卷
    :return LibraryHarness: 用户库测试入口
    """
    return LibraryHarness(database, tmp_path, await user(database))


async def test_add_is_atomic_and_replay_does_not_reset_rules(library: LibraryHarness) -> None:
    """
    幂等重试复用原操作，普通重复添加既不重置覆盖也不增加版本。

    :param library (LibraryHarness): 用户库入口
    """
    candidate = await library.candidate()
    request = SkillAddRequest(idempotency_key="original", expected_generation=0, items=(candidate,))
    first = await library.execute(request)
    replay = await library.execute(request)
    assert first == replay and first.status == "stored" and first.committed
    assert first.data.generation == 1
    assert not (await library.add(candidate)).data.changed
    assert len((await library.info()).revisions) == 1
    rule = SkillRuleRequest(
        command="disable", skill="learning", idempotency_key="disabled", expected_generation=1
    )
    await library.execute(rule)
    with pytest.raises(SkillContentError) as error:
        await library.add(candidate)
    assert error.value.code == "SCOPE_CONFLICT"
    assert not (await library.info()).default_enabled
    assert await library.execute(request) == first
    with pytest.raises(SkillContentError) as error:
        await library.execute(request.model_copy(update={"expected_generation": 2}))
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


async def test_batch_conflict_leaves_no_partial_installation(library: LibraryHarness) -> None:
    """
    第二项同名异源冲突时，第一项也不能留下安装或操作记录。

    :param library (LibraryHarness): 用户库入口
    """
    await library.add(await library.candidate())
    fresh = await library.candidate("fresh")
    conflicting = await library.candidate(origin="different")
    with pytest.raises(SkillContentError) as error:
        await library.add(fresh, conflicting)
    assert error.value.code == "SKILL_SOURCE_CONFLICT"
    with pytest.raises(SkillContentError):
        await library.info("fresh")
    assert await library.generation() == 1
    async with library.database() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(SkillOperation)
            .where(SkillOperation.user_id == library.owner)
        )
        assert count == 1


async def test_default_all_tools_inherits_future_adapter(
    library: LibraryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    默认范围不被永久展开成当前 Claude 列表，明确限定范围不扩大。

    :param library (LibraryHarness): 用户库入口
    :param monkeypatch (pytest.MonkeyPatch): 工具注册表替换
    """
    await library.add(await library.candidate())
    await library.add(await library.candidate("scoped"), scope=SkillScope(tools=("claude",)))
    template = ToolRuntimeTemplate("test-tool", "test", ["test"], "test", "test")
    monkeypatch.setitem(ToolRegistry._templates, "test-tool", template)
    account = await library.account("test-tool")
    default = (await library.info(account=account)).effective
    scoped = (await library.info("scoped", account)).effective
    assert default is not None and default.included and default.enabled_source == "user"
    assert scoped is not None and not scoped.enabled


async def test_stage_pin_field_inheritance_and_all_scopes(library: LibraryHarness) -> None:
    """
    候选只供指定账户试用，启用和版本独立继承，全范围停用保留 pin。

    :param library (LibraryHarness): 用户库入口
    """
    a, b = await library.account(), await library.account()
    original = await library.candidate()
    await library.add(original)
    before = await library.info()
    staged = await library.candidate(version="two")
    result = await library.execute(
        SkillUpdateRequest(
            skill="learning",
            item=staged,
            stage=True,
            idempotency_key="stage",
            expected_generation=1,
        )
    )
    candidate_id = result.data.revision_ids[0]
    assert (await library.info()).default_revision_id == before.default_revision_id
    assert (await library.info()).tracking == before.tracking
    assert not result.data.targets
    await library.execute(
        SkillRuleRequest(
            command="pin",
            skill="learning",
            revision="r2",
            scope=SkillScope(account_id=a),
            idempotency_key="pin",
            expected_generation=2,
        )
    )
    await library.execute(
        SkillRuleRequest(
            command="disable",
            skill="learning",
            scope=SkillScope(tools=("claude",)),
            idempotency_key="tool-disable",
            expected_generation=3,
        )
    )
    await library.execute(
        SkillRuleRequest(
            command="enable",
            skill="learning",
            scope=SkillScope(account_id=a),
            idempotency_key="account-enable",
            expected_generation=4,
        )
    )
    first, second = (
        (await library.info(account=a)).effective,
        (await library.info(account=b)).effective,
    )
    assert first is not None and first.included and first.revision_id == candidate_id
    assert first.enabled_source == "account" and first.revision_source == "account"
    assert (
        second is not None
        and not second.enabled
        and second.revision_id == before.default_revision_id
    )
    await library.execute(
        SkillRuleRequest(
            command="inherit",
            skill="learning",
            field="enabled",
            scope=SkillScope(account_id=a),
            idempotency_key="inherit",
            expected_generation=5,
        )
    )
    inherited = (await library.info(account=a)).effective
    assert inherited is not None and not inherited.enabled and inherited.revision_id == candidate_id
    await library.execute(
        SkillRuleRequest(
            command="disable",
            skill="learning",
            all_scopes=True,
            idempotency_key="all-disabled",
            expected_generation=6,
        )
    )
    final = await library.info(account=a)
    assert final.effective is not None and not final.effective.enabled
    assert final.effective.enabled_source == "user" and final.effective.revision_source == "account"
    assert final.account_overrides[str(a)].enabled is None
    assert final.tool_overrides["claude"].enabled is None


async def test_rollback_uses_activation_history_not_revision_number(
    library: LibraryHarness,
) -> None:
    """
    r2 仅候选、r3 激活后回滚应选择 r1，不能错误选择相邻编号。

    :param library (LibraryHarness): 用户库入口
    """
    await library.add(await library.candidate(origin="git"))
    first = await library.info()
    candidate = await library.candidate(version="two", origin="git")
    await library.execute(
        SkillUpdateRequest(
            skill="learning",
            item=candidate,
            stage=True,
            idempotency_key="stage",
            expected_generation=1,
        )
    )
    third = await library.candidate(version="three", origin="git")
    await library.execute(
        SkillUpdateRequest(
            skill="learning", item=third, idempotency_key="activate-third", expected_generation=2
        )
    )
    rollback = await library.execute(
        SkillRollbackRequest(skill="learning", idempotency_key="back", expected_generation=3)
    )
    assert rollback.data.revision_ids == [first.default_revision_id]
    assert (await library.info()).tracking["ref_kind"] == "fixed"
    with pytest.raises(SkillContentError) as error:
        await library.execute(
            SkillUpdateRequest(
                skill="learning", item=third, idempotency_key="implicit", expected_generation=4
            )
        )
    assert error.value.code == "SOURCE_PINNED"
    await library.execute(
        SkillUpdateRequest(
            skill="learning",
            item=candidate,
            switch_tracking=True,
            idempotency_key="activate-stage",
            expected_generation=4,
        )
    )
    info = await library.info()
    assert len(info.revisions) == 3 and info.default_revision_id == info.revisions[1].id
    assert info.tracking["ref_kind"] == "branch"


async def test_same_content_preserves_first_provenance_and_tag_drift_is_rejected(
    library: LibraryHarness,
) -> None:
    """
    新来源观测不重写旧 revision，移动标签不能被更新静默接受。

    :param library (LibraryHarness): 用户库入口
    """
    original = await library.candidate(origin="git")
    await library.add(original)
    observed = original.model_copy(
        update={"provenance": SkillProvenance(ref_kind="tag", ref="v1", commit="a" * 40)}
    )
    await library.execute(
        SkillUpdateRequest(
            skill="learning",
            item=observed,
            switch_tracking=True,
            idempotency_key="observe",
            expected_generation=1,
        )
    )
    info = await library.info()
    assert len(info.revisions) == 1 and info.revisions[0].provenance == original.provenance
    async with library.database() as session:
        observations = (
            await session.scalars(
                select(SkillSourceObservation).where(
                    SkillSourceObservation.user_id == library.owner
                )
            )
        ).all()
        assert len(observations) == 2
    drifted = observed.model_copy(
        update={"provenance": SkillProvenance(ref_kind="tag", ref="v1", commit="b" * 40)}
    )
    with pytest.raises(SkillContentError) as error:
        await library.execute(
            SkillUpdateRequest(
                skill="learning",
                item=drifted,
                switch_tracking=True,
                idempotency_key="drift",
                expected_generation=2,
            )
        )
    assert error.value.code == "SOURCE_DRIFT"


async def test_uninstall_reinstall_preserves_identity_rules_and_epochs(
    library: LibraryHarness,
) -> None:
    """
    同来源重装创建新 epoch 且保留覆盖，旧身份仍可用于后续归档收尾。

    :param library (LibraryHarness): 用户库入口
    """
    account = await library.account()
    candidate = await library.candidate()
    await library.add(candidate)
    original = await library.info()
    await library.execute(
        SkillRuleRequest(
            command="disable",
            skill="learning",
            scope=SkillScope(account_id=account),
            idempotency_key="disable",
            expected_generation=1,
        )
    )
    await library.execute(
        SkillRemoveRequest(skill="learning", idempotency_key="remove", expected_generation=2)
    )
    removed = await library.info(str(original.id), account)
    assert (
        removed.removed
        and removed.effective is not None
        and removed.effective.exclusion_reason == "removed"
    )
    with pytest.raises(SkillContentError) as error:
        await library.add(candidate, scope=SkillScope(tools=("claude",)))
    assert error.value.code == "SCOPE_CONFLICT"
    await library.add(candidate)
    restored = await library.info(account=account)
    assert restored.id == original.id and restored.epoch == 2
    assert restored.account_overrides[str(account)].enabled is False
    async with library.database() as session:
        old = await session.get(SkillInstallationEpoch, (library.owner, original.id, 1))
        assert old is not None and old.archived_at is not None
        assert (
            await session.get(SkillInstallationEpoch, (library.owner, original.id, 2)) is not None
        )


async def test_replacement_source_has_new_identity_and_old_id_remains_queryable(
    library: LibraryHarness,
) -> None:
    """
    同名异源重装不能复用旧来源的规则、版本或状态身份。

    :param library (LibraryHarness): 用户库入口
    """
    await library.add(await library.candidate())
    old = await library.info()
    await library.execute(
        SkillRemoveRequest(skill="learning", idempotency_key="remove", expected_generation=1)
    )
    await library.add(await library.candidate(origin="replacement"))
    current = await library.info()
    assert current.id != old.id and current.epoch == 1 and not current.account_overrides
    assert (await library.info(str(old.id))).removed


async def test_foreign_user_cannot_query_pin_or_install_known_content(
    library: LibraryHarness,
) -> None:
    """
    知道其他用户 skill、版本、账户、操作和树标识也不能引用它们。

    :param library (LibraryHarness): 用户库入口
    """
    own_account = await library.account()
    candidate = await library.candidate()
    operation = await library.add(candidate)
    info = await library.info()
    stranger = LibraryHarness(library.database, library.root, await user(library.database))
    with pytest.raises(SkillContentError) as error:
        await stranger.add(candidate)
    assert error.value.code == "CONTENT_NOT_FOUND"
    with pytest.raises(SkillContentError):
        await stranger.info(str(info.id))
    own = await stranger.candidate()
    await stranger.add(own)
    foreign_scope = SkillRuleRequest(
        command="enable",
        skill="learning",
        scope=SkillScope(account_id=own_account),
        idempotency_key="account",
        expected_generation=1,
    )
    with pytest.raises(SkillContentError) as error:
        await stranger.execute(foreign_scope)
    assert error.value.code == "ACCOUNT_NOT_FOUND"
    with pytest.raises(SkillContentError) as error:
        await stranger.execute(
            SkillRuleRequest(
                command="pin",
                skill="learning",
                scope=SkillScope(tools=("claude",)),
                revision=str(info.default_revision_id),
                idempotency_key="pin",
                expected_generation=1,
            )
        )
    assert error.value.code == "REVISION_NOT_FOUND"
    async with library.database.begin() as session:
        assert operation.operation_id is not None
        with pytest.raises(SkillContentError):
            await stranger.service(session).status(stranger.owner, operation.operation_id)


async def test_database_rejects_cross_owner_account_override(library: LibraryHarness) -> None:
    """
    直接绕过服务写入也会被账户复合外键拒绝。

    :param library (LibraryHarness): 用户库入口
    """
    await library.add(await library.candidate())
    info = await library.info()
    stranger = LibraryHarness(library.database, library.root, await user(library.database))
    foreign_account = await stranger.account()
    with pytest.raises(IntegrityError):
        async with library.database.begin() as session:
            session.add(
                SkillAccountOverride(
                    user_id=library.owner,
                    installation_id=info.id,
                    account_id=foreign_account,
                    tool_type="claude",
                    enabled=True,
                    revision_id=info.default_revision_id,
                )
            )


async def test_concurrent_generation_has_one_winner(library: LibraryHarness) -> None:
    """
    同代数两个不同安装请求只能提交一个，不能丢失配置更新。

    :param library (LibraryHarness): 用户库入口
    """
    first, second = await library.candidate("first"), await library.candidate("second")

    async def install(candidate: SkillInstallItem) -> str:
        """
        在独立连接竞争同一配置代数。

        :param candidate (SkillInstallItem): 安装候选
        :return str: 成功或稳定竞争错误码
        """
        try:
            await library.execute(
                SkillAddRequest(
                    idempotency_key=candidate.name, expected_generation=0, items=(candidate,)
                )
            )
            return "accepted"
        except SkillContentError as error:
            return error.code

    assert sorted(await asyncio.gather(install(first), install(second))) == [
        "GENERATION_CONFLICT",
        "accepted",
    ]
    assert await library.generation() == 1


@pytest.mark.parametrize(
    "document,code",
    [
        (b"---\nname: other\n---\nInstructions", "SOURCE_LAYOUT_CHANGED"),
        (b"---\nname: [bad]\n---\nInstructions", "SOURCE_LAYOUT_CHANGED"),
        (b"---\ndescription: [bad]\n---\nInstructions", "INVALID_SKILL_FORMAT"),
        (b"---\nname: learning\n", "INVALID_SKILL_FORMAT"),
    ],
)
async def test_invalid_package_metadata_never_creates_installation(
    library: LibraryHarness, document: bytes, code: str
) -> None:
    """
    实际 SKILL.md 校验失败时不把客户端声明当作可信格式。

    :param library (LibraryHarness): 用户库入口
    :param document (bytes): 无效格式内容
    :param code (str): 预期稳定错误码
    """
    candidate = await library.candidate(content=document)
    with pytest.raises(SkillContentError) as error:
        await library.add(candidate)
    assert error.value.code == code
    assert await library.generation() == 0


def test_ambiguous_rule_combinations_fail_before_execution() -> None:
    """
    pin 范围、all-scopes 和 inherit 字段组合必须在命令入口被拒绝。
    """
    common = {"skill": "learning", "idempotency_key": "request", "expected_generation": 0}
    for values in (
        {"command": "pin", "revision": "r1"},
        {"command": "enable", "all_scopes": True},
        {"command": "enable", "revision": "r1"},
        {"command": "disable", "field": "enabled"},
    ):
        with pytest.raises(ValidationError):
            SkillRuleRequest.model_validate(common | values)


async def test_read_only_queries_do_not_create_library_or_usage(library: LibraryHarness) -> None:
    """
    dry-run 所需的新用户查询不能留下任何库或计量写入。

    :param library (LibraryHarness): 用户库入口
    """
    from agent_remote_server.models.skill_library import SkillLibrary
    from agent_remote_server.models.skill_storage import SkillStorageUsage

    assert await library.generation() == 0
    with pytest.raises(SkillContentError):
        await library.info("absent")
    async with library.database() as session:
        assert await session.get(SkillLibrary, library.owner) is None
        assert await session.get(SkillStorageUsage, library.owner) is None


async def test_same_user_cannot_pin_another_skills_revision(library: LibraryHarness) -> None:
    """
    同一用户的另一个技能版本也不能用于当前条目固定。

    :param library (LibraryHarness): 用户库入口
    """
    await library.add(await library.candidate(), await library.candidate("other"))
    other = await library.info("other")
    with pytest.raises(SkillContentError) as error:
        await library.execute(
            SkillRuleRequest(
                command="pin",
                skill="learning",
                scope=SkillScope(tools=("claude",)),
                revision=str(other.default_revision_id),
                idempotency_key="cross-skill",
                expected_generation=1,
            )
        )
    assert error.value.code == "REVISION_NOT_FOUND"
    assert (await library.info()).tool_overrides == {}


async def test_update_layout_difference_is_explicit(library: LibraryHarness) -> None:
    """
    更新不能悄悄跟随上游改名和移动，必须返回可展示的路径差异。

    :param library (LibraryHarness): 用户库入口
    """
    original = await library.candidate(origin="git")
    await library.add(original)
    moved = original.model_copy(
        update={"source": original.source.model_copy(update={"subpath": "moved"})}
    )
    with pytest.raises(SkillContentError) as error:
        await library.execute(
            SkillUpdateRequest(
                skill="learning", item=moved, idempotency_key="moved", expected_generation=1
            )
        )
    assert error.value.code == "SOURCE_LAYOUT_CHANGED"
    assert error.value.details == {
        "expected": {"name": "learning", "subpath": "learning"},
        "actual": {"name": "learning", "subpath": "moved"},
    }
    assert await library.generation() == 1


@pytest.mark.parametrize(
    "document",
    [b"---\nname: learning\n---", b"---\n---\nPlain instructions", b"Plain instructions"],
)
async def test_optional_and_eof_frontmatter_preserves_original_package(
    library: LibraryHarness, document: bytes
) -> None:
    """
    支持工具的可选头部和文件末尾分隔符，解析不会重写原始文件。

    :param library (LibraryHarness): 用户库入口
    :param document (bytes): 有效的可选头部变体
    """
    candidate = await library.candidate(content=document)
    await library.add(candidate)
    assert (await library.info()).revisions[0].content_digest == candidate.tree_digest
