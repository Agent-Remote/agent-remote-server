"""
验证丢失持久化卷时返回稳定诊断，修复挂载后可复用原上传恢复。
"""

from pathlib import Path
from typing import cast
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_identity_api import auth_header, bootstrap
from test_skill_api import skill_client as skill_client
from test_skill_storage import file_entry

from agent_remote_server.schemas.skill_manifest import SkillTreeManifest


def test_missing_volume_reports_storage_failure_and_original_upload_recovers(
    skill_client: TestClient, tmp_path: Path
) -> None:
    """
    真实缺失目录不泄露路径、不误称协议错误，恢复后原租约可以完成。

    :param skill_client (TestClient): 独立公开 API 客户端
    :param tmp_path (Path): 未挂载的私有测试卷
    """
    headers = auth_header(bootstrap(skill_client))
    root = tmp_path / "missing-volume" / "content"
    cast(FastAPI, skill_client.app).state.settings.skill_storage_root = root
    content = b"---\nname: learning\n---\nRemember this.\n"
    entry = file_entry(content)
    manifest = SkillTreeManifest(entries=(entry,))
    response = skill_client.post(
        "/api/v1/skills/content/uploads",
        headers=headers,
        json={"idempotency_key": str(uuid4()), "manifest": manifest.model_dump(mode="json")},
    )
    assert response.status_code == 200
    identity = response.json()["data"]["id"]
    path = f"/api/v1/skills/content/uploads/{identity}/files/{entry.sha256}"
    failed = skill_client.put(path, headers=headers, content=content)
    assert failed.status_code == 503
    assert failed.json()["errors"][0]["code"] == "CONTENT_STORAGE_UNAVAILABLE"
    assert failed.json()["committed"] is False
    assert str(tmp_path) not in failed.text
    root.parent.mkdir(mode=0o700)
    assert skill_client.put(path, headers=headers, content=content).status_code == 200
    completed = skill_client.post(
        f"/api/v1/skills/content/uploads/{identity}/complete", headers=headers
    )
    assert completed.status_code == 200 and completed.json()["committed"] is True
