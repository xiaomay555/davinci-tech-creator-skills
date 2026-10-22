#!/bin/bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd -P)"
codex_root="${CODEX_HOME:-$HOME/.codex}"
mkdir -p "$codex_root/skills"
status=0
legacy="$codex_root/skills/ai-talking-head-edit"
if [[ -L "$legacy" ]] && [[ "$(readlink "$legacy")" == "$root/skills/ai-talking-head-edit" ]]; then
  if rm "$legacy"; then
    echo "已移除旧 Skill 链接 ai-talking-head-edit"
  else
    status=1
  fi
fi
for name in ai-edit a-roll b-roll fx; do
  source_skill="$root/skills/$name"
  destination="$codex_root/skills/$name"
  if [[ ! -f "$source_skill/SKILL.md" ]]; then
    echo "共享 Skill 不存在：$source_skill" >&2
    status=1
    continue
  fi
  if [[ -L "$destination" ]]; then
    if [[ "$(readlink "$destination")" == "$source_skill" ]]; then
      echo "已连接 NAS Skill：$destination -> $source_skill"
    else
      echo "同名 Skill 已连接到别处，未覆盖：$destination" >&2
      status=1
    fi
    continue
  fi
  if [[ -e "$destination" ]]; then
    echo "同名 Skill 已存在，未覆盖：$destination" >&2
    status=1
    continue
  fi
  if ln -s "$source_skill" "$destination"; then
    echo "已连接 NAS Skill：$destination -> $source_skill"
  else
    status=1
  fi
done
exit "$status"
