#!/usr/bin/env bash
set -euo pipefail

[[ "${AGENT_REMOTE_RUN_SKILL_PIPELINE_CAPACITY:-}" == 1 ]]
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pipeline_root="$(mktemp -d "${TMPDIR:-/tmp}/agent-remote-pipeline-test.XXXXXX")"
container="$(basename "$pipeline_root")"
cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  rm -rf -- "$pipeline_root"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
docker run --detach --name "$container" --memory 1g --cpus 2 \
  --publish '127.0.0.1::5432' --env POSTGRES_HOST_AUTH_METHOD=trust \
  --env POSTGRES_USER=skill_test --env POSTGRES_DB=skill_pipeline_test \
  postgres:16-alpine >/dev/null
port="$(docker port "$container" 5432/tcp)"
port="${port##*:}"
ready=0
for ((attempt = 0; attempt < 100; attempt++)); do
  if docker exec "$container" pg_isready -U skill_test -d skill_pipeline_test >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.1
done
[[ "$ready" == 1 ]]
cd "$repo_root"
export SKILL_TEST_DATABASE_URL="postgresql+asyncpg://skill_test@127.0.0.1:$port/skill_pipeline_test"
DATABASE_URL="$SKILL_TEST_DATABASE_URL" uv run alembic upgrade head
uv run pytest -s -q --basetemp "$pipeline_root/pytest" tests/test_skill_pipeline_capacity.py
