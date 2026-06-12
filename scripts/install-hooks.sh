#!/usr/bin/env bash
#
# install-hooks.sh —— 把 scripts/hooks/ 下的 git hook 安装到 .git/hooks/。
# 团队成员克隆仓库后运行一次即可启用 pre-commit code review：
#   ./scripts/install-hooks.sh
#
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
SRC_DIR="$REPO_ROOT/scripts/hooks"
DST_DIR="$REPO_ROOT/.git/hooks"

if [[ ! -d "$SRC_DIR" ]]; then
  echo "未找到 $SRC_DIR" >&2
  exit 1
fi

for hook in "$SRC_DIR"/*; do
  name="$(basename "$hook")"
  dst="$DST_DIR/$name"
  cp "$hook" "$dst"
  chmod +x "$dst"
  echo "已安装 hook: $name -> $dst"
done

chmod +x "$REPO_ROOT/scripts/code-review.sh" 2>/dev/null || true
echo "完成。提交时将自动运行 AI code review。"
echo "临时跳过：git commit --no-verify  或  SKIP_CODE_REVIEW=1 git commit"
