#!/bin/sh
set -eu
root=/home/runtime/.claude/skills
test "$(cat "$root/learning/memory.txt")" = "$1"
test "$root/learning/memory.txt" -ef /account/.claude/skills/learning/memory.txt
test "$(stat -c %a "$root/learning/memory.txt")" = 750
test ! -e "$root/learning/remove-me"
test -d "$root/learning/empty"
test "$(readlink "$root/learning/link")" = memory.txt
test -f "$root/local-proof/SKILL.md"
grep -q 'Learned in first session' "$root/learning/SKILL.md"
printf '\000\377\001binary' | cmp - "$root/learning/state.db"
