#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 <version>" >&2
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
fix_match = re.fullmatch(r"([0-9]+\.[0-9]+\.[0-9]+)-fix\.([0-9]+)", version)
package_version = (
    f"{fix_match.group(1)}+fix.{fix_match.group(2)}" if fix_match else version
)

pyproject = Path("pyproject.toml")
text = pyproject.read_text()
text, count = re.subn(
    r'(?m)^version = "[^"]+"$', f'version = "{package_version}"', text, count=1
)
if count != 1:
    raise SystemExit("Project version was not updated exactly once")
pyproject.write_text(text)
PY

uv lock

scripts/update-changelog.sh "$VERSION"

echo "Prepared agent-remote-server v${VERSION}"
