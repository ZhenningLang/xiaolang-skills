#!/usr/bin/env bash
# Fail if tracked files mention private paths, private tools or private skills.
# Run before every push. Add a pattern here whenever a new private name shows up.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

patterns=(
  '/Users/'
  '~/\.dotfiles'
  '\.dotfiles/'
  'zhenninglang'
  '\blr\.py\b'
  '\bdc\.py\b'
  'dotfiles-capsule'
  'dotfiles-memory'
  '\b(think|guard|dev|write|readable|assist|fe)-[a-z][a-z-]+'
  'cloud-workspace'
)

joined="$(IFS='|'; echo "${patterns[*]}")"
hits="$( { git ls-files; git ls-files --others --exclude-standard; } \
  | grep -v '^scripts/check-leaks\.sh$' \
  | while IFS= read -r f; do [[ -f "$f" ]] && grep -nEIH "$joined" "$f" || true; done )"
if [[ -n "$hits" ]]; then
  echo "private references found:" >&2
  echo "$hits" >&2
  exit 1
fi
echo "check-leaks: clean"
