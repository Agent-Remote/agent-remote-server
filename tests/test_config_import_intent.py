"""
冻结跨语言摘要算法、请求字段严格性以及内容清单的顺序边界。
"""

import copy
import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_remote_server.schemas.config_import_requests import ConfigImportRequest
from agent_remote_server.services.config_import_intent import intent_fingerprint, manifest


def test_shared_unicode_golden_and_order_contract() -> None:
    """
    非 ASCII 内容、路径、文件排序及用户选择顺序必须与固定协议向量一致。
    """
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/config_import_intent_v1.json").read_text()
    )
    account = UUID(fixture["tool_account_id"])
    payload = ConfigImportRequest.model_validate(fixture["request"])
    entries = manifest(payload)
    assert [entry.model_dump() for entry in entries] == fixture["manifest"]
    expected = "sha256:2d3b769b81f1f10415ab6ddfffe0fffe2e7596763bfbfaf5a40761784d500e9f"
    assert (
        intent_fingerprint(account, payload, entries) == expected == fixture["intent_fingerprint"]
    )
    import hashlib

    assert (
        "sha256:" + hashlib.sha256(fixture["canonical_json"].encode("utf-8")).hexdigest()
        == expected
    )
    reordered = payload.model_copy(deep=True)
    reordered.files.reverse()
    assert intent_fingerprint(account, reordered, manifest(reordered)) == expected
    for field in ["include", "exclude"]:
        changed = payload.model_copy(deep=True)
        getattr(changed, field).reverse()
        assert intent_fingerprint(account, changed, manifest(changed)) != expected
    renamed_request = payload.model_copy(
        update={"request_id": "11111111-1111-4111-8111-111111111111"}
    )
    assert intent_fingerprint(account, renamed_request, manifest(renamed_request)) == expected


@pytest.mark.parametrize("field", list(ConfigImportRequest.model_fields))
def test_all_request_fields_are_required_and_nonnullable(field: str) -> None:
    """
    协议字段不能通过遗漏或空值落入实现默认行为。

    :param field (str): 当前检查的明确必需字段
    """
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/config_import_intent_v1.json").read_text()
    )
    for missing in [True, False]:
        value = copy.deepcopy(fixture["request"])
        if missing:
            del value[field]
        else:
            value[field] = None
        with pytest.raises(ValidationError):
            ConfigImportRequest.model_validate(value)


@pytest.mark.parametrize("control", ["\x00", "\x7f", "\x85", "\x9f", "\ud800"])
def test_canonical_paths_reject_unicode_controls_and_surrogates(control: str) -> None:
    """
    C1 控制符和代理码位与 Rust、Go 的审核边界保持一致。

    :param control (str): 无法进入规范路径的控制或代理字符
    """
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/config_import_intent_v1.json").read_text()
    )
    value = fixture["request"]
    value["files"][0]["path"] = "~/.claude/agents/a" + control + ".md"
    with pytest.raises(ValidationError):
        ConfigImportRequest.model_validate(value)
