# Git Hooks —— AI Code Review + README 自动更新

提交前由 `pre-commit` 依次完成两件事：

1. **AI Code Review** —— 用 Claude CLI 审查暂存改动，发现 **严重问题（安全漏洞 / 明确 bug / 误提交密钥）** 时阻断提交。
2. **README 自动更新** —— 用 Claude CLI 依据本次改动按需更新 `README.md`（同步「中文版」与「English Version」两部分），并把更新后的 README 重新加入暂存区，随本次提交一起提交。**此步骤永不阻断提交。**

## 安装

克隆仓库后运行一次（`.git/hooks/` 不纳入版本控制，每位成员需各自安装）：

```bash
./scripts/install-hooks.sh
```

## 工作方式

`git commit` 触发 `.git/hooks/pre-commit`：

**① `scripts/code-review.sh`**

1. 取 `git diff --cached`，发给 Claude（默认 `sonnet`，纯推理不授予任何工具）。
2. 打印审查报告。
3. Claude 裁决为 `BLOCK`（存在 CRITICAL / 安全问题）时退出码非 0，阻断提交；否则放行。

**② `scripts/update-readme.sh`**

1. 取 `git diff --cached`，发给 Claude（默认 `sonnet`，授予 `Read`/`Edit`/`Glob`/`Grep` 工具，`acceptEdits` 自动接受改动）。
2. Claude 判断本次改动是否需要反映到 README（新增/删除功能、命令、API、配置项、环境变量、部署步骤等）；纯内部重构/注释/测试改动则不动。
3. 如需更新，就地修改 `README.md`，**中英文两个版本同步更新**，随后 `git add README.md`。

两步在 claude CLI 不可用或调用失败时都**自动跳过、不阻断**，避免影响正常开发。

## 跳过检查

```bash
git commit --no-verify               # git 原生跳过所有 hook
SKIP_CODE_REVIEW=1 git commit ...    # 仅跳过 AI code review
SKIP_README_UPDATE=1 git commit ...  # 仅跳过 README 自动更新
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `REVIEW_MODEL` | `sonnet` | code review 用模型，可设为 `opus` |
| `README_MODEL` | `sonnet` | README 更新用模型，可设为 `opus` |
| `CLAUDE_BIN` | `claude` | claude 可执行文件路径 |
| `MAX_DIFF_BYTES` | `120000` | diff 超出则截断，避免输入过长 |
| `README_FILE` | `<repo>/README.md` | 待更新的 README 路径 |

## 手动运行

```bash
./scripts/code-review.sh           # 审查暂存区
./scripts/code-review.sh --all     # 审查工作区全部改动

./scripts/update-readme.sh         # 依据暂存区改动更新 README
./scripts/update-readme.sh --all   # 依据工作区全部改动更新 README
```
