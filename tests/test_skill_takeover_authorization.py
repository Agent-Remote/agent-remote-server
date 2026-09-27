"""
验证接管租约、捕获输入和账户归属的逐次授权。
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from skill_takeover_support import TakeoverHarness, tree
from skill_takeover_support import takeover as takeover
from sqlalchemy import update
from test_skill_content_service import database as database
from test_skill_library import library as library

from agent_remote_server.models import Node, NodeTask, ToolAccount, User
from agent_remote_server.models.skill_state import AccountSkillDirectoryState
from agent_remote_server.models.skill_storage import SkillContentUpload
from agent_remote_server.schemas.skill_takeover import SkillTakeoverCapture, SkillTakeoverRequest
from agent_remote_server.services.skills.content import SkillContentError


@pytest.mark.parametrize(
    "change",
    [
        "pending",
        "expired",
        "failed",
        "wrong_task",
        "wrong_node",
        "payload_bool",
        "disabled_owner",
        "backend",
        "epoch",
    ],
)
async def test_capture_requires_exact_current_authorization(
    takeover: TakeoverHarness, change: str
) -> None:
    """
    相同摘要或账户归属不能替代精确租约，布尔值也不能冒充协议版本一。

    :param takeover (TakeoverHarness): 未接管账户
    :param change (str): 被破坏的授权维度
    """
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    async with takeover.library.database.begin() as session:
        task = await session.get(NodeTask, receipt.task_id)
        assert task is not None
        if change in {"pending", "failed"}:
            task.status = change
        elif change == "expired":
            task.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        elif change == "payload_bool":
            await session.execute(
                update(NodeTask)
                .where(NodeTask.id == task.id)
                .values(payload={**task.payload, "protocol_version": True})
            )
        elif change == "disabled_owner":
            owner = await session.get(User, takeover.library.owner)
            assert owner is not None
            owner.status = "disabled"
        elif change == "backend":
            account = await session.get(ToolAccount, takeover.account)
            assert account is not None
            account.runtime_backend = "docker_sandbox"
        elif change == "epoch":
            directory = await session.get(AccountSkillDirectoryState, takeover.account)
            assert directory is not None
            directory.epoch += 1
    async with takeover.library.database.begin() as session:
        with pytest.raises(SkillContentError) as error:
            await takeover.service(session).begin_capture(
                uuid4() if change == "wrong_node" else takeover.node,
                receipt.id,
                uuid4() if change == "wrong_task" else receipt.task_id,
                takeover.capture(receipt, tree({})),
            )
        assert error.value.code == {
            "backend": "TAKEOVER_BINDING_CHANGED",
            "epoch": "HEAD_CHANGED",
        }.get(change, "TAKEOVER_NOT_FOUND")


@pytest.mark.parametrize("change", ["quiescence", "zero_receipt", "epoch", "inventory"])
async def test_helper_proof_must_match_reserved_fence(
    takeover: TakeoverHarness, change: str
) -> None:
    """
    静止证明必须绑定预约纪元、清单和非空本地收据。

    :param takeover (TakeoverHarness): 未接管账户
    :param change (str): 捕获声明破坏方式
    """
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    request = takeover.capture(receipt, tree({}))
    variants: dict[str, dict[str, object]] = {
        "quiescence": {"writers_quiescent": False},
        "zero_receipt": {"helper_receipt_id": UUID(int=0)},
        "epoch": {"directory_epoch": 2},
        "inventory": {"inventory_digest": "f" * 64},
    }
    request = SkillTakeoverCapture.model_validate({**request.model_dump(), **variants[change]})
    with pytest.raises(SkillContentError) as error:
        await takeover.begin(receipt, request)
    assert error.value.code == "TAKEOVER_CAPTURE_INVALID"


@pytest.mark.parametrize("change", ["manifest", "helper"])
async def test_capture_identity_is_immutable_after_first_upload(
    takeover: TakeoverHarness, change: str
) -> None:
    """
    重传只能复用同一本地冻结输入，不能改树或换捕获身份。

    :param takeover (TakeoverHarness): 未接管账户
    :param change (str): 变更输入维度
    """
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    request = takeover.capture(receipt, tree({}))
    receipt = await takeover.begin(receipt, request)
    changed = request.model_copy(
        update=(
            {"manifest": tree({"new": b"late write"})}
            if change == "manifest"
            else {"helper_receipt_id": uuid4()}
        )
    )
    with pytest.raises(SkillContentError) as error:
        await takeover.begin(receipt, changed)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert (await takeover.begin(receipt, request)).upload_id == receipt.upload_id


async def test_expired_upload_renews_only_identical_capture(takeover: TakeoverHarness) -> None:
    """
    上传过期后使用新尝试续传，相同捕获保持不变且旧尝试不可写或完成。

    :param takeover (TakeoverHarness): 未接管账户
    """
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    request = takeover.capture(receipt, tree({"saved": b"saved"}))
    receipt = await takeover.begin(receipt, request)
    async with takeover.library.database.begin() as session:
        upload = await session.get(SkillContentUpload, receipt.upload_id)
        assert upload is not None
        upload.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    renewed = await takeover.begin(receipt, request)
    assert renewed.upload_id != receipt.upload_id and renewed.upload_attempt == 2
    with pytest.raises(SkillContentError) as complete_error:
        await takeover.complete(receipt)
    assert complete_error.value.code == "UPLOAD_SUPERSEDED"
    with pytest.raises(SkillContentError) as write_error:
        await takeover.transfer(receipt, {"saved": b"saved"})
    assert write_error.value.code == "UPLOAD_SUPERSEDED"
    await takeover.transfer(renewed, {"saved": b"saved"})
    assert (await takeover.complete(renewed)).status == "committed"
    with pytest.raises(SkillContentError) as committed_write:
        await takeover.transfer(renewed, {"saved": b"saved"})
    assert committed_write.value.code == "UPLOAD_SUPERSEDED"


@pytest.mark.parametrize("change", ["disabled", "stale", "capability", "backend", "epoch", "owner"])
async def test_reservation_preconditions_leave_legacy_untouched(
    takeover: TakeoverHarness, change: str
) -> None:
    """
    能力、用户和旧纪元不满足时不能预先写入迁移模式。

    :param takeover (TakeoverHarness): 未接管账户
    :param change (str): 不满足的预约前置条件
    """
    async with takeover.library.database.begin() as session:
        node = await session.get(Node, takeover.node)
        account = await session.get(ToolAccount, takeover.account)
        assert node is not None and account is not None
        if change == "disabled":
            takeover.settings = takeover.settings.model_copy(
                update={"skill_manager_enabled": False}
            )
        elif change == "stale":
            node.last_heartbeat_at = datetime.now(UTC) - timedelta(days=1)
        elif change == "capability":
            node.runtime_capabilities = {"backends": ["native"]}
        elif change == "backend":
            account.runtime_backend = None
    async with takeover.library.database.begin() as session:
        with pytest.raises(SkillContentError) as error:
            await takeover.service(session).reserve(
                uuid4() if change == "owner" else takeover.library.owner,
                takeover.account,
                SkillTakeoverRequest(
                    idempotency_key="key", expected_directory_epoch=(1 if change == "epoch" else 0)
                ),
            )
        assert error.value.code == {
            "disabled": "SKILL_MANAGER_DISABLED",
            "epoch": "HEAD_CHANGED",
            "owner": "ACCOUNT_NOT_FOUND",
        }.get(change, "SKILL_MANAGER_UNSUPPORTED")
    async with takeover.library.database() as session:
        assert await session.get(AccountSkillDirectoryState, takeover.account) is None


@pytest.mark.parametrize("value", [True, -1, 2**63 - 1])
def test_request_epoch_does_not_coerce_or_overflow(value: int) -> None:
    """
    请求纪元不能隐式转换布尔值或在预约自增时溢出。

    :param value (int): 非法纪元
    """
    with pytest.raises(ValidationError):
        SkillTakeoverRequest(idempotency_key="key", expected_directory_epoch=value)
