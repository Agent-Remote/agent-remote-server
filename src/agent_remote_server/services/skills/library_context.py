"""
提供技能库事务共享的归属、内容格式和版本校验。
"""

import re
from collections import deque
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import yaml
from yaml.tokens import AliasToken, AnchorToken

from agent_remote_server.models import ToolAccount
from agent_remote_server.models.skill_library import (
    SkillActivation,
    SkillInstallation,
    SkillRevision,
    SkillSourceObservation,
)
from agent_remote_server.models.skill_local import AccountLocalSkill
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.schemas.skill_library import SkillInstallItem, SkillProvenance, SkillScope
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.content_admission import require_available_files
from agent_remote_server.services.tool_registry import ToolRegistry
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


@dataclass
class LibraryChange:
    """
    一次原子用户库命令的确定结果和部署范围。
    """

    skills: list[SkillInstallation] = field(default_factory=list)
    local_skills: list[AccountLocalSkill] = field(default_factory=list)
    revisions: list[SkillRevision] = field(default_factory=list)
    changed: bool = False
    scope: SkillScope = field(default_factory=SkillScope)
    warnings: list[str] = field(default_factory=list)
    deploy: bool = True


@dataclass
class LibraryContext:
    """
    已取得用户锁的单次命令上下文。
    """

    repository: SkillLibraryRepository
    store: PrivateObjectStore
    user_id: UUID
    generation: int

    async def require_skill(self, identifier: str, *, active: bool = True) -> SkillInstallation:
        """
        解析当前用户条目，并按命令要求检查卸载状态。

        :param identifier (str): 名称或稳定标识
        :param active (bool): 是否要求当前安装仍有效
        :return SkillInstallation: 已授权的安装记录
        """
        item = await self.resolve_source(identifier, None)
        if isinstance(item, AccountLocalSkill):
            raise SkillContentError(
                "LOCAL_SKILL_SCOPE_REQUIRED", "local skill requires its account"
            )
        if active and item.removed:
            raise SkillContentError("SKILL_REMOVED", "skill is uninstalled")
        return item

    async def resolve_source(
        self, identifier: str, scope: SkillScope | None
    ) -> SkillInstallation | AccountLocalSkill:
        """
        统一库与本地来源消歧，禁止通过名称静默扩大账户范围。

        :param identifier (str): 原始名称或稳定身份
        :param scope (SkillScope | None): 已验证的命令范围
        :return SkillInstallation | AccountLocalSkill: 精确已授权来源
        """
        account_id = scope.account_id if scope is not None else None
        library = await self.repository.installation(self.user_id, identifier)
        locals_ = await self.repository.local.matching(self.user_id, identifier, account_id)
        if len(locals_) > 1 or (library is not None and locals_):
            raise SkillContentError("SKILL_SOURCE_CONFLICT", "ambiguous name; use stable skill ID")
        if library is not None:
            return library
        if locals_:
            if account_id is None:
                raise SkillContentError(
                    "LOCAL_SKILL_SCOPE_REQUIRED", "local skill requires its account"
                )
            return locals_[0]
        raise SkillContentError("SKILL_NOT_FOUND", "skill not found")

    async def require_revision(self, item: SkillInstallation, selector: str) -> SkillRevision:
        """
        验证版本保留状态和用户、技能归属。

        :param item (SkillInstallation): 已授权安装
        :param selector (str): 版本编号或稳定标识
        :return SkillRevision: 可被固定或恢复的版本
        """
        revision = await self.repository.revision(self.user_id, item.id, selector)
        if revision is None:
            raise SkillContentError("REVISION_NOT_FOUND", "revision does not belong to this skill")
        if not revision.retained or revision.tree_digest is None:
            raise SkillContentError("REVISION_EXPIRED", "revision content has expired")
        tree = await self.repository.storage.tree(self.user_id, "package", revision.tree_digest)
        if tree is None:
            raise SkillContentError("REVISION_EXPIRED", "revision content has expired")
        manifest = SkillTreeManifest.model_validate(tree.manifest_json)
        await require_available_files(
            self.repository.storage,
            self.user_id,
            {entry.sha256 for entry in manifest.entries if entry.kind == "file"},
        )
        return revision

    async def validate_scope(self, scope: SkillScope) -> ToolAccount | None:
        """
        未接入工具和其他用户的账户不能成为规则目标。

        :param scope (SkillScope): 请求范围
        :return ToolAccount | None: 账户范围下的已授权账户
        """
        supported = ToolRegistry.supported_tool_types()
        for tool in scope.tools:
            if tool not in supported:
                raise SkillContentError("UNSUPPORTED_TOOL", "tool adapter is not registered")
        if scope.account_id is None:
            return None
        account = await self.repository.account(self.user_id, scope.account_id)
        if account is None:
            raise SkillContentError("ACCOUNT_NOT_FOUND", "tool account not found")
        if account.tool_type not in supported:
            raise SkillContentError("UNSUPPORTED_TOOL", "account tool adapter is not registered")
        return account

    async def validate_package(self, candidate: SkillInstallItem) -> dict[str, object]:
        """
        只接受当前用户完整已提交包，并从真实字节独立校验工具格式。

        :param candidate (SkillInstallItem): 本次安装候选
        :return dict[str, object]: 已校验的非执行格式元数据
        """
        tree = await self.repository.storage.tree(self.user_id, "package", candidate.tree_digest)
        if tree is None:
            raise SkillContentError(
                "CONTENT_NOT_FOUND", "complete package is not owned by this user"
            )
        manifest = SkillTreeManifest.model_validate(tree.manifest_json)
        await require_available_files(
            self.repository.storage,
            self.user_id,
            {entry.sha256 for entry in manifest.entries if entry.kind == "file"},
        )
        if any(entry.kind == "runtime_link" for entry in manifest.entries):
            raise SkillContentError(
                "INVALID_PACKAGE", "installation cannot contain runtime dependencies"
            )
        await self.store.verify_manifest(self.user_id, manifest)
        entry = _skill_document(manifest)
        content = await self.store.read_prefix(self.user_id, entry, 65_544)
        return _metadata(content, candidate.name)

    async def validate_observation(
        self, item: SkillInstallation, candidate: SkillInstallItem
    ) -> None:
        """
        重装和更新均拒绝已知标签的提交漂移。

        :param item (SkillInstallation): 相同来源的安装记录
        :param candidate (SkillInstallItem): 此次完整来源观测
        """
        if candidate.provenance.ref_kind != "tag":
            return
        for observation in await self.repository.observations(item):
            old = SkillProvenance.model_validate(observation.provenance_json)
            if (
                old.ref_kind == "tag"
                and old.ref == candidate.provenance.ref
                and old.commit != candidate.provenance.commit
            ):
                raise SkillContentError("SOURCE_DRIFT", "tag moved to a different commit")

    async def register_revision(
        self, item: SkillInstallation, candidate: SkillInstallItem, metadata: dict[str, object]
    ) -> tuple[SkillRevision, bool]:
        """
        相同内容复用原始 provenance，所有新来源观测独立保存。

        :param item (SkillInstallation): 已授权安装
        :param candidate (SkillInstallItem): 本次候选内容
        :param metadata (dict[str, object]): 从实际字节解析的元数据
        :return tuple[SkillRevision, bool]: 登记版本及是否新增版本
        """
        revision = await self.repository.content_revision(
            self.user_id, item.id, candidate.tree_digest
        )
        created = revision is None
        if revision is None:
            revision = SkillRevision(
                id=uuid4(),
                user_id=self.user_id,
                installation_id=item.id,
                number=await self.repository.next_revision_number(self.user_id, item.id),
                category="package",
                content_digest=candidate.tree_digest,
                tree_digest=candidate.tree_digest,
                provenance_json=candidate.provenance.model_dump(mode="json"),
                metadata_json=metadata,
                retained=True,
            )
            self.repository.add(revision)
            await self.repository.flush()
        elif not revision.retained:
            raise SkillContentError("REVISION_EXPIRED", "identical historical revision has expired")
        self.repository.add(
            SkillSourceObservation(
                user_id=self.user_id,
                installation_id=item.id,
                revision_id=revision.id,
                provenance_json=candidate.provenance.model_dump(mode="json"),
            )
        )
        return revision, created

    def activate(self, item: SkillInstallation, revision: SkillRevision) -> bool:
        """
        只有默认版本实际变化才进入可回滚激活历史。

        :param item (SkillInstallation): 已授权安装
        :param revision (SkillRevision): 同技能完整版本
        :return bool: 默认版本是否实际改变
        """
        if item.default_revision_id == revision.id:
            return False
        item.default_revision_id = revision.id
        self.repository.add(
            SkillActivation(
                user_id=self.user_id,
                installation_id=item.id,
                revision_id=revision.id,
                generation=self.generation + 1,
            )
        )
        return True


def _skill_document(manifest: SkillTreeManifest, path: str = "SKILL.md") -> SkillTreeEntry:
    """
    在完整清单内解析入口链接，绝不沿宿主文件系统读取。

    :param manifest (SkillTreeManifest): 已完整验证的来源树
    :param path (str): 在完整目录内解析的说明文件路径
    :return SkillTreeEntry: 最终普通文本文件
    """
    entries = {entry.path: entry for entry in manifest.entries}
    pending = deque(path.split("/"))
    resolved: list[str] = []
    hops = 0
    selected: SkillTreeEntry | None = None
    while pending:
        component = pending.popleft()
        if component in {"", "."}:
            continue
        if component == "..":
            if not resolved:
                raise SkillContentError("INVALID_SKILL_FORMAT", "skill entry escapes package")
            resolved.pop()
            continue
        selected = entries.get("/".join([*resolved, component]))
        if selected is None:
            raise SkillContentError("INVALID_SKILL_FORMAT", "SKILL.md is missing")
        if selected.kind == "symlink":
            hops += 1
            if hops > 40:
                raise SkillContentError("INVALID_SKILL_FORMAT", "skill entry link cycle")
            pending.extendleft(reversed(selected.target.split("/")))
        else:
            resolved.append(component)
    if selected is None or selected.kind != "file" or selected.content_kind != "text":
        raise SkillContentError(
            "INVALID_SKILL_FORMAT", "SKILL.md must resolve to a UTF-8 text file"
        )
    return selected


def _metadata(content: bytes, name: str) -> dict[str, object]:
    """
    有界解析 Claude 可选 frontmatter，原始内容始终不被改写。

    :param content (bytes): 原始说明文件字节
    :param name (str): 来源发现阶段选定的名称
    :return dict[str, object]: 供清单展示的名称与描述
    """
    # 前缀可能正好截在多字节字符中间；完整对象已通过 UTF-8 校验，只丢弃尾部残字节。
    document = content.decode("utf-8", errors="ignore").removeprefix("\ufeff").replace("\r\n", "\n")
    body = document
    metadata: dict[str, object] = {}
    if document.startswith("---\n"):
        closing = re.search(r"(?m)^---(?:\n|$)", document[4:])
        if closing is None or closing.start() > 65_536:
            raise SkillContentError(
                "INVALID_SKILL_FORMAT", "frontmatter is incomplete or too large"
            )
        header = document[4 : 4 + closing.start()]
        try:
            if any(isinstance(token, (AliasToken, AnchorToken)) for token in yaml.scan(header)):
                raise ValueError("aliases are not skill metadata")
            parsed = yaml.safe_load(header)
            if parsed is None:
                parsed = {}
            if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
                raise ValueError("frontmatter must be a mapping")
            metadata = parsed
        except (yaml.YAMLError, ValueError, RecursionError) as error:
            raise SkillContentError("INVALID_SKILL_FORMAT", "invalid skill frontmatter") from error
        body = document[4 + closing.end() :]
    if metadata.get("name", name) != name:
        raise SkillContentError(
            "SOURCE_LAYOUT_CHANGED", "declared skill name differs from selected name"
        )
    description = metadata.get("description", body.strip().split("\n", 1)[0][:1024])
    if not isinstance(description, str) or len(description) > 1024:
        raise SkillContentError("INVALID_SKILL_FORMAT", "description must be bounded text")
    return {"name": name, "description": description}
