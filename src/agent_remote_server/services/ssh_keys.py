import hashlib
from collections.abc import Sequence
from uuid import UUID

from agent_remote_server.models import SshKey


def ssh_key_sync_task_id(*, node_id: UUID, device_id: UUID, ssh_keys: Sequence[SshKey]) -> str:
    """
    生成随设备活跃 SSH key 集合变化的同步任务 ID

    :param node_id (UUID): 节点 ID
    :param device_id (UUID): 设备 ID
    :param ssh_keys (Sequence[SshKey]): 设备活跃 SSH key

    :return str: SSH key 同步任务 ID
    """

    digest = hashlib.sha256()
    for ssh_key in sorted(ssh_keys, key=lambda item: str(item.id)):
        digest.update(str(ssh_key.id).encode())
        digest.update(b"\0")
        digest.update(ssh_key.public_key.encode())
        digest.update(b"\0")
    revision = digest.hexdigest()[:16]
    return f"sync_ssh_keys:v3:{node_id}:{device_id}:{revision}"


def ssh_key_sync_payload(
    *, device_id: UUID, ssh_user: str, ssh_keys: Sequence[SshKey]
) -> dict[str, object]:
    """
    构建设备级 SSH key 同步任务载荷

    :param device_id (UUID): 设备 ID
    :param ssh_user (str): SSH 网关用户
    :param ssh_keys (Sequence[SshKey]): 设备活跃 SSH key

    :return dict: SSH key 同步任务载荷
    """

    return {
        "device_id": str(device_id),
        "ssh_user": ssh_user,
        "authorized_keys_path": None,
        "ssh_keys": [
            {
                "id": str(ssh_key.id),
                "public_key": ssh_key.public_key,
                "forced_command": f"agent-remote-attach --device {device_id}",
            }
            for ssh_key in ssh_keys
        ],
    }
