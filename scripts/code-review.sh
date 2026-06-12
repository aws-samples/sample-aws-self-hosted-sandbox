#!/usr/bin/env bash
#
# code-review.sh —— 使用 Claude CLI 对暂存的改动做 AI code review。
#
# 由 pre-commit hook 调用，也可手动运行：
#   ./scripts/code-review.sh            # 审查暂存区（git diff --cached）
#   ./scripts/code-review.sh --all      # 审查工作区全部改动（含未暂存）
#
# 阻断策略：仅当 Claude 判定存在 critical / security 级别的严重问题时，
# 才以非 0 退出码阻断提交。一般性建议只警告，不阻断。
# 跳过本次检查：git commit --no-verify
#
set -uo pipefail

# ---- 配置（可用环境变量覆盖）-------------------------------------------------
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
REVIEW_MODEL="${REVIEW_MODEL:-sonnet}"   # 用 sonnet 控制速度/成本，可设为 opus
MAX_DIFF_BYTES="${MAX_DIFF_BYTES:-120000}"

# ---- 收集 diff ---------------------------------------------------------------
if [[ "${1:-}" == "--all" ]]; then
  DIFF="$(git diff HEAD)"
  SCOPE="工作区全部改动"
else
  DIFF="$(git diff --cached)"
  SCOPE="暂存区改动"
fi

if [[ -z "${DIFF//[$' \t\n']/}" ]]; then
  echo "[code-review] 没有检测到改动，跳过。"
  exit 0
fi

# 检查 claude 是否可用
if ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
  echo "[code-review] 未找到 claude CLI（$CLAUDE_BIN），跳过 AI 审查。" >&2
  echo "[code-review] 如需启用，请安装 Claude Code 或设置 CLAUDE_BIN。" >&2
  exit 0
fi

# 截断超大 diff，避免超长输入
DIFF_BYTES=$(printf '%s' "$DIFF" | wc -c | tr -d ' ')
TRUNC_NOTE=""
if (( DIFF_BYTES > MAX_DIFF_BYTES )); then
  DIFF="$(printf '%s' "$DIFF" | head -c "$MAX_DIFF_BYTES")"
  TRUNC_NOTE="（注意：diff 过大已截断到前 ${MAX_DIFF_BYTES} 字节）"
fi

echo "[code-review] 正在用 Claude（${REVIEW_MODEL}）审查${SCOPE} ${TRUNC_NOTE}..."

# ---- 构造 prompt -------------------------------------------------------------
read -r -d '' PROMPT <<'EOF'
你是一名严格但务实的资深代码审查者。下面是一次 git commit 的 diff。
请审查其中引入的改动，重点关注：
  - 安全漏洞（注入、鉴权缺失、密钥/凭证硬编码、路径穿越等）
  - 会导致崩溃或数据损坏的明确 bug
  - 资源泄漏、并发竞态、错误处理缺失
  - 误提交的密钥、密码、token、私钥

用中文输出一份简洁的审查报告：
  1. 先用要点列出发现的问题，每条标注严重级别 [CRITICAL]/[WARNING]/[INFO]，
     并指明文件与大致位置。
  2. 如无明显问题，直接说明“未发现明显问题”。

最后必须单独输出一行裁决，格式严格为下列之一：
  VERDICT: BLOCK     （存在 CRITICAL 或安全问题，应阻断提交）
  VERDICT: PASS      （没有需要阻断的问题）

以下是 diff：
---
EOF
PROMPT="${PROMPT}
${DIFF}"

# ---- 调用 Claude（纯文本推理，不授予任何工具）------------------------------
OUTPUT="$(printf '%s' "$PROMPT" | "$CLAUDE_BIN" -p \
  --model "$REVIEW_MODEL" \
  --allowedTools "" \
  2>/dev/null)"
CLAUDE_RC=$?

if (( CLAUDE_RC != 0 )) || [[ -z "$OUTPUT" ]]; then
  echo "[code-review] Claude 调用失败（rc=$CLAUDE_RC），跳过审查不阻断提交。" >&2
  exit 0
fi

echo "----------------------------------------------------------------------"
# 去掉裁决行后展示报告正文
printf '%s\n' "$OUTPUT" | grep -v '^VERDICT:'
echo "----------------------------------------------------------------------"

# ---- 解析裁决并决定退出码 ----------------------------------------------------
if printf '%s' "$OUTPUT" | grep -q '^VERDICT: BLOCK'; then
  echo "[code-review] ❌ 发现严重问题，提交被阻断。"
  echo "[code-review]    修复后重新提交，或用 'git commit --no-verify' 跳过检查。"
  exit 1
fi

echo "[code-review] ✅ 未发现需要阻断的严重问题。"
exit 0
