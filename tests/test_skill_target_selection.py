"""
回归账号隔离安装后全局版本操作误报无关节点不支持的问题。
"""

from deployment_attempt_support import bound_account
from test_skill_content_service import database as database
from test_skill_library import LibraryHarness
from test_skill_library import library as library

from agent_remote_server.schemas.skill_library import (
    SkillRemoveRequest,
    SkillRollbackRequest,
    SkillRuleRequest,
    SkillScope,
    SkillUpdateRequest,
)


async def test_global_lifecycle_excludes_unrelated_unsupported_account(
    library: LibraryHarness,
) -> None:
    """
    未启用来源的旧节点不阻断更新、回滚和卸载，卸载仍包含原来启用的账户。

    :param library (LibraryHarness): 私有库及真实数据库事务
    """
    unrelated = await bound_account(library)
    isolated = await library.account()
    installed = await library.add(await library.candidate(), scope=SkillScope(account_id=isolated))
    first = installed.data.revision_ids[0]
    updated = await library.execute(
        SkillUpdateRequest(
            skill="learning",
            item=await library.candidate(version="two"),
            idempotency_key="update",
            expected_generation=1,
        )
    )
    rolled_back = await library.execute(
        SkillRollbackRequest(
            skill="learning",
            revision=str(first),
            idempotency_key="rollback",
            expected_generation=2,
        )
    )
    removed = await library.execute(
        SkillRemoveRequest(skill="learning", idempotency_key="remove", expected_generation=3)
    )
    for result in (installed, updated, rolled_back, removed):
        assert result.status == "stored" and result.committed and not result.errors
        assert [target.account_id for target in result.data.targets] == [isolated]
        assert all(target.account_id != unrelated for target in result.data.targets)
        assert result.operation_id is not None
        async with library.database() as session:
            assert (
                await library.service(session).status(library.owner, result.operation_id) == result
            )


async def test_global_update_preserves_pin_and_disable_keeps_cleanup_target(
    library: LibraryHarness,
) -> None:
    """
    固定原版本的账户不随默认更新部署，全局禁用仍清理此前启用的目标。

    :param library (LibraryHarness): 私有库及真实数据库事务
    """
    account = await bound_account(library)
    added = await library.add(await library.candidate())
    await library.execute(
        SkillRuleRequest(
            command="pin",
            skill="learning",
            revision=str(added.data.revision_ids[0]),
            scope=SkillScope(account_id=account),
            idempotency_key="pin",
            expected_generation=1,
        )
    )
    update = await library.execute(
        SkillUpdateRequest(
            skill="learning",
            item=await library.candidate(version="two"),
            idempotency_key="update",
            expected_generation=2,
        )
    )
    assert update.status == "stored" and update.data.targets == []
    disabled = await library.execute(
        SkillRuleRequest(
            command="disable", skill="learning", idempotency_key="disable", expected_generation=3
        )
    )
    assert [target.account_id for target in disabled.data.targets] == [account]
    assert disabled.errors[0].code == "SKILL_MANAGER_UNSUPPORTED"
