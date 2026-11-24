#!/usr/bin/env bash
# Install skills and tools from this repo by symlinking them into place.
#
#   ./install.sh                      # all skills and tools, all targets
#   ./install.sh think-brainstorm     # only the named skills or tools
#   ./install.sh --target agents      # skills for kilo / opencode / kimi / Grok Bot (~/.agents/skills)
#   ./install.sh --target claude      # skills for Claude Code (~/.claude/skills)
#   ./install.sh --target tools       # command-line tools only (~/.local/bin, or $XDG_BIN_HOME)
#   ./install.sh --copy               # copy instead of symlink
#   ./install.sh --uninstall          # remove what this script installed
#   ./install.sh --dry-run            # print what would happen
#
# Existing files or folders that this script did not create are never touched.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_DIR="$REPO_DIR/skills"
TOOLS_DIR="$REPO_DIR/tools"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
MARKER=".installed-from-xiaolang-skills"

target="all"
mode="link"
action="install"
dry_run=0
names=()

usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; }

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

with_tools=1
case "$target" in
  all) dests=("$HOME/.agents/skills" "$HOME/.claude/skills") ;;
  agents) dests=("$HOME/.agents/skills"); with_tools=0 ;;
  claude) dests=("$HOME/.claude/skills"); with_tools=0 ;;
  tools) dests=() ;;
  *) echo "--target must be all, agents, claude or tools" >&2; exit 2 ;;
esac

# A tool lives in tools/<name>/ and runs from scripts/<name with - as _>.py.
tool_entry() { echo "$TOOLS_DIR/$1/scripts/${1//-/_}.py"; }

if [[ ${#names[@]} -eq 0 ]]; then
  for d in "$SKILLS_DIR"/*/; do
    [[ -f "$d/SKILL.md" ]] && names+=("$(basename "$d")")
  done
  for d in "$TOOLS_DIR"/*/; do
    [[ -d "$d" ]] && names+=("$(basename "$d")")
  done
fi

run() {
  if [[ $dry_run -eq 1 ]]; then echo "[dry-run] $*"; else "$@"; fi
}

say() {  # say <done text> <would text>
  if [[ $dry_run -eq 1 ]]; then echo "$2"; else echo "$1"; fi
}

status=0

# place <name> <src> <dest> <marker>: link or copy src to dest, never touching what this script did not create.
place() {
  local name="$1" src="$2" dest="$3" marker="$4" ours=0
  if [[ -L "$dest" && "$(readlink "$dest")" == "$src" ]]; then ours=1; fi
  if [[ -e "$dest" && ! -L "$dest" && -f "$marker" ]]; then ours=1; fi

  if [[ "$action" == "uninstall" ]]; then
    if [[ $ours -eq 1 ]]; then
      run rm -rf "$dest"
      [[ -f "$marker" ]] && run rm -f "$marker"
      say "removed $dest" "would remove $dest"
    elif [[ -e "$dest" || -L "$dest" ]]; then
      echo "left $dest: not installed by this script" >&2
    fi
    return 0
  fi

  if [[ -e "$dest" || -L "$dest" ]]; then
    if [[ $ours -eq 0 ]]; then
      echo "skip $dest: already exists and was not installed by this script" >&2
      status=1; return 0
    fi
    run rm -rf "$dest"
  fi
  run mkdir -p "$(dirname "$dest")"
  if [[ "$mode" == "link" ]]; then
    run ln -s "$src" "$dest"
  else
    run cp -R "$src" "$dest"
    run touch "$marker"
  fi
  say "installed $name -> $dest ($mode)" "would install $name -> $dest ($mode)"
}

for name in "${names[@]}"; do
  if [[ -f "$SKILLS_DIR/$name/SKILL.md" ]]; then
    for dest_dir in ${dests[@]+"${dests[@]}"}; do
      place "$name" "$SKILLS_DIR/$name" "$dest_dir/$name" "$dest_dir/$name/$MARKER"
    done
  elif [[ -d "$TOOLS_DIR/$name" ]]; then
    [[ $with_tools -eq 1 ]] || continue
    entry="$(tool_entry "$name")"
    if [[ ! -f "$entry" ]]; then
      echo "skip $name: no ${entry#"$REPO_DIR"/}" >&2; status=1; continue
    fi
    place "$name" "$entry" "$BIN_DIR/$name" "$BIN_DIR/.$name$MARKER"
  else
    echo "skip $name: no skills/$name/SKILL.md or tools/$name/" >&2; status=1
  fi
done

if [[ $with_tools -eq 1 && "$action" == "install" && ":$PATH:" != *":$BIN_DIR:"* ]]; then
  echo "note: $BIN_DIR is not on PATH; add it so that \`git tidy\` and other tools are found" >&2
fi
exit $status
