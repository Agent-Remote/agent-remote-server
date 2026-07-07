#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 <version>" >&2
  echo "Example: $0 0.0.2" >&2
}

if [[ $# -ne 1 ]]; then
  usage
  exit 2
fi

VERSION="${1#v}"
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-.+][0-9A-Za-z.-]+)?$ ]]; then
  echo "Invalid semantic version: $1" >&2
  exit 2
fi

python3 - "$VERSION" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

version = sys.argv[1]

pyproject = Path("pyproject.toml")
text = pyproject.read_text()
text = re.sub(r'(?m)^version = "[^"]+"$', f'version = "{version}"', text, count=1)
pyproject.write_text(text)

dockerfile = Path("Dockerfile")
text = dockerfile.read_text()
text = re.sub(
    r"ARG AGENT_REMOTE_VERSION=[0-9A-Za-z.+-]+",
    f"ARG AGENT_REMOTE_VERSION={version}",
    text,
    count=1,
)
dockerfile.write_text(text)

runtime = Path("src/agent_remote_server/__init__.py")
text = runtime.read_text()
text = re.sub(r'_version = "[0-9A-Za-z.+-]+"', f'_version = "{version}"', text, count=1)
runtime.write_text(text)

routes = Path("src/agent_remote_server/api/routes.py")
text = routes.read_text()
text = re.sub(
    r'"protocol_version": "[0-9A-Za-z.+-]+"',
    f'"protocol_version": "{version}"',
    text,
    count=1,
)
routes.write_text(text)

for path in sorted(Path("tests").glob("test_*.py")):
    text = path.read_text()
    text = re.sub(r'"version": "[0-9A-Za-z.+-]+"', f'"version": "{version}"', text)
    text = re.sub(
        r'\["protocol_version"\] == "[0-9A-Za-z.+-]+"',
        f'["protocol_version"] == "{version}"',
        text,
    )
    path.write_text(text)
PY

uv lock

scripts/update-changelog.sh "$VERSION"

echo "Prepared agent-remote-server v${VERSION}"
