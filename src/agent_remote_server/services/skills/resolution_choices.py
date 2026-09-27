"""
在严格选择契约与持久化行之间转换，并完整授权人工内容。
"""

import hashlib
import json
from uuid import UUID

from agent_remote_server.models.skill_publications import SkillPublication
from agent_remote_server.models.skill_resolution import SkillResolutionChoice as ChoiceRow
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.services.skills.content import SkillContentService
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


def choice_row(publication: SkillPublication, choice: SkillResolutionChoice) -> ChoiceRow:
    """
    选择键只由范围决定，内容和方法改变需要新的计划版本。

    :param publication (SkillPublication): 已授权尝试
    :param choice (SkillResolutionChoice): 已验证选择
    :return ChoiceRow: 等待同事务保存的选择引用
    """
    selector = json.dumps(
        {"path": choice.path, "unit": choice.unit}, sort_keys=True, separators=(",", ":")
    )
    return ChoiceRow(
        publication_id=publication.id,
        selector_key=hashlib.sha256(selector.encode()).hexdigest(),
        user_id=publication.user_id,
        account_id=publication.account_id,
        path=choice.path,
        unit_json=list(choice.unit),
        kind=choice.use or ("file" if choice.file_tree_digest else "directory"),
        tree_digest=choice.tree_digest,
    )


async def load_choices(
    content: SkillContentService,
    store: PrivateObjectStore,
    user_id: UUID,
    choices: list[SkillResolutionChoice],
) -> list[LoadedResolutionChoice]:
    """
    人工文件或目录必须在当前用户状态存储中完整存在。

    :param content (SkillContentService): 同事务私有内容访问
    :param store (PrivateObjectStore): 私有字节卷
    :param user_id (UUID): 已认证所有者
    :param choices (list[SkillResolutionChoice]): 完整选择集合
    :return list[LoadedResolutionChoice]: 已验证字节与摘要的纯算法输入
    """
    loaded = []
    for choice in choices:
        tree = None
        if choice.tree_digest is not None:
            tree = await content.read_tree(user_id, "state", choice.tree_digest)
            await store.verify_manifest(user_id, tree)
        loaded.append(LoadedResolutionChoice(choice, tree))
    return loaded
