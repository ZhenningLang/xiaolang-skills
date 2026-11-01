#!/usr/bin/env bash
# Install skills from this repo by symlinking them into agent skill folders.
#
#   ./install.sh                      # all skills, all targets
#   ./install.sh brainstorm           # only the named skills
#   ./install.sh --target agents      # kilo / opencode / kimi / Grok Bot (~/.agents/skills)
#   ./install.sh --target claude      # Claude Code (~/.claude/skills)
#   ./install.sh --copy               # copy instead of symlink
#   ./install.sh --uninstall          # remove what this script installed
#   ./install.sh --dry-run            # print what would happen
#
# Existing files or folders that this script did not create are never touched.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_DIR="$REPO_DIR/skills"

target="all"
mode="link"
action="install"
dry_run=0
names=()

usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) target="${2:-}"; shift 2 ;;
    --copy) mode="copy"; shift ;;
    --uninstall) action="uninstall"; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    *) names+=("$1"); shift ;;
  esac
done

case "$target" in
  all) dests=("$HOME/.agents/skills" "$HOME/.claude/skills") ;;
  agents) dests=("$HOME/.agents/skills") ;;
  claude) dests=("$HOME/.claude/skills") ;;
  *) echo "--target must be all, agents or claude" >&2; exit 2 ;;
esac

if [[ ${#names[@]} -eq 0 ]]; then
  for d in "$SKILLS_DIR"/*/; do
    [[ -f "$d/SKILL.md" ]] && names+=("$(basename "$d")")
  done
fi

run() {
  if [[ $dry_run -eq 1 ]]; then echo "[dry-run] $*"; else "$@"; fi
}

status=0
for name in "${names[@]}"; do
  src="$SKILLS_DIR/$name"
  if [[ ! -f "$src/SKILL.md" ]]; then
    echo "skip $name: no skills/$name/SKILL.md" >&2; status=1; continue
  fi
  for dest_dir in "${dests[@]}"; do
    dest="$dest_dir/$name"
    marker="$dest/.installed-from-xiaolang-skills"
    ours=0
    if [[ -L "$dest" && "$(readlink "$dest")" == "$src" ]]; then ours=1; fi
    if [[ -d "$dest" && ! -L "$dest" && -f "$marker" ]]; then ours=1; fi

    if [[ "$action" == "uninstall" ]]; then
      if [[ $ours -eq 1 ]]; then
        run rm -rf "$dest"
        [[ $dry_run -eq 1 ]] && echo "would remove $dest" || echo "removed $dest"
      elif [[ -e "$dest" || -L "$dest" ]]; then
        echo "left $dest: not installed by this script" >&2
      fi
      continue
    fi

    if [[ -e "$dest" || -L "$dest" ]]; then
      if [[ $ours -eq 0 ]]; then
        echo "skip $dest: already exists and was not installed by this script" >&2
        status=1; continue
      fi
      run rm -rf "$dest"
    fi
    run mkdir -p "$dest_dir"
    if [[ "$mode" == "link" ]]; then
      run ln -s "$src" "$dest"
    else
      run cp -R "$src" "$dest"
      run touch "$marker"
    fi
    if [[ $dry_run -eq 1 ]]; then
      echo "would install $name -> $dest ($mode)"
    else
      echo "installed $name -> $dest ($mode)"
    fi
  done
done
exit $status
