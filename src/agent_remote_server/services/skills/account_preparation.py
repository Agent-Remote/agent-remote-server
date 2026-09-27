"""
在账户启动事务中准备全部有效版本，保留正常冲突并复用同一比较的内部受理键。
"""

import hashlib
import json
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.schemas.skill_preparation import (
    SkillPreparationRequest,
    SkillPreparationView,
)
from agent_remote_server.schemas.skill_state_commands import (
    SkillStatePrecondition,
    SkillStateSelector,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.preparation import SkillPreparationService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


class AccountSkillPreparation:
    """
    调用方持有用户锁及外层保存点，本层不创建 session 或更新有效使用账本。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, policy: SkillStoragePolicy
    ) -> None:
        """
        复用独立准备的身份、内容和发布事务。

        :param session (AsyncSession): 启动事务
        :param store (PrivateObjectStore): 私有内容卷
        :param policy (SkillStoragePolicy): 部署配额
        """
        self.service = SkillPreparationService(session, store, policy)

    async def prepare(self, user_id: UUID, account_id: UUID) -> tuple[UUID, ...]:
        """
        正常冲突继续收集，非法来源或内容异常交给外层回滚整次账户准备。

        :param user_id (UUID): 已授权用户
        :param account_id (UUID): 已授权账户
        :return tuple[UUID, ...]: 阻止启动的已保留迁移身份
        """
        selection = self.service.selection
        current = await selection.current(
            user_id, SkillStateSelector(account_id=account_id, scope="account-directory")
        )
        if current.precondition.directory_mode != "managed_v1":
            raise SkillContentError(
                "MIGRATION_PENDING", "account directory takeover is not complete"
            )
        conflicts = []
        for target in current.precondition.targets:
            if not target.rule.included:
                continue
            if target.expired:
                raise SkillContentError(
                    "STATE_EXPIRED", "selected branch requires explicit recovery"
                )
            if target.origin != "user_library" or target.head_checkpoint_id is not None:
                continue
            selector = SkillStateSelector(
                account_id=account_id, scope="item", skill=str(target.skill_id)
            )
            expected = (await selection.current(user_id, selector)).precondition
            key = _key(account_id, expected)
            prior = await self.service.repository.receipt(user_id, key)
            if prior is not None:
                if (
                    prior.mode == "incremental"
                    or _key(
                        prior.account_id,
                        SkillPreparationView.model_validate(prior.response_json).before,
                    )
                    != key
                    or prior.target_state_id != expected.targets[0].state_id
                ):
                    raise SkillContentError(
                        "IDEMPOTENCY_CONFLICT", "admission key belongs to another preparation"
                    )
                if prior.status == "conflicted":
                    conflicts.append(prior.id)
                    continue
                raise SkillContentError(
                    "STATE_PRECONDITION_CHANGED", "saved admission comparison is no longer active"
                )
            result = await self.service.execute(
                user_id,
                SkillPreparationRequest(
                    selector=selector,
                    expected=expected,
                    idempotency_key=key,
                ),
            )
            if result.status == "conflicted":
                assert result.operation_id is not None
                conflicts.append(result.operation_id)
        return tuple(conflicts)


def _key(account_id: UUID, state: SkillStatePrecondition) -> str:
    """
    空分支创建不改变输入含义，其稳定身份不应使相同冲突在每次重试重复受理。

    :param account_id (UUID): 精确账户
    :param state (SkillStatePrecondition): 本次完整前置条件
    :return str: 有界内部幂等键
    """
    value = state.model_dump(mode="json")
    target = state.targets[0].model_dump(mode="json")
    if state.targets[0].head_checkpoint_id is None:
        target.update(state_id=None, state_epoch=state.targets[0].state_epoch or 1)
    value["targets"] = [target]
    encoded = json.dumps(
        {"account_id": str(account_id), "expected": value}, sort_keys=True, separators=(",", ":")
    )
    return "admission:" + hashlib.sha256(encoded.encode()).hexdigest()
