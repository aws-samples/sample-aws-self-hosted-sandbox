#!/usr/bin/env bash
# 构建并推送控制面 + node-agent 镜像到 ECR
#
# 用法:
#   bash scripts/build_and_push.sh [--region us-east-1] [--cluster claude-sbx]
#
# 假设:
#   - AWS CLI 已配置
#   - Docker 已运行
#   - terraform/phase1 已 apply(ECR 仓库 claude-sbx 已存在)

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
