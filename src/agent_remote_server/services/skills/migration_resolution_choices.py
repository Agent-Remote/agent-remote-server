"""
转换迁移专用选择引用，并验证该迁移下完成人工上传的实际内容。
"""

import hashlib
import json

from agent_remote_server.models.skill_migration_resolution import (
    SkillMigrationResolutionChoice as ChoiceRow,
)
from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.repositories.skill_migration_resolution import (
    SkillMigrationResolutionRepository,
)
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.services.skills.content import SkillContentError, SkillContentService
from agent_remote_server.skill_manager.resolution import LoadedResolutionChoice
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


def choice_spec(row: ChoiceRow) -> SkillResolutionChoice:
    """
    持久化列是唯一内容指针，不从回执 JSON 恢复授权。

    :param row (ChoiceRow): 已授权选择行
    :return SkillResolutionChoice: 完整严格选择
    """
    values: dict[str, object] = {"path": row.path, "unit": row.unit_json}
    if row.kind in {"current", "incoming"}:
        values["use"] = row.kind
    else:
        values[f"{row.kind}_tree_digest"] = row.tree_digest
    return SkillResolutionChoice.model_validate(values)


def choice_row(migration: SkillBranchPreparation, choice: SkillResolutionChoice) -> ChoiceRow:
    """
    选择键只由范围决定，内容和方法改变需要新的计划版本。

    :param migration (SkillBranchPreparation): 已授权迁移
    :param choice (SkillResolutionChoice): 已验证选择
    :return ChoiceRow: 等待同事务保存的选择引用
    """
    selector = json.dumps(
        {"path": choice.path, "unit": choice.unit}, sort_keys=True, separators=(",", ":")
    )
    return ChoiceRow(
        migration_id=migration.id,
        selector_key=hashlib.sha256(selector.encode()).hexdigest(),
        user_id=migration.user_id,
        account_id=migration.account_id,
        path=choice.path,
        unit_json=list(choice.unit),
        kind=choice.use or ("file" if choice.file_tree_digest else "directory"),
        tree_digest=choice.tree_digest,
    )


async def load_choices(
    content: SkillContentService,
    store: PrivateObjectStore,
    migration: SkillBranchPreparation,
    repository: SkillMigrationResolutionRepository,
    choices: list[SkillResolutionChoice],
) -> list[LoadedResolutionChoice]:
    """
    人工内容必须属于本迁移的已完成上传，不能借用其他同用户状态树。

    :param content (SkillContentService): 同事务私有内容访问
    :param store (PrivateObjectStore): 私有字节卷
    :param migration (SkillBranchPreparation): 已授权原迁移
    :param repository (SkillMigrationResolutionRepository): 同事务授权仓储
    :param choices (list[SkillResolutionChoice]): 完整选择集合
    :return list[LoadedResolutionChoice]: 已验证字节与摘要的纯算法输入
    """
    loaded = []
    for choice in choices:
        tree = None
        if choice.tree_digest is not None:
            if await repository.content(migration, choice.tree_digest) is None:
                raise SkillContentError(
                    "RESOLUTION_CONTENT_NOT_FOUND", "custom tree not completed for this migration"
                )
            tree = await content.read_tree(migration.user_id, "state", choice.tree_digest)
            try:
                await store.verify_manifest(migration.user_id, tree)
            except FileNotFoundError as error:
                raise SkillContentError(
                    "CONTENT_INCOMPLETE", "custom migration content is unavailable"
                ) from error
            except ValueError as error:
                raise SkillContentError(
                    "CONTENT_INVALID", "custom migration content verification failed"
                ) from error
        loaded.append(LoadedResolutionChoice(choice, tree))
    return loaded
