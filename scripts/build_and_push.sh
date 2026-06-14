#!/usr/bin/env bash
# 构建并推送控制面 + node-agent 镜像到 ECR（arm64 架构）
#
# 用法:
#   bash scripts/build_and_push.sh [--region us-east-1] [--cluster claude-sbx]
#
# 前提:
#   - AWS CLI 已配置（有 ECR 推送权限）
#   - Docker 已运行，并已配置 buildx 多平台支持：
#       docker buildx create --use --name arm64-builder --platform linux/arm64
#     或直接在 arm64 机器（M 系列 Mac / Graviton EC2）上运行此脚本（无需 buildx）
#
# 注意：在 x86 机器上跨平台构建 arm64 镜像需要 QEMU 支持且速度较慢。
#       推荐在 .metal 节点上通过 SSM 原生构建（见 README Step 5 方式A）。

set -euo pipefail

REGION="us-east-1"
CLUSTER="claude-sbx"
while [[ $# -gt 0 ]]; do
  case $1 in
    --region)  REGION="$2";  shift 2 ;;
    --cluster) CLUSTER="$2"; shift 2 ;;
    *) shift ;;
  esac
done

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

# ---- 控制面镜像 ----
echo "==> Building sandbox-control-plane"
docker build \
  --platform linux/arm64 \
  -t "${ECR_BASE}/sandbox-control-plane:latest" \
  "${ROOT}/sandbox-api"
docker push "${ECR_BASE}/sandbox-control-plane:latest"
echo "  Pushed: ${ECR_BASE}/sandbox-control-plane:latest"

# ---- node-agent 镜像 ----
echo "==> Building node-agent"
docker build \
  --platform linux/arm64 \
  -t "${ECR_BASE}/node-agent:latest" \
  "${ROOT}/node-agent"
docker push "${ECR_BASE}/node-agent:latest"
echo "  Pushed: ${ECR_BASE}/node-agent:latest"

echo ""
echo "==> Done. Use these in terraform apply:"
echo "  -var=\"control_plane_image=${ECR_BASE}/sandbox-control-plane:latest\""
echo "  -var=\"node_agent_image=${ECR_BASE}/node-agent:latest\""
echo "  -var=\"sandbox_image=${ECR_BASE}/claude-sbx:poc\""
