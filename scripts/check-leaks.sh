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
  # not after '-' or a letter/digit, so git flags such as --write-tree are not private skill names
  '(^|[^-[:alnum:]])(think|guard|dev|write|readable|assist|fe)-[a-z][a-z-]+'
  'cloud-workspace'
)

# skills published here that share a prefix with the patterns above
public='think-(brainstorm|compare)'

joined="$(IFS='|'; echo "${patterns[*]}")"
hits="$( { git ls-files; git ls-files --others --exclude-standard; } \
  | grep -v '^scripts/check-leaks\.sh$' \
  | while IFS= read -r f; do [[ -f "$f" ]] && LC_ALL=C sed -E "s/$public//g" "$f" | grep -nEI "$joined" | sed "s|^|$f:|" || true; done )"
if [[ -n "$hits" ]]; then
  echo "private references found:" >&2
  echo "$hits" >&2
  exit 1
fi
echo "check-leaks: clean"
