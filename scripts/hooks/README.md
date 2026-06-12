# Git Hooks —— AI Code Review

提交前用 Claude CLI 自动审查暂存的改动，发现 **严重问题（安全漏洞 / 明确 bug / 误提交密钥）** 时阻断提交。

## 安装

克隆仓库后运行一次（`.git/hooks/` 不纳入版本控制，每位成员需各自安装）：

```bash
./scripts/install-hooks.sh
```

## 工作方式

`git commit` 触发 `.git/hooks/pre-commit` → 调用 `scripts/code-review.sh`：

1. 取 `git diff --cached`，发给 Claude（默认 `sonnet`，纯推理不授予任何工具）。
2. 打印审查报告。
3. Claude 裁决为 `BLOCK`（存在 CRITICAL / 安全问题）时退出码非 0，阻断提交；否则放行。

claude CLI 不可用或调用失败时**自动跳过、不阻断**，避免影响正常开发。

## 跳过检查

```bash
git commit --no-verify             # git 原生跳过所有 hook
SKIP_CODE_REVIEW=1 git commit ...  # 仅跳过本 code review
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `REVIEW_MODEL` | `sonnet` | 审查用模型，可设为 `opus` |
| `CLAUDE_BIN` | `claude` | claude 可执行文件路径 |
| `MAX_DIFF_BYTES` | `120000` | diff 超出则截断，避免输入过长 |

## 手动运行

```bash
./scripts/code-review.sh          # 审查暂存区
./scripts/code-review.sh --all    # 审查工作区全部改动
```
