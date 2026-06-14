#!/usr/bin/env bash
#
# update-readme.sh —— 使用 Claude CLI 根据本次提交的改动自动更新 README.md。
#
# 由 pre-commit hook 调用，也可手动运行：
#   ./scripts/update-readme.sh          # 依据暂存区改动（git diff --cached）更新
#   ./scripts/update-readme.sh --all    # 依据工作区全部改动更新
#
# README.md 同时维护「中文版」与「English Version」两部分，本脚本会让 Claude
# 同步更新两个版本，保持内容一致。更新后自动把 README.md 重新加入暂存区，
# 使其随本次提交一起进入版本库。
#
# 设计原则：README 更新属于「锦上添花」，任何失败都不应阻断提交（始终 exit 0）。
# 跳过本次更新：SKIP_README_UPDATE=1 git commit ...   或   git commit --no-verify
#
set -uo pipefail

# ---- 配置（可用环境变量覆盖）-------------------------------------------------
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
README_MODEL="${README_MODEL:-sonnet}"     # 控制速度/成本，可设为 opus
MAX_DIFF_BYTES="${MAX_DIFF_BYTES:-120000}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
README_FILE="${README_FILE:-$REPO_ROOT/README.md}"

# 允许临时跳过
if [[ "${SKIP_README_UPDATE:-}" == "1" ]]; then
  echo "[update-readme] SKIP_README_UPDATE=1，跳过 README 自动更新。"
  exit 0
fi

# ---- 收集 diff ---------------------------------------------------------------
if [[ "${1:-}" == "--all" ]]; then
  DIFF="$(git -C "$REPO_ROOT" diff HEAD)"
  SCOPE="工作区全部改动"
else
  DIFF="$(git -C "$REPO_ROOT" diff --cached)"
  SCOPE="暂存区改动"
fi

if [[ -z "${DIFF//[$' \t\n']/}" ]]; then
  echo "[update-readme] 没有检测到改动，跳过。"
  exit 0
fi

# README 不存在则无从更新
if [[ ! -f "$README_FILE" ]]; then
  echo "[update-readme] 未找到 $README_FILE，跳过。" >&2
  exit 0
fi

# 检查 claude 是否可用
if ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
  echo "[update-readme] 未找到 claude CLI（$CLAUDE_BIN），跳过 README 自动更新。" >&2
  echo "[update-readme] 如需启用，请安装 Claude Code 或设置 CLAUDE_BIN。" >&2
  exit 0
fi

# 截断超大 diff，避免超长输入
DIFF_BYTES=$(printf '%s' "$DIFF" | wc -c | tr -d ' ')
TRUNC_NOTE=""
if (( DIFF_BYTES > MAX_DIFF_BYTES )); then
  DIFF="$(printf '%s' "$DIFF" | head -c "$MAX_DIFF_BYTES")"
  TRUNC_NOTE="（注意：diff 过大已截断到前 ${MAX_DIFF_BYTES} 字节）"
fi

echo "[update-readme] 正在用 Claude（${README_MODEL}）依据${SCOPE}更新 README ${TRUNC_NOTE}..."

# ---- 构造 prompt -------------------------------------------------------------
# 让 Claude 直接读写工作区中的 README.md（授予 Read/Edit 工具）。
read -r -d '' PROMPT <<EOF
你是该项目的技术文档维护者。下面给出本次 git 提交的代码改动 diff。

请完成以下工作：
1. 用 Read 工具读取仓库根目录的 README.md（路径：${README_FILE}）。
2. 判断本次 diff 是否引入了需要反映到 README 的变化，例如：
     - 新增/删除/重命名的功能、命令、API、配置项、环境变量
     - 部署步骤、依赖、目录结构、使用方式的变化
     - 默认值、端口、行为的变化
   纯内部重构、注释改动、测试改动、不影响用户的实现细节，则【无需】改 README。
3. 如确需更新，用 Edit 工具就地修改 README.md：
     - README.md 同时维护「中文版」（## 中文版 起）与「English Version」
       （## English Version 起）两部分，必须【同步更新两个版本】，保持表述一致。
     - 只做与本次 diff 相关的、最小且准确的改动；不要臆造未在 diff 中体现的内容，
       不要大幅重写或调整无关章节，保持原有的标题层级、表格与排版风格。
4. 如果判断无需更新，请不要修改文件，直接说明原因即可。

完成后，简要用中文说明你做了什么（或为什么没改）。

以下是本次改动的 diff：
---
EOF
PROMPT="${PROMPT}
${DIFF}"

# ---- 调用 Claude（授予文件读写工具，自动接受编辑）--------------------------
OUTPUT="$(printf '%s' "$PROMPT" | "$CLAUDE_BIN" -p \
  --model "$README_MODEL" \
  --permission-mode acceptEdits \
  --allowedTools Read Edit Glob Grep \
  2>/dev/null)"
CLAUDE_RC=$?

if (( CLAUDE_RC != 0 )); then
  echo "[update-readme] Claude 调用失败（rc=$CLAUDE_RC），跳过 README 更新、不阻断提交。" >&2
  exit 0
fi

if [[ -n "$OUTPUT" ]]; then
  echo "----------------------------------------------------------------------"
  printf '%s\n' "$OUTPUT"
  echo "----------------------------------------------------------------------"
fi

# ---- 若 README 被改动，则重新加入暂存区 -------------------------------------
if ! git -C "$REPO_ROOT" diff --quiet -- "$README_FILE" 2>/dev/null; then
  git -C "$REPO_ROOT" add -- "$README_FILE"
  echo "[update-readme] ✅ 已更新 README.md 并重新加入暂存区，将随本次提交一起提交。"
else
  echo "[update-readme] ℹ️  README.md 无需更新。"
fi

exit 0
