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
  # 跳过说明文档等非 hook 文件
  [[ "$name" == "README.md" ]] && continue
  dst="$DST_DIR/$name"
  cp "$hook" "$dst"
  chmod +x "$dst"
  echo "已安装 hook: $name -> $dst"
done

chmod +x "$REPO_ROOT/scripts/code-review.sh" 2>/dev/null || true
chmod +x "$REPO_ROOT/scripts/update-docs.sh" 2>/dev/null || true
echo "完成。提交时将自动运行 AI code review，并按需自动同步文档（README 中/英文 + docs/deploy.md 等）。"
echo "临时跳过 code review：SKIP_CODE_REVIEW=1 git commit"
echo "临时跳过 文档更新：SKIP_DOC_UPDATE=1 git commit"
echo "跳过全部 hook：git commit --no-verify"
