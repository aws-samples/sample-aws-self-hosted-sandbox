#!/usr/bin/env bash
# 构建并推送控制面 + node-agent 镜像到 ECR
#
# 用法:
#   bash scripts/build_and_push.sh [--region us-east-1] [--cluster claude-sbx] \
#     [--control-plane-platform linux/arm64] [--node-agent-platform linux/amd64]
#
# 架构:
#   - control plane 默认 linux/arm64，运行在 On-Demand Graviton system 节点。
#   - node-agent 使用数据节点架构（c6g.metal=linux/arm64，i7i=linux/amd64）。
#   - 兼容旧参数 --platform / PLATFORM：同时覆盖两个镜像的平台。
#
# 前提:
#   - AWS CLI 已配置（有 ECR 推送权限）
#   - Docker 已运行。单架构原生构建无需 buildx;跨架构或多架构 manifest list 需要 buildx:
#       docker buildx create --use --name sbx-builder
#     在目标架构的 .metal 节点上原生构建最快（见 README Step 5 方式A）。
#   - node-agent 镜像不包含 SSH 私钥；exec 默认使用 vsock。需要 SSH fallback 时，
#     由部署系统在运行时以 Secret 只读挂载 /root/.ssh/id_ed25519。
#
# 注意:跨架构构建（如 x86 机器上构建 arm64,或反之）需 QEMU 模拟,速度较慢;
#       多平台 manifest list 模式会直接 push（buildx 限制,无法只 load 多架构镜像）。

set -euo pipefail

REGION="us-east-1"
CLUSTER="claude-sbx"
LEGACY_PLATFORM="${PLATFORM:-}"
CONTROL_PLANE_PLATFORM="${CONTROL_PLANE_PLATFORM:-${LEGACY_PLATFORM:-linux/arm64}}"
NODE_AGENT_PLATFORM="${NODE_AGENT_PLATFORM:-${LEGACY_PLATFORM:-linux/arm64}}"
COMPONENT="all"
while [[ $# -gt 0 ]]; do
  case $1 in
    --region)                 REGION="$2";                 shift 2 ;;
    --cluster)                CLUSTER="$2";                shift 2 ;;
    --platform)               CONTROL_PLANE_PLATFORM="$2"; NODE_AGENT_PLATFORM="$2"; shift 2 ;;
    --control-plane-platform) CONTROL_PLANE_PLATFORM="$2"; shift 2 ;;
    --node-agent-platform)    NODE_AGENT_PLATFORM="$2";    shift 2 ;;
    --component)              COMPONENT="$2";              shift 2 ;;
    *) shift ;;
  esac
done
if [[ ! "$COMPONENT" =~ ^(all|control-plane|node-agent)$ ]]; then
  echo "ERROR: --component must be all, control-plane, or node-agent" >&2
  exit 1
fi

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

# build_one <镜像 tag> <构建上下文目录> <目标平台>
#   - 多平台(platform 含逗号): buildx --push 一步构建并推送 manifest list
#   - 单平台: docker build --platform 后单独 push
build_one() {
  local tag="$1" ctx="$2" platform="$3"
  echo "==> Building ${tag} (platform=${platform})"
  if [[ "$platform" == *,* ]]; then
    docker buildx build \
      --platform "$platform" \
      -t "$tag" \
      --push \
      "$ctx"
  else
    if docker buildx version &>/dev/null; then
      docker buildx build \
        --platform "$platform" \
        -t "$tag" \
        --load \
        "$ctx"
    else
      docker build \
        --platform "$platform" \
        -t "$tag" \
        "$ctx"
    fi
    local expected_arch="${platform#linux/}"
    local actual_arch
    actual_arch=$(docker image inspect --format '{{.Architecture}}' "$tag")
    if [[ "$actual_arch" != "$expected_arch" ]]; then
      echo "ERROR: built ${tag} as ${actual_arch}, expected ${expected_arch}." >&2
      echo "The Docker builder ignored --platform; use buildx or a native ${expected_arch} builder." >&2
      exit 1
    fi
    docker push "$tag"
  fi
  echo "  Pushed: ${tag}"
}

# ---- 控制面镜像 ----
if [[ "$COMPONENT" == "all" || "$COMPONENT" == "control-plane" ]]; then
  build_one "${ECR_BASE}/sandbox-control-plane:latest" "${ROOT}/sandbox-api" "$CONTROL_PLANE_PLATFORM"
fi

# ---- node-agent 镜像 ----
if [[ "$COMPONENT" == "all" || "$COMPONENT" == "node-agent" ]]; then
  build_one "${ECR_BASE}/node-agent:latest" "${ROOT}/node-agent" "$NODE_AGENT_PLATFORM"
fi

echo ""
echo "==> Done. Use these in terraform apply:"
echo "  -var=\"control_plane_image=${ECR_BASE}/sandbox-control-plane:latest\""
echo "  -var=\"node_agent_image=${ECR_BASE}/node-agent:latest\""
echo "  -var=\"sandbox_image=${ECR_BASE}/claude-sbx:poc\""
