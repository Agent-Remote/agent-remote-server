"""
定义可保存和预览的明确冲突选择，不允许混用解决方式。
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_remote_server.schemas.skill_manifest import SkillTreeManifest, validate_relative_path


class SkillResolutionChoice(BaseModel):
    """
    空范围表示完整目录，普通路径和完整连通单元互斥。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str | None = Field(default=None, max_length=4096, description="普通路径冲突的相对路径")
    unit: tuple[str, ...] = Field(default=(), description="需要整体选择的连通范围")
    use: Literal["current", "incoming"] | None = Field(
        default=None, description="选择保留的完整一侧"
    )
    file_tree_digest: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$", description="只含一个普通文件的已上传状态树"
    )
    directory_tree_digest: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$", description="人工处理后的完整范围状态树"
    )

    @model_validator(mode="after")
    def validate_choice(self) -> Self:
        """
        每次请求只有一个范围和一种解决方式。

        :return Self: 明确的完整选择
        """
        if (
            sum(
                value is not None
                for value in (self.use, self.file_tree_digest, self.directory_tree_digest)
            )
            != 1
        ):
            raise ValueError("choose exactly one side, file or directory")
        if self.path is not None:
            validate_relative_path(self.path)
            if self.unit or self.directory_tree_digest is not None:
                raise ValueError("path cannot be combined with a unit or directory")
        elif self.file_tree_digest is not None:
            raise ValueError("file choice requires a conflict path")
        if len(set(self.unit)) != len(self.unit) or tuple(sorted(self.unit)) != self.unit:
            raise ValueError("unit scopes must be unique and sorted")
        for name in self.unit:
            if name != ".":
                validate_relative_path(name)
                if "/" in name:
                    raise ValueError("unit members must be top-level identities")
        return self

    @property
    def whole(self) -> bool:
        """
        是否选择整个冲突目录。

        :return bool: 是否没有更窄的显式范围
        """
        return self.path is None and not self.unit

    @property
    def tree_digest(self) -> str | None:
        """
        获取需要按用户归属验证的人工内容引用。

        :return str | None: 内容树摘要或纯侧选择
        """
        return self.file_tree_digest or self.directory_tree_digest


class SkillResolutionRequest(BaseModel):
    """
    一次计划修改具有持久幂等键和明确计划版本，预览不保存任何选择。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    idempotency_key: str = Field(
        min_length=1, max_length=128, pattern=r"^[!-~]+$", description="客户端保存的命令幂等键"
    )
    expected_revision: int = Field(strict=True, ge=0, le=2**63 - 2, description="预期解决计划版本")
    choice: SkillResolutionChoice = Field(description="此次完整选择")
    dry_run: bool = Field(default=False, strict=True, description="只验证及预览，不保存计划或发布")


class SkillResolutionUploadRequest(BaseModel):
    """
    人工解决内容上传只建立私有状态树，不直接修改任何账户目录。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    idempotency_key: str = Field(
        min_length=1, max_length=128, pattern=r"^[!-~]+$", description="持久化的人工内容上传键"
    )
    manifest: SkillTreeManifest = Field(description="人工处理后的完整状态清单")
