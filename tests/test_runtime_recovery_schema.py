"""
验证恢复动作的严格版本组合与旧字段集合。
"""

import pytest
from pydantic import ValidationError

from agent_remote_server.schemas.runtime_recovery import (
    RuntimeRecoveryBinding,
    RuntimeRecoveryRequest,
)


def test_recovery_request_requires_absent_or_explicit_source_action() -> None:
    """
    旧请求不输出空动作，显式空值或未知动作不能降级为默认动作。
    """
    request = {
        "original_task_id": "original",
        "request_id": "11111111-1111-4111-8111-111111111111",
    }
    assert RuntimeRecoveryRequest.model_validate(request).model_dump(mode="json") == request
    for action in (None, "", "repair", 1):
        with pytest.raises(ValidationError):
            RuntimeRecoveryRequest.model_validate({**request, "action": action})
    for action in ("verify_source", "repair_source"):
        selected = {**request, "action": action}
        assert RuntimeRecoveryRequest.model_validate(selected).model_dump(mode="json") == selected


@pytest.mark.parametrize("version", [1, 2, 3])
def test_recovery_binding_preserves_exact_versioned_action(version: int) -> None:
    """
    旧绑定保持十一个字段，源验证绑定必须使用第十二个明确动作字段。

    :param version (int): 所验证的协议版本
    """
    identity = "11111111-1111-4111-8111-111111111111"
    binding: dict[str, object] = {
        "version": version,
        "task_id": "recovery",
        "task_record_id": identity,
        "original_task_id": "original",
        "original_task_record_id": identity,
        "node_id": identity,
        "user_id": identity,
        "tool_account_id": identity,
        "tool_type": "claude",
        "source_runtime_backend": "native",
        "target_runtime_backend": "docker_sandbox",
    }
    if version == 2:
        binding["action"] = "verify_source"
    elif version == 3:
        binding["action"] = "repair_source"
    assert RuntimeRecoveryBinding.model_validate(binding).model_dump(mode="json") == binding
    wrong_action = "verify_source" if version == 3 else "repair_source"
    for action in (None, "", "repair", 1, wrong_action):
        with pytest.raises(ValidationError):
            RuntimeRecoveryBinding.model_validate({**binding, "action": action})
    if version == 1:
        with pytest.raises(ValidationError):
            RuntimeRecoveryBinding.model_validate({**binding, "action": "verify_source"})
    else:
        binding.pop("action")
        with pytest.raises(ValidationError):
            RuntimeRecoveryBinding.model_validate(binding)
