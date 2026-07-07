#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 <version>" >&2
  echo "Example: $0 0.1.0" >&2
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
PY

uv lock

echo "Prepared agent-remote-server v${VERSION}"
