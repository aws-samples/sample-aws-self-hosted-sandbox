#!/usr/bin/env bash
#
# update-docs.sh —— 使用 Claude CLI 根据本次提交的改动自动同步项目文档，
# 防止「代码已更新、文档还是旧的」。
#
# 由 pre-commit hook 调用，也可手动运行：
#   ./scripts/update-docs.sh          # 依据暂存区改动（git diff --cached）更新
#   ./scripts/update-docs.sh --all    # 依据工作区全部改动更新
#
# 覆盖范围（核心三份，固定同步）：
#   - README.md      （中文）
#   - README.en.md   （English，与中文版保持内容一致）
#   - docs/deploy.md （部署文档）
# 此外让 Claude 判断 docs/ 下是否有其他「活文档」需要同步（如架构、设计、
# 使用说明），但【明确排除】带日期的历史快照 / 实测报告 / 部署日志等存档类文档
# （例如 deploy-log-2026-06-14.md、*实测报告*、deploy-validation.md、POC-实测结果.md），
# 这些是某一时间点的记录，不应被回改。
#
# 设计原则：文档更新属于「锦上添花」，任何失败都不应阻断提交（始终 exit 0）。
# 跳过本次更新：SKIP_DOC_UPDATE=1 git commit ...   或   git commit --no-verify
#（兼容旧变量名 SKIP_README_UPDATE）
#
set -uo pipefail

# 强制按字节处理，规避 bash 在 UTF-8 locale 下处理含大量中文的大 diff 时的
# 多字节性能陷阱与偶发 tokenization 问题（中文字符串仍以 UTF-8 字节原样输出）。
export LC_ALL=C

# ---- 配置（可用环境变量覆盖）-------------------------------------------------
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
# 兼容旧变量名 README_MODEL
DOC_MODEL="${DOC_MODEL:-${README_MODEL:-sonnet}}"     # 控制速度/成本，可设为 opus
MAX_DIFF_BYTES="${MAX_DIFF_BYTES:-120000}"
REPO_ROOT="$(git rev-parse --show-toplevel)"

# 核心文档清单（相对仓库根目录）。仅当文件存在时才纳入。
CORE_DOCS=(
  "README.md"
  "README.en.md"
  "docs/deploy.md"
)

# 允许临时跳过（兼容旧变量名）
if [[ "${SKIP_DOC_UPDATE:-}" == "1" || "${SKIP_README_UPDATE:-}" == "1" ]]; then
  echo "[update-docs] SKIP_DOC_UPDATE=1，跳过文档自动更新。"
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

# 注意：不要用 ${DIFF//[$' \t\n']/} 这类空白剔除展开来判空——在 UTF-8 locale 下
# 对含大量中文（多字节）的大 diff 会慢到分钟级（bash 多字节性能陷阱）。
# 用 tr（C 实现，快且对任意字节安全）剔除空白后判空。
if [[ -z "$(printf '%s' "$DIFF" | tr -d '[:space:]')" ]]; then
  echo "[update-docs] 没有检测到改动，跳过。"
  exit 0
fi

# 检查 claude 是否可用
if ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
  echo "[update-docs] 未找到 claude CLI（$CLAUDE_BIN），跳过文档自动更新。" >&2
  echo "[update-docs] 如需启用，请安装 Claude Code 或设置 CLAUDE_BIN。" >&2
  exit 0
fi

# 组装实际存在的核心文档清单
EXISTING_CORE=()
for rel in "${CORE_DOCS[@]}"; do
  [[ -f "$REPO_ROOT/$rel" ]] && EXISTING_CORE+=("$rel")
done
if (( ${#EXISTING_CORE[@]} == 0 )); then
  echo "[update-docs] 未找到任何核心文档，跳过。" >&2
  exit 0
fi

# 截断超大 diff，避免超长输入
DIFF_BYTES=$(printf '%s' "$DIFF" | wc -c | tr -d ' ')
TRUNC_NOTE=""
if (( DIFF_BYTES > MAX_DIFF_BYTES )); then
  DIFF="$(printf '%s' "$DIFF" | head -c "$MAX_DIFF_BYTES")"
  TRUNC_NOTE="（注意：diff 过大已截断到前 ${MAX_DIFF_BYTES} 字节）"
fi

CORE_LIST_STR="$(printf '  - %s\n' "${EXISTING_CORE[@]}")"

# 记录调用 Claude 前的文件内容哈希快照，调用后只把「哈希发生变化＝确实被 Claude
# 改过」的文件重新 add。这样绝不会误 add 用户在提交前就已存在、但本次并不打算
# 提交的 docs/ 无关改动。关心范围：核心三份 + docs/ 下所有 .md 活文档。
WATCH_FILES=("${EXISTING_CORE[@]}")
while IFS= read -r rel; do
  [[ -z "$rel" ]] && continue
  # 去重：核心三份已在列表里
  dup=0
  for c in "${EXISTING_CORE[@]}"; do [[ "$c" == "$rel" ]] && dup=1 && break; done
  (( dup )) || WATCH_FILES+=("$rel")
done < <(git -C "$REPO_ROOT" ls-files 'docs/*.md')

# hash_of <相对路径> —— 输出文件内容的 git blob 哈希；文件不存在则输出空串。
hash_of() {
  local f="$REPO_ROOT/$1"
  [[ -f "$f" ]] && git -C "$REPO_ROOT" hash-object -- "$1" 2>/dev/null || echo ""
}

declare -a SNAP_KEYS=() SNAP_VALS=()
for rel in "${WATCH_FILES[@]}"; do
  SNAP_KEYS+=("$rel")
  SNAP_VALS+=("$(hash_of "$rel")")
done

echo "[update-docs] 正在用 Claude（${DOC_MODEL}）依据${SCOPE}同步文档 ${TRUNC_NOTE}..."

# ---- 构造 prompt -------------------------------------------------------------
read -r -d '' PROMPT <<EOF
你是该项目的技术文档维护者。下面给出本次 git 提交的代码改动 diff，
你的任务是让文档与代码保持同步，防止「代码已更新、文档还是旧的」。

## 必须检查的核心文档（固定三份，存在则逐一核对）
${CORE_LIST_STR}
其中：
  - README.md 是中文版、README.en.md 是英文版，二者是【两个独立文件】，
    内容需保持一致；若某项变更需要反映到 README，请【同时更新中英文两个文件】。
  - docs/deploy.md 是部署文档，涉及部署步骤/依赖/环境变量/命令变化时需同步。

## 可选检查（docs/ 下的其他「活文档」）
除上述三份外，你可以判断 docs/ 下是否还有描述当前架构 / 设计 / 使用方式的
「活文档」需要同步更新（用 Glob/Grep 自行发现）。但【绝对不要】改动以下存档类
文档——它们是某一时间点的记录，回改会破坏历史真实性：
  - 文件名含日期的（如 *2026-06-14*、deploy-log-*）；
  - 实测 / 验证 / 结果类报告（如 *实测报告*、*实测结果*、deploy-validation.md、
    POC-实测结果.md、*e2e-实测*）；
  - 一次性的调研 / checklist / gap 分析类快照（除非其内容明显是需持续维护的说明）。
如不确定某个文档是否属于「活文档」，请【保守地不改】。

## 判断与改动原则
1. 先用 Read 读取需要核对的文档，再判断本次 diff 是否引入了需要反映到文档的变化，例如：
     - 新增/删除/重命名的功能、命令、API、配置项、环境变量；
     - 部署步骤、依赖、目录结构、使用方式的变化；
     - 默认值、端口、行为的变化。
   纯内部重构、注释改动、测试改动、不影响用户的实现细节，则【无需】改文档。
2. 如确需更新，用 Edit 就地修改，只做与本次 diff 相关的、最小且准确的改动；
   不要臆造未在 diff 中体现的内容，不要大幅重写或调整无关章节，
   保持原有的标题层级、表格与排版风格。
3. 如果判断无需更新任何文档，请不要修改文件，直接说明原因即可。

完成后，用中文简要说明你改了哪些文件、改了什么（或为什么没改）。

以下是本次改动的 diff：
---
EOF
PROMPT="${PROMPT}
${DIFF}"

# ---- 调用 Claude（授予文件读写工具，自动接受编辑）--------------------------
OUTPUT="$(printf '%s' "$PROMPT" | "$CLAUDE_BIN" -p \
  --model "$DOC_MODEL" \
  --permission-mode acceptEdits \
  --allowedTools Read Edit Glob Grep \
  2>/dev/null)"
CLAUDE_RC=$?

if (( CLAUDE_RC != 0 )); then
  echo "[update-docs] Claude 调用失败（rc=$CLAUDE_RC），跳过文档更新、不阻断提交。" >&2
  exit 0
fi

if [[ -n "$OUTPUT" ]]; then
  echo "----------------------------------------------------------------------"
  printf '%s\n' "$OUTPUT"
  echo "----------------------------------------------------------------------"
fi

# ---- 把被 Claude 改动过的文档重新加入暂存区 --------------------------------
# 与调用前的哈希快照逐一对比，只 add「哈希变化＝确实被 Claude 改过」的文件，
# 避免误 add 用户预先存在的无关 docs/ 改动。
CHANGED_ADDED=()
for i in "${!SNAP_KEYS[@]}"; do
  rel="${SNAP_KEYS[$i]}"
  before="${SNAP_VALS[$i]}"
  after="$(hash_of "$rel")"
  if [[ "$before" != "$after" ]]; then
    git -C "$REPO_ROOT" add -- "$rel"
    CHANGED_ADDED+=("$rel")
  fi
done

if (( ${#CHANGED_ADDED[@]} > 0 )); then
  echo "[update-docs] ✅ 已更新并重新加入暂存区的文档："
  printf '[update-docs]    - %s\n' "${CHANGED_ADDED[@]}"
  echo "[update-docs] 将随本次提交一起提交。"
else
  echo "[update-docs] ℹ️  文档无需更新。"
fi

exit 0
