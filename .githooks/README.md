# Git Hooks —— AI Code Review + 文档自动同步

提交前由 `pre-commit` 依次完成两件事：

1. **AI Code Review** —— 用 Claude CLI 审查暂存改动，发现 **严重问题（安全漏洞 / 明确 bug / 误提交密钥）** 时阻断提交。
2. **文档自动同步** —— 用 Claude CLI 依据本次改动按需同步项目文档，并把更新后的文档重新加入暂存区，随本次提交一起提交。**此步骤永不阻断提交。** 覆盖范围：
   - `README.md`（中文）与 `README.en.md`（English）—— 两个独立文件，同步更新、内容保持一致；
   - `docs/deploy.md` —— 部署文档；
   - `docs/` 下由 Claude 判断的其他「活文档」（架构 / 设计 / 使用说明），但**明确排除**带日期的历史快照、实测/验证报告、部署日志等存档类文档（如 `deploy-log-2026-06-14.md`、`*实测报告*`、`deploy-validation.md`、`POC-实测结果.md`），避免回改历史记录。

## 安装

hook 源文件就在版本库的 `.githooks/`（随仓库版本化、`git pull` 即更新）。启用方式是把 git 的 hook 目录指向它——**每位成员克隆后运行一次**即可（git 出于安全不允许仓库自动改本地配置，故需各自设一次；之后一直生效）：

```bash
./scripts/install-hooks.sh          # 等价于 git config core.hooksPath .githooks
```

> 与旧的"复制到 `.git/hooks/`"方式不同：现在 hook 逻辑随 `git pull` 自动更新，无需每次改动后重装。若 `.git/hooks/pre-commit` 有旧的复制残留，可删除（设了 `core.hooksPath` 后它不再生效）。

## 工作方式

`git commit` 触发 `.githooks/pre-commit`（由 `core.hooksPath` 指向）：

**① `scripts/code-review.sh`**

1. 取 `git diff --cached`，发给 Claude（默认 `sonnet`，纯推理不授予任何工具）。
2. 打印审查报告。
3. Claude 裁决为 `BLOCK`（存在 CRITICAL / 安全问题）时退出码非 0，阻断提交；否则放行。

**② `scripts/update-docs.sh`**

1. 取 `git diff --cached`，发给 Claude（默认 `sonnet`，授予 `Read`/`Edit`/`Glob`/`Grep` 工具，`acceptEdits` 自动接受改动）。
2. Claude 判断本次改动是否需要反映到文档（新增/删除功能、命令、API、配置项、环境变量、部署步骤等）；纯内部重构/注释/测试改动则不动。
3. 如需更新，就地修改核心文档（README 中英文两个版本同步、`docs/deploy.md`）及必要的其他活文档，随后把被改动的文件 `git add` 进暂存区。

两步在 claude CLI 不可用或调用失败时都**自动跳过、不阻断**，避免影响正常开发。

## 跳过检查

```bash
git commit --no-verify            # git 原生跳过所有 hook
SKIP_CODE_REVIEW=1 git commit ... # 仅跳过 AI code review
SKIP_DOC_UPDATE=1 git commit ...  # 仅跳过文档自动同步
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `REVIEW_MODEL` | `sonnet` | code review 用模型，可设为 `opus` |
| `DOC_MODEL` | `sonnet` | 文档同步用模型，可设为 `opus`（兼容旧名 `README_MODEL`） |
| `CLAUDE_BIN` | `claude` | claude 可执行文件路径 |
| `MAX_DIFF_BYTES` | `120000` | diff 超出则截断，避免输入过长 |

## 手动运行

```bash
./scripts/code-review.sh           # 审查暂存区
./scripts/code-review.sh --all     # 审查工作区全部改动

./scripts/update-docs.sh           # 依据暂存区改动同步文档
./scripts/update-docs.sh --all     # 依据工作区全部改动同步文档
```
