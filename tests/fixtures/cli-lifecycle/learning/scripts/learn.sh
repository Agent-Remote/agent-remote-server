#!/bin/sh
set -eu
root=/home/runtime/.claude/skills
test "$(cat /workspace/local-proof)" = "$1"
printf '%s' "$1" > "$root/learning/memory.txt"
printf '\000\377\001binary' > "$root/learning/state.db"
printf '\nLearned in first session\n' >> "$root/learning/SKILL.md"
chmod 750 "$root/learning/memory.txt"
rm "$root/learning/remove-me"
mkdir -p "$root/learning/empty" "$root/local-proof"
cat > "$root/local-proof/SKILL.md" <<'SKILL'
---
name: local-proof
description: account generated
---
Local state
SKILL
ln -s memory.txt "$root/learning/link"
