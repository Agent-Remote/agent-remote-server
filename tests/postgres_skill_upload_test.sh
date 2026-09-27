#!/usr/bin/env bash
set -euo pipefail

if [[ "${AGENT_REMOTE_RUN_SKILL_UPLOAD_TEST:-}" != 1 ]]; then
  printf 'Set AGENT_REMOTE_RUN_SKILL_UPLOAD_TEST=1 for isolated PostgreSQL upload tests.\n' >&2
  exit 2
fi
mode="${AGENT_REMOTE_SKILL_UPLOAD_TEST_MODE:-regression}"
[[ "$mode" == regression || "$mode" == capacity || "$mode" == bytes || "$mode" == packages || "$mode" == downloads ]]
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
upload_root="$(mktemp -d "${TMPDIR:-/tmp}/agent-remote-upload-test.XXXXXX")"
container="$(basename "$upload_root")"
cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  rm -rf -- "$upload_root"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker run --detach --name "$container" --memory 1g --cpus 2 \
  --publish '127.0.0.1::5432' --env POSTGRES_HOST_AUTH_METHOD=trust \
  --env POSTGRES_USER=skill_test --env POSTGRES_DB=skill_upload_test \
  postgres:16-alpine >/dev/null
port="$(docker port "$container" 5432/tcp)"
port="${port##*:}"
ready=0
for ((attempt = 0; attempt < 100; attempt++)); do
  if docker exec "$container" pg_isready -U skill_test -d skill_upload_test >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.1
done
[[ "$ready" == 1 ]]
cd "$repo_root"
export SKILL_TEST_DATABASE_URL="postgresql+asyncpg://skill_test@127.0.0.1:$port/skill_upload_test"
DATABASE_URL="$SKILL_TEST_DATABASE_URL" uv run alembic upgrade head
if [[ "$mode" == regression ]]; then
  uv run pytest -q --basetemp "$upload_root/pytest" \
    tests/test_skill_upload_index.py tests/test_skill_upload_index_migration.py \
    tests/test_skill_content_service.py tests/test_skill_finalization.py \
    tests/test_node_skill_finalization.py tests/test_skill_content_gc_races.py \
    tests/test_skill_content_deletion_races.py tests/test_skill_content_gc.py \
    tests/test_skill_compaction_reclamation.py tests/test_skill_retention.py \
    tests/test_skill_tree_downloads.py tests/test_skill_tree_download_migration.py \
    tests/test_skill_content_retirement_admission.py tests/test_node_skill_content.py \
    tests/test_node_skill_deployment.py tests/test_skill_snapshot_lease.py
elif [[ "$mode" == capacity ]]; then
  AGENT_REMOTE_RUN_SKILL_UPLOAD_CAPACITY=1 uv run pytest -s -q --basetemp "$upload_root/pytest" \
    tests/test_skill_upload_capacity.py
elif [[ "$mode" == bytes ]]; then
  AGENT_REMOTE_RUN_SKILL_UPLOAD_BYTE_CAPACITY=1 uv run pytest -s -q --basetemp "$upload_root/pytest" \
    tests/test_skill_upload_byte_capacity.py
elif [[ "$mode" == packages ]]; then
  AGENT_REMOTE_RUN_SKILL_PACKAGE_BYTE_CAPACITY=1 uv run pytest -s -q --basetemp "$upload_root/pytest" \
    tests/test_skill_package_byte_capacity.py
else
  AGENT_REMOTE_RUN_SKILL_DOWNLOAD_CAPACITY=1 uv run pytest -s -q --basetemp "$upload_root/pytest" \
    tests/test_skill_download_capacity.py
fi
