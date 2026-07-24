#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 <version>" >&2
  echo "Example: $0 0.0.4-fix.13" >&2
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
import stat
import sys
import tempfile
from pathlib import Path

version = sys.argv[1]
fix_match = re.fullmatch(r"([0-9]+\.[0-9]+\.[0-9]+)-fix\.([0-9]+)", version)
package_version = (
    f"{fix_match.group(1)}+fix.{fix_match.group(2)}" if fix_match else version
)

script = Path("scripts/prepare-release.sh")
text = script.read_text()
text = re.sub(r"Example: \$0 [0-9A-Za-z.+-]+", f"Example: $0 {version}", text)
mode = stat.S_IMODE(script.stat().st_mode)
with tempfile.NamedTemporaryFile(
    mode="w", encoding="utf-8", dir=script.parent, delete=False
) as temporary:
    temporary.write(text)
replacement = Path(temporary.name)
replacement.chmod(mode)
replacement.replace(script)

pyproject = Path("pyproject.toml")
text = pyproject.read_text()
text = re.sub(
    r'(?m)^version = "[^"]+"$', f'version = "{package_version}"', text, count=1
)
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
text = re.sub(
    r'_version = "[0-9A-Za-z.+-]+"',
    f'_version = "{package_version}"',
    text,
    count=1,
)
runtime.write_text(text)

for path in sorted(Path("tests").glob("test_*.py")):
    text = path.read_text()
    text = re.sub(
        r'"version": "[0-9A-Za-z.+-]+"',
        f'"version": "{package_version}"',
        text,
    )
    path.write_text(text)
PY

uv lock

scripts/update-changelog.sh "$VERSION"

echo "Prepared agent-remote-server v${VERSION}"
