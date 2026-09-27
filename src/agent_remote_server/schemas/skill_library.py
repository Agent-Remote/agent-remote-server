"""
定义技能来源、安装与版本规则变更的严格命令契约。
"""

import hashlib
import re
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_remote_server.schemas.skill_manifest import validate_relative_path


def validate_skill_name(value: str) -> str:
    """
    库安装和账户本地发现共享名称规则，避免系统来源被覆盖。

    :param value (str): 目标顶层名称
    :return str: 已校验的非系统名称
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value):
        raise ValueError("skill name must be lowercase ASCII letters, digits or hyphens")
    if value in {"ego-browser", "agent-remote-device"}:
        raise ValueError("skill name is reserved by the managed runtime")
    return value


class SkillSource(BaseModel):
    """
    来源身份不包含凭据；本地来源以不可解析为远端路径的指纹标识。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["git", "local"] = Field(description="来源种类")
    locator: str = Field(min_length=1, max_length=2048, description="HTTPS 仓库地址或本地来源指纹")
    subpath: str = Field(default="", max_length=4096, description="选定条目相对来源根的路径")

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        """
        验证来源可移植身份并拒绝 URL 中的凭据。

        :return Self: 已校验来源
        """
        if self.subpath:
            validate_relative_path(self.subpath)
        if self.kind == "local":
            if not re.fullmatch(r"[a-f0-9]{64}", self.locator):
                raise ValueError("local source requires a SHA-256 identity, not a host path")
        else:
            parsed = urlsplit(self.locator)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or not parsed.path.strip("/")
                or "\\" in self.locator
                or any(char.isspace() or ord(char) < 32 for char in self.locator)
            ):
                raise ValueError("git source requires an HTTPS repository URL without credentials")
            _ = parsed.port
        return self

    def identity(self) -> str:
        """
        为来源及子路径生成稳定、与分支选择无关的身份。

        :return str: 规范来源身份摘要
        """
        locator = self.locator
        if self.kind == "git":
            parsed = urlsplit(locator)
            path = parsed.path.rstrip("/").removesuffix(".git")
            if parsed.hostname == "github.com":
                path = path.lower()
            locator = urlunsplit(("https", parsed.netloc.lower(), path, "", ""))
        return hashlib.sha256(f"{self.kind}\0{locator}\0{self.subpath}".encode()).hexdigest()


class SkillProvenance(BaseModel):
    """
    一次完整来源观测，与用户默认跟踪策略分离。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    ref_kind: Literal["branch", "tag", "commit", "local"] = Field(description="来源引用类型")
    ref: str = Field(default="", max_length=256, description="来源分支、标签或提交引用")
    commit: str = Field(
        default="", pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})?$", description="完整提交摘要"
    )

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        """
        拒绝本地来源伪造 Git 引用和缺失的提交身份。

        :return Self: 完整来源观测
        """
        if self.ref_kind == "local":
            if self.ref or self.commit:
                raise ValueError("local provenance cannot contain git references")
        elif not self.ref or not self.commit or any(c.isspace() or ord(c) < 32 for c in self.ref):
            raise ValueError("git provenance requires a complete ref and commit")
        elif self.ref_kind == "commit" and self.ref != self.commit:
            raise ValueError("commit reference must equal the complete commit digest")
        return self


class SkillScope(BaseModel):
    """
    用户默认、多个工具或单一账户范围，三者不混用。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    tools: tuple[str, ...] = Field(default=(), max_length=32, description="显式工具范围")
    account_id: UUID | None = Field(default=None, description="显式账户范围")

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        """
        拒绝账户与工具并用以及重复工具。

        :return Self: 唯一明确的范围
        """
        if self.tools and self.account_id is not None:
            raise ValueError("tool and account scopes are mutually exclusive")
        if len(set(self.tools)) != len(self.tools):
            raise ValueError("tool scopes must be unique")
        return self

    @property
    def is_user(self) -> bool:
        """
        判断是否修改用户默认范围。

        :return bool: 是否未提供覆盖范围
        """
        return not self.tools and self.account_id is None


class SkillInstallItem(BaseModel):
    """
    已完整上传并由 Server 再校验格式的安装候选。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$", description="目标工具中的条目名称")
    source: SkillSource = Field(description="不可混淆的来源身份")
    provenance: SkillProvenance = Field(description="此次完整来源观测")
    tree_digest: str = Field(pattern=r"^[a-f0-9]{64}$", description="当前用户已上传的完整树摘要")

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """
        系统技能保留名称不能被普通安装占用。

        :param value (str): 目标名称
        :return str: 非系统名称
        """
        return validate_skill_name(value)

    @model_validator(mode="after")
    def validate_source_kind(self) -> Self:
        """
        来源类型和观测类型必须一致。

        :return Self: 一致的安装候选
        """
        if (self.source.kind == "local") != (self.provenance.ref_kind == "local"):
            raise ValueError("source and provenance kinds differ")
        return self


class SkillMutation(BaseModel):
    """
    修改命令共享的幂等身份和配置代数前置条件。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    idempotency_key: str = Field(
        min_length=1, max_length=128, pattern=r"^[!-~]+$", description="客户端持久化幂等键"
    )
    expected_generation: int = Field(strict=True, ge=0, le=2**63 - 2, description="预期用户库代数")


class SkillAddRequest(SkillMutation):
    """
    多项安装共享一次用户库事务。
    """

    command: Literal["add"] = Field(default="add", description="安装命令")
    items: tuple[SkillInstallItem, ...] = Field(
        min_length=1, max_length=100, description="全部选定候选"
    )
    scope: SkillScope = Field(default_factory=SkillScope, description="首次安装范围")
    scope_explicit: bool = Field(
        default=False, strict=True, description="重新安装是否显式请求新范围"
    )


class SkillUpdateRequest(SkillMutation):
    """
    为单项登记或激活来源版本，不改变固定到其他版本的覆盖。
    """

    command: Literal["update"] = Field(default="update", description="更新命令")
    skill: str = Field(min_length=1, max_length=64, description="当前用户名称或稳定标识")
    item: SkillInstallItem = Field(description="此次完整来源版本")
    stage: bool = Field(default=False, strict=True, description="是否只登记候选版本")
    switch_tracking: bool = Field(
        default=False, strict=True, description="是否显式通过新引用切换跟踪"
    )


class SkillRuleRequest(SkillMutation):
    """
    明确区分启用、固定版本和恢复继承的范围操作。
    """

    command: Literal["enable", "disable", "pin", "unpin", "inherit"] = Field(description="规则命令")
    skill: str = Field(min_length=1, max_length=64, description="当前用户名称或稳定标识")
    scope: SkillScope = Field(default_factory=SkillScope, description="用户、工具或账户范围")
    revision: str | None = Field(
        default=None, max_length=64, description="同一技能的版本标识或编号"
    )
    field: Literal["enabled", "revision", "all"] = Field(
        default="all", description="恢复继承的字段"
    )
    all_scopes: bool = Field(default=False, strict=True, description="停用并清除全部启用覆盖")

    @model_validator(mode="after")
    def validate_command(self) -> Self:
        """
        在事务之前拒绝含糊或不受支持的命令组合。

        :return Self: 有效的字段变更
        """
        if self.command in {"pin", "unpin", "inherit"} and self.scope.is_user:
            raise ValueError("pin, unpin and inherit require tool or account scope")
        if (self.command == "pin") != (self.revision is not None):
            raise ValueError("only pin requires a revision")
        if self.field != "all" and self.command != "inherit":
            raise ValueError("field selection is only valid for inherit")
        if self.all_scopes and (self.command != "disable" or not self.scope.is_user):
            raise ValueError("all_scopes is only valid for user-wide disable")
        return self


class SkillRollbackRequest(SkillMutation):
    """
    从真实激活历史选择旧版本，并固定默认上游更新策略。
    """

    command: Literal["rollback"] = Field(default="rollback", description="回滚命令")
    skill: str = Field(min_length=1, max_length=64, description="当前用户名称或稳定标识")
    revision: str | None = Field(
        default=None, max_length=64, description="显式版本或自动上次激活版本"
    )


class SkillRemoveRequest(SkillMutation):
    """
    逻辑卸载用户库条目，不删除版本、覆盖或活动会话数据。
    """

    command: Literal["remove"] = Field(default="remove", description="卸载命令")
    skill: str = Field(min_length=1, max_length=64, description="当前用户名称或稳定标识")


type SkillLibraryRequest = Annotated[
    SkillAddRequest
    | SkillUpdateRequest
    | SkillRuleRequest
    | SkillRollbackRequest
    | SkillRemoveRequest,
    Field(discriminator="command"),
]
