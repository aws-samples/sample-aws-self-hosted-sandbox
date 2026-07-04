#!/usr/bin/env bash
# 构建并推送控制面 + node-agent 镜像到 ECR
#
# 用法:
#   bash scripts/build_and_push.sh [--region us-east-1] [--cluster claude-sbx] [--platform linux/arm64]
#
# 架构（--platform / PLATFORM 环境变量,默认 linux/arm64）:
#   - linux/arm64  : Graviton 节点（默认）
#   - linux/amd64  : Intel x86 节点
#   - linux/arm64,linux/amd64 : 同时构建多架构 manifest list(需 buildx,见下)
#
# 前提:
#   - AWS CLI 已配置（有 ECR 推送权限）
#   - Docker 已运行。单架构原生构建无需 buildx;跨架构或多架构 manifest list 需要 buildx:
#       docker buildx create --use --name sbx-builder
#     在目标架构的 .metal 节点上原生构建最快（见 README Step 5 方式A）。
#
# 注意:跨架构构建（如 x86 机器上构建 arm64,或反之）需 QEMU 模拟,速度较慢;
#       多平台 manifest list 模式会直接 push（buildx 限制,无法只 load 多架构镜像）。

set -euo pipefail

REGION="us-east-1"
CLUSTER="claude-sbx"
PLATFORM="${PLATFORM:-linux/arm64}"
while [[ $# -gt 0 ]]; do
  case $1 in
    --region)   REGION="$2";   shift 2 ;;
    --cluster)  CLUSTER="$2";  shift 2 ;;
    --platform) PLATFORM="$2"; shift 2 ;;
    *) shift ;;
  esac
done

# 多平台（逗号分隔）必须用 buildx 且直接 push;单平台用普通 docker build + push
MULTIARCH=0
[[ "$PLATFORM" == *,* ]] && MULTIARCH=1

# ---------- 前置检查 ----------
if ! command -v docker &>/dev/null; then
  echo "ERROR: Docker not found."
  echo ""
  echo "在非 arm64 本地机器上，请用方式 B（SSM 在 .metal 节点上原生构建）："
  echo "  详见 README Step 5 方式 B"
  echo ""
  echo "或在 arm64 机器（M 系列 Mac/Graviton EC2）上重新运行此脚本。"
  exit 1
fi

if ! docker buildx version &>/dev/null; then
  echo "WARNING: docker buildx 未找到，将尝试使用 docker build --platform"
fi

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ECR_BASE="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

echo "==> ECR login"
aws ecr get-login-password --region "$REGION" | \
  docker login --username AWS --password-stdin "$ECR_BASE"

# 确保仓库存在
for REPO in sandbox-control-plane node-agent; do
  aws ecr describe-repositories --repository-names "$REPO" --region "$REGION" \
    >/dev/null 2>&1 || \
  aws ecr create-repository --repository-name "$REPO" --region "$REGION" \
    --query 'repository.repositoryUri' --output text
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# build_one <镜像 tag> <构建上下文目录>
#   - 多平台(PLATFORM 含逗号): buildx --push 一步构建并推送 manifest list
#   - 单平台: docker build --platform 后单独 push
build_one() {
  local tag="$1" ctx="$2"
  echo "==> Building ${tag} (platform=${PLATFORM})"
  if [[ "$MULTIARCH" == "1" ]]; then
    docker buildx build \
      --platform "$PLATFORM" \
      -t "$tag" \
      --push \
      "$ctx"
  else
    docker build \
      --platform "$PLATFORM" \
      -t "$tag" \
      "$ctx"
    docker push "$tag"
  fi
  echo "  Pushed: ${tag}"
}

# ---- 控制面镜像 ----
build_one "${ECR_BASE}/sandbox-control-plane:latest" "${ROOT}/sandbox-api"

# ---- node-agent 镜像 ----
build_one "${ECR_BASE}/node-agent:latest" "${ROOT}/node-agent"

echo ""
echo "==> Done. Use these in terraform apply:"
echo "  -var=\"control_plane_image=${ECR_BASE}/sandbox-control-plane:latest\""
echo "  -var=\"node_agent_image=${ECR_BASE}/node-agent:latest\""
echo "  -var=\"sandbox_image=${ECR_BASE}/claude-sbx:poc\""
