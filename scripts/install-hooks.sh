#!/usr/bin/env bash
#
# install-hooks.sh —— 启用团队共享 git hook。
#
# 做法:把 git 的 hook 目录指向版本库内的 .githooks/(core.hooksPath),
# 而不是复制到 .git/hooks/。好处:hook 随 git pull 自动更新,无需每次改动后重装。
#
# 团队成员克隆仓库后运行一次即可:
#   ./scripts/install-hooks.sh
#
# (git 出于安全不允许仓库自动改本地 git 配置,故 core.hooksPath 需每人各自设一次;
#  设置后除非有人手动改回,否则一直生效。)
#
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOKS_DIR=".githooks"   # 相对仓库根;git config 在仓库根执行时按此相对路径解析

if [[ ! -d "$REPO_ROOT/$HOOKS_DIR" ]]; then
  echo "未找到 $REPO_ROOT/$HOOKS_DIR" >&2
  exit 1
fi

# 确保 hook 及被调脚本可执行(clone 后权限位一般保留,保险起见再设一次)
chmod +x "$REPO_ROOT/$HOOKS_DIR"/* 2>/dev/null || true
chmod +x "$REPO_ROOT/scripts/code-review.sh" 2>/dev/null || true
chmod +x "$REPO_ROOT/scripts/update-docs.sh" 2>/dev/null || true

git -C "$REPO_ROOT" config core.hooksPath "$HOOKS_DIR"
echo "已设置 core.hooksPath = $HOOKS_DIR"

# 设了 hooksPath 后,.git/hooks/ 下的旧复制副本不再生效,留着易混淆 → 提示清理
if [[ -f "$REPO_ROOT/.git/hooks/pre-commit" ]]; then
  echo "提示:.git/hooks/pre-commit 是旧的复制安装残留,已不再生效,可删除:"
  echo "      rm \"$REPO_ROOT/.git/hooks/pre-commit\""
fi

echo ""
echo "完成。此后提交时将自动运行 AI code review,并按需自动同步文档(README 中/英文 + docs/deploy.md 等)。"
echo "hook 内容随 git pull 自动更新,无需重装。"
echo "临时跳过 code review:SKIP_CODE_REVIEW=1 git commit"
echo "临时跳过 文档更新:SKIP_DOC_UPDATE=1 git commit"
echo "跳过全部 hook:git commit --no-verify"
