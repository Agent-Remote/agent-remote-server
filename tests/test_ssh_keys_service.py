from uuid import uuid4

from agent_remote_server.models import SshKey
from agent_remote_server.services.ssh_keys import ssh_key_sync_task_id


def test_ssh_key_sync_task_revision_tracks_key_set() -> None:
    node_id = uuid4()
    device_id = uuid4()
    first = SshKey(
        id=uuid4(),
        user_device_id=device_id,
        public_key="ssh-ed25519 AAAAFIRST first@test",
        fingerprint="SHA256:first",
        status="active",
    )
    second = SshKey(
        id=uuid4(),
        user_device_id=device_id,
        public_key="ssh-ed25519 AAAASECOND second@test",
        fingerprint="SHA256:second",
        status="active",
    )

    first_revision = ssh_key_sync_task_id(
        node_id=node_id, device_id=device_id, ssh_keys=[first, second]
    )
    reordered_revision = ssh_key_sync_task_id(
        node_id=node_id, device_id=device_id, ssh_keys=[second, first]
    )
    rotated_revision = ssh_key_sync_task_id(node_id=node_id, device_id=device_id, ssh_keys=[second])

    assert first_revision == reordered_revision
    assert first_revision != rotated_revision
    assert first_revision.startswith("sync_ssh_keys:v4:")
    assert len(first_revision) <= 128
