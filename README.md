# AWS Self-Hosted AI Agent Sandbox Platform

> Build your own Fly.io-style Firecracker microVM sandbox on AWS — lower cost, full control, data stays in your account.

[中文版](#中文版) | [English Version](#english-version)

---

## 中文版

### 项目简介

在 AWS 上复刻 Fly.io Firecracker microVM 架构，以更低成本、更高可控性运行 Claude Code 及各类 AI Agent。

- **真实 microVM 隔离**：每个沙盒运行在独立的 Firecracker/Kata guest 内核，与裸机行为完全一致
- **后端可插拔**：同一套 API，底层可切换 Kata-on-EKS（编排优先）或裸 Firecracker（成本优先）
- **快照驱动成本控制**：空闲沙盒快照挂起释放内存，访问时 ~1.2s 恢复
- **Fly Machines 风格 API**：create/wait/suspend/resume/exec/locate，幂等键、乐观锁、capability 模型
- **凭据零进沙盒**：Bedrock 凭据仅在 LiteLLM Pod 的 IRSA 角色，沙盒永远看不到真实 key

### 适用场景

| 场景 | 说明 |
|---|---|
| **Claude Code** | fork/exec 密集、文件监听重、嵌套进程 — microVM 保障与裸机一致的行为 |
| **OpenClaw / Hermes** | 会话式智能助理，需多租户隔离、按需扩缩 |
| **OpenAI Codex / 代码生成 Agent** | 任意代码执行，VM 级安全边界，防逃逸 |
| **长程 Agentic 任务** | 任务暂停恢复、工作流中断续跑、快照持久化 session 状态 |
| **SaaS 沙盒服务** | 向终端用户暴露隔离执行环境，多租户、按量计费 |
| **CI/CD 沙盒** | 隔离的构建/测试环境，npm install / docker build / 任意端口服务 |

### 核心优势

#### 1. 裸机保真度（microVM 不是容器）

```
guest kernel: 6.18.28   ≠   node kernel: 6.1.172   ✅ 真独立内核
nproc: 3 (guest 配额)   ≠   宿主: 64              ✅ CPU 视图隔离
inotify 配额: 独立                                  ✅ 密集容器不会耗尽
root 可绑 80 端口、dnf 装包、嵌套 docker            ✅ 完整 root 无 seccomp 裁剪
```

#### 2. 成本控制：快照 = 成本杠杆

| 模式 | 每沙盒内存占用 | 月均成本估算 |
|---|---|---|
| 24×7 常驻 | 1.5 GiB 有效 | ~$22/沙盒·月（c6g.metal 按需） |
| 快照空闲回收 | ~50 MB（空载驻留） | **~$4/沙盒·月** |
| 暖池（Savings Plan） | — | 再降 40-60% |

- 单台 `c6g.metal`（128 GiB）：空载可承载 **400+ 沙盒**，活跃工作集约 **75 沙盒**
- **resume 延迟 1.2s 实测**，用户无感知
- **~10000 并发估算：134 台 c6g.metal，Savings Plan 后约 $1,700-$4,500/月**

#### 3. API 开发者友好性

```bash
# 创建沙盒（幂等）
POST /sandboxes
{"image": "...", "cpu": 2, "mem_mib": 4096, "idempotency_key": "req-123"}

# 等待就绪
GET /sandboxes/{id}/wait?state=running&timeout=30

# 挂起（快照 + 释放内存）
POST /sandboxes/{id}/suspend   # → snapshot_s3, restore_time

# 恢复（1.2s）
POST /sandboxes/{id}/resume

# 执行命令
POST /sandboxes/{id}/exec
{"cmd": "npm test"}
```

#### 4. 安全性
- VM 级隔离：每沙盒独立 guest 内核，无共享宿主内核泄漏
- 凭据零进沙盒：Bedrock 凭据只在 LiteLLM IRSA
- Bearer token 认证，多 key 支持多租户
- Karpenter 空闲整合：.metal 节点闲置 30 分钟自动回收

---

### 与主流方案对比

| 维度 | 本方案（AWS 自建） | E2B | Fly.io Machines | AWS AgentCore |
|---|---|---|---|---|
| **隔离层** | Firecracker/Kata microVM | Firecracker microVM | Firecracker microVM | 容器（共享内核）|
| **裸机保真度** | ✅ 最高 | ✅ 高 | ✅ 高 | ❌ 容器行为偏差 |
| **自定义镜像** | ✅ 任意 ECR | ✅ | ✅ | ❌ 受限 |
| **任意端口** | ✅ 通配符子域名 + 共享 NLB | ✅ | ✅ | ❌ |
| **24×7 长驻** | ✅ | ✅ | ✅ | ❌ 有 TTL |
| **快照 suspend/resume** | ✅ 实测 1.2s | ✅ | ✅ | ❌ |
| **凭据隔离** | ✅ LiteLLM IRSA（已落地）| ✅ | ✅ | N/A |
| **数据主权** | ✅ 数据留 AWS 账号内 | ❌ 第三方 | ❌ 第三方 | ✅ |
| **K8s 生态集成** | ✅ 原生 | ❌ | ❌ | ❌ |
| **月费（1000并发，估算）** | **~$170-$450** | ~$800+ | ~$600+ | 按调用计费 |

> 月费基于 c6g.metal Savings Plan（~-50%）+ 快照空闲回收估算，未含流量/存储费用。

---

### 架构概览

```
┌─ EKS cluster ─────────────────────────────────────────────────────┐
│                                                                      │
│  托管节点组（系统节点）          Karpenter c6g.metal 节点（沙盒）     │
│  ┌──────────────────────────┐      ┌───────────────────────────┐   │
│  │ sandbox-control-plane    │ HTTP │  Kata microVM (kata-qemu) │   │
│  │ (Deployment, IRSA)       │─────►│  kata 由 UserData 预装    │   │
│  │  KataDriver (k8s client) │      │  node-agent DaemonSet     │   │
│  │  FirecrackerDriver       │      │  jailer / tap / snapshot  │   │
│  │  WarmPool                │      └───────────────────────────┘   │
│  │  无状态 → DynamoDB        │                                       │
│  └──────────────────────────┘                                       │
│         ↑ ingress-nginx (NLB)                                       │
│         api.sbx.<domain>  ←── 生产外部访问（需 AWS LB Controller）  │
│                                                                      │
│  DynamoDB  LiteLLM(Bedrock代理)  Karpenter(.metal自动扩缩+预装kata)  │
└──────────────────────────────────────────────────────────────────────┘
```

---

### 快速开始（Agent 部署指南）

> 将以下内容复制给 Claude Code / Cursor / 任意支持代码执行的 Agent，即可引导完整部署。

```
你是一名 DevOps 工程师，需要在 AWS 上部署一套 AI Agent 沙盒平台。
请严格按照以下步骤执行，遇到错误先排查再继续。

【前提条件】
- AWS CLI 已配置（有权限创建 EKS/EC2/IAM/DynamoDB/ECR/S3）
- kubectl, terraform(>=1.5), helm, git 已安装
- 已申请好足够的 EC2 vCPU 服务配额（c6g.metal = 64 vCPU，默认配额不够需提前申请）

【Step 0: 克隆代码库】
git clone https://github.com/teaguexiao/aws-self-hosted-sandbox.git
cd aws-self-hosted-sandbox
export AWS_REGION=us-east-1

【Step 1: 创建 DynamoDB 状态表】
cd terraform/stage1-dynamodb
terraform init && terraform apply -auto-approve
# 验证：
aws dynamodb list-tables --region us-east-1 | grep claude-sbx

【Step 2: 创建 EKS 集群 + .metal 节点组】
cd ../phase3
MY_IP=$(curl -s https://checkip.amazonaws.com)
terraform init && terraform apply -auto-approve \
  -var="endpoint_public_access_cidrs=[\"${MY_IP}/32\"]"
# EKS 控制面约 10-12 分钟，加 .metal 节点组冷启动整体约 15 分钟，等待 Ready
aws eks update-kubeconfig --name claude-sbx --region us-east-1
kubectl wait node --all --for=condition=Ready --timeout=900s

【Step 3: 创建 kata-qemu RuntimeClass（不要用 kata-deploy DaemonSet！）】
#
# ⚠️⚠️ 重要架构决策：本方案【不】使用官方 kata-deploy DaemonSet 来装 Kata。
#
# 原因（已实测定位的阻断性问题）：
#   kata-deploy 安装末尾会在【已运行 kubelet + 多个容器】的节点上执行
#   `systemctl restart containerd`。在 c6g.metal + AL2023 上，这会留下孤儿
#   containerd-shim 并导致【整个裸金属节点 hang 约 12 分钟后才重新开机】
#   （containerd 重启本身只要 200ms，慢的是节点级挂死）。这期间 EC2 reachability
#   检查失败，托管节点组/ASG 会把它当 unhealthy 反复替换 → 节点替换死循环。
#
# 正确做法：把 Kata 安装【前置到节点 bootstrap 阶段】（kubelet 向 EKS 注册之前），
#   由 Karpenter EC2NodeClass.userData 完成（见 Step 7）。bootstrap 阶段容器为空、
#   EKS 还看不到该节点，containerd 重启瞬时完成、零抖动（实测新 metal 节点 30-60s Ready）。
#
# 因此 Step 3 只需创建 sandbox pod 调度用的 RuntimeClass（集群级对象，不碰节点）：

kubectl apply -f - <<'RUNTIMECLASS'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: kata-qemu
handler: kata-qemu
overhead:
  podFixed:
    cpu: 250m
    memory: 160Mi
scheduling:
  # 只调度到 Step 7 由 Karpenter UserData 预装好 Kata 的 .metal 节点
  nodeSelector:
    katacontainers.io/kata-runtime: "true"
RUNTIMECLASS

kubectl get runtimeclass kata-qemu   # 应能看到 kata-qemu

【Step 4: 安装 ingress-nginx（共享 NLB）】
# 注意：必须指定 namespace
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx --create-namespace \
  --set controller.service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-type"=nlb \
  --set controller.ingressClassResource.default=true
# 等待 NLB 分配外部地址（需 1-3 分钟）
kubectl get svc -n ingress-nginx ingress-nginx-controller --watch

【Step 5: 创建 ECR 仓库并构建 arm64 镜像】
# 注：sandbox 镜像仓库 claude-sbx 已由 phase3 (Step 2) 自动创建，这里只需建以下两个：
ACCT=$(aws sts get-caller-identity --query Account --output text)
aws ecr create-repository --repository-name sandbox-control-plane --region us-east-1 2>/dev/null || true
aws ecr create-repository --repository-name node-agent --region us-east-1 2>/dev/null || true

# 方式 A：本地 arm64 机器（M 系列 Mac）或 arm64 EC2 直接构建
bash scripts/build_and_push.sh

# 方式 B：在 .metal 节点上原生构建（x86 机器无 buildx 时推荐）
# 需要 Step 2 的 .metal 节点已 Ready，node-agent 已通过 SSM 可访问
# 详见 scripts/build_and_push.sh 注释中的 SSM 构建方式

【Step 6: 部署控制面 + LiteLLM + Karpenter IAM】
# sandbox_domain 传入的是"子域名根"，控制面将暴露在 api.<sandbox_domain>
# 例如传 sbx.example.com，则访问地址为 http://api.sbx.example.com
cd terraform/stage2-control-plane
terraform init
ACCT=$(aws sts get-caller-identity --query Account --output text)
S3_BUCKET="my-sandbox-snapshots-${ACCT}"
aws s3 mb s3://${S3_BUCKET} --region us-east-1 2>/dev/null || true

# 注意：Step 4 已手动安装 ingress-nginx，必须加 create_ingress_nginx=false 避免冲突
terraform apply -auto-approve \
  -var="sandbox_image=public.ecr.aws/amazonlinux/amazonlinux:2023" \
  -var="control_plane_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/sandbox-control-plane:latest" \
  -var="node_agent_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/node-agent:latest" \
  -var="snapshot_s3_bucket=${S3_BUCKET}" \
  -var="enable_fargate=false" \
  -var="create_ingress_nginx=false" \
  -var="sandbox_domain=sbx.example.com"   # 控制面将暴露在 api.sbx.example.com

# Terraform 会自动：
# - 创建 IRSA 角色（控制面/node-agent/LiteLLM/Karpenter）
# - 创建 Karpenter Worker Node IAM Role（节点加入集群所需）
# - 创建 EKS Access Entry（karpenter_node role → EC2_LINUX 类型）
#   ← 这是让 Karpenter 启动的节点能 join 集群的关键；没有它 kubelet TLS bootstrap 会被拒绝
# - 部署 K8s 资源（sandbox-system/litellm namespace）
# - 创建控制面 Ingress（api.<sandbox_domain>）
# - 通过 null_resource 部署 Karpenter NodePool（install_karpenter=true 时）

【Step 7: 手动安装 Karpenter】
# Karpenter Helm 使用 OCI registry，某些环境下需要移除 Docker credential store：
python3 -c "
import json, pathlib
cfg = pathlib.Path.home() / '.docker/config.json'
if cfg.exists():
    d = json.loads(cfg.read_text())
    d.pop('credsStore', None)
    cfg.write_text(json.dumps(d))
    print('credsStore removed')
"

ACCT=$(aws sts get-caller-identity --query Account --output text)
CLUSTER_ENDPOINT=$(aws eks describe-cluster --name claude-sbx --query 'cluster.endpoint' --output text)
KARPENTER_ROLE_ARN="arn:aws:iam::${ACCT}:role/claude-sbx-karpenter"

helm upgrade --install karpenter \
  oci://public.ecr.aws/karpenter/karpenter --version 1.3.3 \
  --namespace karpenter --create-namespace \
  --set "settings.clusterName=claude-sbx" \
  --set "settings.clusterEndpoint=${CLUSTER_ENDPOINT}" \
  --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=${KARPENTER_ROLE_ARN}" \
  --set "controller.resources.limits.memory=1Gi"

# 单节点集群：缩为 1 副本避免 anti-affinity 阻塞
kubectl scale deployment karpenter -n karpenter --replicas=1
kubectl rollout status deployment/karpenter -n karpenter --timeout=120s

# 获取 Terraform 创建的 worker node role 名称（格式固定为 <cluster-name>-karpenter-node）
KARPENTER_NODE_ROLE="claude-sbx-karpenter-node"
# 或通过 AWS CLI 查询：
# KARPENTER_NODE_ROLE=$(aws iam list-roles --query 'Roles[?contains(RoleName,`karpenter-node`)].RoleName' --output text)
echo "Node role: $KARPENTER_NODE_ROLE"

# 部署 NodePool + EC2NodeClass（kata 由 EC2NodeClass.userData 在 bootstrap 阶段预装，见 Step 3 说明）
# 实测：用此 EC2NodeClass 起的新 c6g.metal 节点 30-60s 内 Ready，全程零抖动、不触发 ASG 替换。
#
# 注意：用【带引号的 heredoc】(<<'NODEPOOL') 写到文件，避免本地 shell 干扰 userData 里的
#       $VAR / 反引号；role 用占位符后 sed 替换。这是实测可直接复制运行的写法。
cat > /tmp/kata-metal.yaml <<'NODEPOOL'
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: kata-metal
spec:
  amiSelectorTerms:
    - alias: al2023@latest
  role: __KARPENTER_NODE_ROLE__
  subnetSelectorTerms:
    - tags:
        kubernetes.io/role/elb: "1"
  securityGroupSelectorTerms:
    - tags:
        kubernetes.io/cluster/claude-sbx: owned
  blockDeviceMappings:
    - deviceName: /dev/xvda
      ebs:
        volumeSize: 200Gi
        volumeType: gp3
  # ── 方案 A：bootstrap 阶段（kubelet 注册前）预装 Kata，根治 c6g.metal 节点 hang ──
  # Karpenter 把这段 shell 包成 MIME 的第一个 part，nodeadm（启动 kubelet）排在其后，
  # 因此 kata 安装 + containerd 重启发生在 kubelet 注册前，节点对 EKS 始终"一次就绪"。
  userData: |
    #!/bin/bash
    set -euxo pipefail
    KATA_VERSION="3.31.0"; ARCH="arm64"
    cd /tmp
    # 注意：release 包是 .tar.zst（不是 .tar.xz），AL2023 自带 zstd
    curl -fsSL "https://github.com/kata-containers/kata-containers/releases/download/${KATA_VERSION}/kata-static-${KATA_VERSION}-${ARCH}.tar.zst" -o kata.tar.zst
    tar --use-compress-program=unzstd -xf kata.tar.zst -C /   # 包内路径 ./opt/kata/...
    # containerd 2.x（AL2023）用 v2 配置路径 io.containerd.cri.v1.runtime；只注册 kata-qemu
    mkdir -p /opt/kata/containerd/config.d
    cat > /opt/kata/containerd/config.d/kata-deploy.toml <<'TOML'
    [plugins."io.containerd.cri.v1.runtime".containerd.runtimes.kata-qemu]
    runtime_type = "io.containerd.kata-qemu.v2"
    runtime_path = "/opt/kata/bin/containerd-shim-kata-v2"
    privileged_without_host_devices = true
    pod_annotations = ["io.katacontainers.*"]

    [plugins."io.containerd.cri.v1.runtime".containerd.runtimes.kata-qemu.options]
    ConfigPath = "/opt/kata/share/defaults/kata-containers/configuration-qemu.toml"
    TOML
    if ! grep -q "kata-deploy.toml" /etc/containerd/config.toml 2>/dev/null; then
      if grep -q "^imports" /etc/containerd/config.toml 2>/dev/null; then
        sed -i 's#^imports = \[#imports = ["/opt/kata/containerd/config.d/kata-deploy.toml", #' /etc/containerd/config.toml
      else
        sed -i '1i imports = ["/opt/kata/containerd/config.d/kata-deploy.toml"]' /etc/containerd/config.toml
      fi
    fi
    systemctl restart containerd && systemctl enable containerd
---
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: kata-metal
spec:
  template:
    metadata:
      labels:
        sandbox: "true"
        # 必填：kata-qemu RuntimeClass（Step 3）带 nodeSelector katacontainers.io/kata-runtime=true。
        # 方案 A 下 kata 由 UserData 预装、不经 kata-deploy 打 label，必须在此显式声明，
        # 否则 Karpenter 认为 NodePool 不满足 RuntimeClass 的 nodeSelector，拒绝起节点。
        katacontainers.io/kata-runtime: "true"
    spec:
      requirements:
        - {key: node.kubernetes.io/instance-type, operator: In, values: ["c6g.metal"]}
        - {key: kubernetes.io/arch, operator: In, values: ["arm64"]}
        - {key: karpenter.sh/capacity-type, operator: In, values: ["on-demand"]}
      taints:
        - {key: kata-dedicated, value: "true", effect: NoSchedule}
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: kata-metal
  disruption:
    consolidationPolicy: WhenEmpty
    consolidateAfter: 30m
NODEPOOL

sed -i.bak "s#__KARPENTER_NODE_ROLE__#${KARPENTER_NODE_ROLE}#" /tmp/kata-metal.yaml
kubectl apply -f /tmp/kata-metal.yaml

# 触发并验证：创建一个 sandbox（或测试 pod）后，Karpenter 会起一台 c6g.metal，
# 实测从 launch 到节点 Ready 仅 30-60s，无 NotReady 抖动：
# kubectl get nodeclaims -w

kubectl get nodepools     # kata-metal READY=True
kubectl get ec2nodeclasses # kata-metal READY=True

【Step 8: 配置 DNS（生产访问控制面 API）】
NLB_HOST=$(kubectl get svc -n ingress-nginx ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
echo "NLB DNS: $NLB_HOST"
# 在 Route53 或你的 DNS 提供商添加（以上面的 sandbox_domain=sbx.example.com 为例）：
#   api.sbx.example.com  CNAME  $NLB_HOST
# POC 可跳过 DNS，直接用 --resolve 参数测试（见 Step 9）

【Step 9: 验证部署】
# Terraform apply 完成后，等待镜像拉取（ECR 首次拉取需 1-3 分钟）
kubectl rollout status deployment/sandbox-control-plane -n sandbox-system --timeout=300s
kubectl rollout status deployment/litellm -n litellm --timeout=300s

# 常见问题处理：
# LiteLLM OOMKilled → stage2 默认已设 4Gi+1 副本（litellm.tf）。若仍 OOM 再加大：
#   kubectl set resources deployment/litellm -n litellm --limits=cpu=2,memory=4Gi
#   kubectl scale deployment/litellm -n litellm --replicas=1
# Terraform kubernetes provider "Unexpected Identity Change" 错误 → 清理 state 重试：
#   terraform state rm kubernetes_deployment.litellm kubernetes_deployment.control_plane
#   terraform apply ...（重新执行 apply）

kubectl get pods -n sandbox-system    # 控制面 2/2 + node-agent（DaemonSet，调度到 kata-metal 节点）
kubectl get pods -n litellm           # LiteLLM 1/1
kubectl get nodepools                 # kata-metal READY=True

# ── 推荐：本地 port-forward 模式（不依赖 DNS/Ingress，实测 ALL TESTS PASSED）──
bash scripts/e2e_test.sh
# 期望：脚本结尾显示 ALL TESTS PASSED（按 driver 不同，部分用例会 skip）

# ── 生产 Ingress 外部访问（⚠️ 实测开箱不通，见下）──
# ingress-nginx 用的是 in-tree NLB（target=instance + preserve_client_ip），叠加
# Karpenter kata-metal 节点（带 taint、跨 AZ、无 ingress pod）混入 NLB 目标组、cross-zone
# 默认关闭，会导致外部 HTTP empty reply（集群内 ClusterIP 访问正常）。
# 生产要走外部 Ingress，请改装 AWS Load Balancer Controller（target type=ip，直接指向 pod，
# 绕过 NodePort/SNAT），或限定 NLB 只注册系统节点。仅做功能验证用上面的 port-forward 即可。
# （DNS/--resolve 命令保留备查）：
# NLB_IP=$(dig +short $NLB_HOST | head -1)
# bash scripts/e2e_test.sh --api-url "http://api.sbx.example.com" --resolve "api.sbx.example.com:80:${NLB_IP}"

【Step 10: 开始使用 API】
# 直接访问控制面（需 DNS 或 port-forward）
BASE_URL="http://api.sbx.example.com"   # 或 http://localhost:18000

# 创建沙盒
curl -s $BASE_URL/sandboxes \
  -X POST -H "Content-Type: application/json" \
  -d '{"cpu":2,"mem_mib":4096,"tenant_id":"user-1","services":[{"port":8080}]}'

# 等待就绪
curl "$BASE_URL/sandboxes/{id}/wait?state=running"

# 执行命令
curl -s $BASE_URL/sandboxes/{id}/exec \
  -X POST -d '{"cmd":"claude --version"}'

# 挂起（释放内存，快照到 S3）
curl -s -X POST $BASE_URL/sandboxes/{id}/suspend

# 恢复（~1.2s）
curl -s -X POST $BASE_URL/sandboxes/{id}/resume

# 销毁
curl -s -X DELETE $BASE_URL/sandboxes/{id}

【清理（避免费用）】
ACCT=$(aws sts get-caller-identity --query Account --output text)
S3_BUCKET="my-sandbox-snapshots-${ACCT}"
# 先删 stage2（K8s 资源/LiteLLM/Karpenter IAM）
cd terraform/stage2-control-plane && terraform destroy -auto-approve \
  -var="sandbox_image=public.ecr.aws/amazonlinux/amazonlinux:2023" \
  -var="control_plane_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/sandbox-control-plane:latest" \
  -var="node_agent_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/node-agent:latest" \
  -var="snapshot_s3_bucket=${S3_BUCKET}" \
  -var="enable_fargate=false" \
  -var="create_ingress_nginx=false"

# 先删 Karpenter NodePool（让它回收所有 kata-metal 节点），再卸载 helm 安装物 +
# 删 ingress-nginx 创建的 NLB，否则残留节点/NLB 占用子网公网地址，
# 会让下面 phase3 destroy 卡在 VPC/子网删除并报 DependencyViolation：
kubectl delete nodepool kata-metal 2>/dev/null || true     # 触发 Karpenter 回收 .metal 节点
kubectl delete ec2nodeclass kata-metal 2>/dev/null || true
sleep 60  # 等 Karpenter 终止 .metal 实例
helm uninstall karpenter     -n karpenter     2>/dev/null || true
helm uninstall ingress-nginx -n ingress-nginx 2>/dev/null || true
for arn in $(aws elbv2 describe-load-balancers --region us-east-1 \
    --query 'LoadBalancers[?Type==`network`].LoadBalancerArn' --output text); do
  aws elbv2 delete-load-balancer --region us-east-1 --load-balancer-arn "$arn"
done
sleep 30  # 等 NLB 的 ENI 释放

# 再删 EKS 集群（含 .metal 节点组，整体约 15-20 分钟）
MY_IP=$(curl -s https://checkip.amazonaws.com)
cd ../phase3 && terraform destroy -auto-approve \
  -var="endpoint_public_access_cidrs=[\"${MY_IP}/32\"]"
# 最后删 DynamoDB
cd ../stage1-dynamodb && terraform destroy -auto-approve

# 清理 terraform destroy 不会自动删、但会阻塞下次重建的残留资源：
aws logs delete-log-group --log-group-name /aws/eks/claude-sbx/cluster --region us-east-1 2>/dev/null || true
aws ecr delete-repository --repository-name claude-sbx --force --region us-east-1 2>/dev/null || true
```

---

### 后期运维提示词

```
你是这套 AWS 沙盒平台的运维工程师。平台概况：
- EKS 集群 claude-sbx，c6g.metal 节点，Kata 3.31 + kata-qemu runtime
- 控制面：sandbox-system namespace，Deployment 2 副本
  外部访问：http://api.sbx.<domain>（ingress-nginx NLB）
- 状态存储：DynamoDB（claude-sbx-sandboxes / events / tap-idx）
- 凭据隔离：LiteLLM（litellm namespace）持有 Bedrock IRSA，沙盒无凭据
- 快照：S3 bucket，三件套（vm.mem + vm.snapshot + rootfs.ext4）
- Karpenter：kata-metal NodePool，空闲 30 分钟自动整合节点

常见运维操作：
1. 查看所有沙盒：curl http://api.sbx.<domain>/sandboxes?tenant_id=<id>
   或本地：kubectl port-forward -n sandbox-system svc/sandbox-control-plane 18000:80 &
2. 重启控制面：kubectl rollout restart deployment/sandbox-control-plane -n sandbox-system
3. 查看 Karpenter 节点：kubectl get nodeclaims; kubectl get nodes
4. 查看 LiteLLM：kubectl logs -n litellm deployment/litellm --tail=50
5. DynamoDB 直查：aws dynamodb scan --table-name claude-sbx-sandboxes --select COUNT
6. 镜像更新：bash scripts/build_and_push.sh，然后 kubectl rollout restart deployment/sandbox-control-plane -n sandbox-system
7. 节点扩容：修改 NodePool limits，Karpenter 自动调度新节点
8. 成本优化：批量挂起空闲沙盒
   for id in $(curl -s http://api.sbx.<domain>/sandboxes?tenant_id=all | python3 -c "import sys,json; [print(s['id']) for s in json.load(sys.stdin)['sandboxes'] if s['state']=='running']"); do
     curl -s -X POST http://api.sbx.<domain>/sandboxes/$id/suspend
   done

监控关注点：
- node-agent 内存水位：kubectl exec -n sandbox-system daemonset/node-agent -- python3 -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8002/health').read().decode())"
- DynamoDB 写入延迟：AWS Console → DynamoDB → Metrics → SuccessfulRequestLatency
- Karpenter 节点利用率：kubectl top nodes
- LiteLLM 请求量：kubectl logs -n litellm deployment/litellm | grep "INFO:"
```

---

### 本地冒烟测试

```bash
# 无需 AWS，本地直接跑
pip install "moto[dynamodb]" boto3 kubernetes
python3 sandbox-api/smoke_test.py
# 期望：21/21 PASS
```

---

### 实测关键数据

| 指标 | 实测值 | 环境 |
|---|---|---|
| microVM 启动延迟 | ~0.31s | c6g.metal，Firecracker v1.16 |
| 快照 resume 延迟 | **1.2s（跨机）/ 7ms（同机）** | Full 快照，4GB 内存 |
| 空载驻留内存 | ~50 MB/VM | 512 MiB 分配 |
| 单机最大并发 | 60 VM（测试截止，未到上限）| c6g.metal 128 GiB |
| npm install 耗时 | 18s（JuiceFS）/ 4s（本地 ext4）| 7160 文件，8 依赖 |
| LiteLLM → Bedrock | ~1-2s | claude-haiku-4-5 |
| e2e 测试通过率 | **17/17（ALL PASS）** | 集群部署，Kata driver |

---

---

## English Version

### Overview

A production-grade AI Agent sandbox platform built on AWS, replicating Fly.io's Firecracker microVM architecture — with lower cost, full data sovereignty, and native Kubernetes integration.

- **True microVM isolation**: Each sandbox runs in an independent Firecracker/Kata guest kernel — identical behavior to bare metal
- **Pluggable backends**: Same API, switch between Kata-on-EKS (orchestration-first) or bare Firecracker (cost-first)
- **Snapshot-driven cost control**: Idle sandboxes snapshot to S3, resume in ~1.2s
- **Fly Machines-style API**: create/wait/suspend/resume/exec/locate with idempotency, optimistic locking, capability model
- **Zero credentials in sandboxes**: Bedrock credentials live only in LiteLLM Pod's IRSA role

### Use Cases

| Use Case | Description |
|---|---|
| **Claude Code** | fork/exec-heavy, file-watch-intensive, nested processes — microVM guarantees bare-metal fidelity |
| **OpenClaw / Hermes** | Conversational agents needing multi-tenant isolation and autoscaling |
| **OpenAI Codex / Code-gen Agents** | Arbitrary code execution with VM-level security boundary |
| **Long-horizon Agentic Tasks** | Pause/resume workflows, snapshot session state mid-task |
| **SaaS Sandbox Service** | Expose isolated execution to end users, multi-tenant, usage-based billing |
| **CI/CD Sandboxes** | Isolated build/test environments with full OS access |

### Comparison with Alternatives

| Feature | This (AWS Self-Hosted) | E2B | Fly.io Machines | AWS AgentCore |
|---|---|---|---|---|
| **Isolation** | Firecracker/Kata microVM | Firecracker microVM | Firecracker microVM | Container (shared kernel) |
| **Bare-metal fidelity** | ✅ Highest | ✅ High | ✅ High | ❌ Container behavior gaps |
| **Custom images** | ✅ Any ECR image | ✅ | ✅ | ❌ Restricted |
| **Arbitrary ports** | ✅ Wildcard subdomain + NLB | ✅ | ✅ | ❌ |
| **24×7 persistent** | ✅ | ✅ | ✅ | ❌ TTL enforced |
| **Snapshot suspend/resume** | ✅ 1.2s measured | ✅ | ✅ | ❌ |
| **Credential isolation** | ✅ LiteLLM IRSA (verified) | ✅ | ✅ | N/A |
| **Data sovereignty** | ✅ Stays in your AWS account | ❌ 3rd party | ❌ 3rd party | ✅ |
| **K8s ecosystem** | ✅ Native | ❌ | ❌ | ❌ |
| **Est. cost (1000 concurrent)** | **~$170-$450/mo** | ~$800+/mo | ~$600+/mo | Per-call |

> Cost based on c6g.metal with Savings Plan (~-50%) + snapshot idle recovery. Actual depends on active ratio.

### Architecture

```
┌─ EKS cluster ────────────────────────────────────────────────────────┐
│                                                                        │
│  System nodes (Fargate/general)    c6g.metal node group (sandboxes)  │
│  ┌────────────────────────────┐    ┌──────────────────────────────┐  │
│  │ sandbox-control-plane      │    │  node-agent DaemonSet        │  │
│  │ (Deployment, IRSA)         │───►│  Firecracker REST            │  │
│  │  KataDriver (k8s client)   │    │  jailer / tap / snapshots    │  │
│  │  FirecrackerDriver         │    │  S3 upload/download          │  │
│  │  WarmPool                  │    └──────────────────────────────┘  │
│  │  Stateless → DynamoDB      │                                       │
│  └────────────────────────────┘                                       │
│        ↑ ingress-nginx (NLB)                                          │
│        api.sbx.<domain>  ←── production external access              │
│                                                                        │
│  DynamoDB   LiteLLM (Bedrock proxy)   Karpenter (metal autoscaling)  │
└───────────────────────────────────────────────────────────────────────┘
```

### Quick Start (Agent Deployment Guide)

> Copy the following to Claude Code, Cursor, or any code-capable Agent to deploy the platform end-to-end.

```
You are a DevOps engineer deploying an AI Agent sandbox platform on AWS.
Follow these steps exactly, debugging any errors before proceeding.

[Prerequisites]
- AWS CLI configured (IAM permissions: EKS, EC2, IAM, DynamoDB, ECR, S3)
- kubectl, terraform(>=1.5), helm, git installed
- EC2 vCPU service quota for c6g.metal (64 vCPU) — request increase if needed

[Step 0: Clone the repository]
git clone https://github.com/teaguexiao/aws-self-hosted-sandbox.git
cd aws-self-hosted-sandbox
export AWS_REGION=us-east-1

[Step 1: Create DynamoDB state tables]
cd terraform/stage1-dynamodb
terraform init && terraform apply -auto-approve
aws dynamodb list-tables --region us-east-1 | grep claude-sbx

[Step 2: Create EKS cluster + .metal node group]
cd ../phase3
MY_IP=$(curl -s https://checkip.amazonaws.com)
terraform init && terraform apply -auto-approve \
  -var="endpoint_public_access_cidrs=[\"${MY_IP}/32\"]"
# EKS control plane ~10-12 min; with .metal node group cold start, ~15 min total
aws eks update-kubeconfig --name claude-sbx --region us-east-1
kubectl wait node --all --for=condition=Ready --timeout=900s

[Step 3: Create the kata-qemu RuntimeClass (do NOT use the kata-deploy DaemonSet!)]
#
# ⚠️⚠️ Key architecture decision: this project does NOT use the official kata-deploy
#       DaemonSet to install Kata.
#
# Why (a measured, blocking issue): kata-deploy ends its install by running
#   `systemctl restart containerd` on a node that ALREADY runs kubelet + many containers.
#   On c6g.metal + AL2023 this leaves orphaned containerd-shim processes and makes the
#   whole bare-metal node HANG for ~12 minutes before it reboots (containerd restart itself
#   takes 200ms — the slowness is a node-level hang). During that window EC2 reachability
#   checks fail and the managed node group / ASG keeps replacing it as "unhealthy" → a
#   node-replacement loop.
#
# The fix: install Kata at node BOOTSTRAP time (before kubelet registers with EKS), via
#   the Karpenter EC2NodeClass.userData (see Step 7). At bootstrap there are no running
#   containers and EKS can't see the node yet, so the containerd restart is instant and
#   causes zero churn (measured: a fresh .metal node reaches Ready in 30-60s).
#
# So Step 3 only creates the cluster-level RuntimeClass used to schedule sandbox pods:

kubectl apply -f - <<'RUNTIMECLASS'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: kata-qemu
handler: kata-qemu
overhead:
  podFixed:
    cpu: 250m
    memory: 160Mi
scheduling:
  # Only schedule onto the .metal nodes where Karpenter's UserData pre-installed Kata (Step 7)
  nodeSelector:
    katacontainers.io/kata-runtime: "true"
RUNTIMECLASS

kubectl get runtimeclass kata-qemu

[Step 4: Install ingress-nginx (shared NLB)]
# IMPORTANT: specify namespace to avoid conflicts with Terraform later
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx --create-namespace \
  --set controller.service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-type"=nlb \
  --set controller.ingressClassResource.default=true
# Wait for NLB external address (~1-3 min)
kubectl get svc -n ingress-nginx ingress-nginx-controller --watch

[Step 5: Build and push arm64 images]
# Note: the sandbox image repo claude-sbx is auto-created by phase3 (Step 2); only create these two:
ACCT=$(aws sts get-caller-identity --query Account --output text)
aws ecr create-repository --repository-name sandbox-control-plane --region us-east-1 2>/dev/null || true
aws ecr create-repository --repository-name node-agent --region us-east-1 2>/dev/null || true
# Run on arm64 machine (M-series Mac, Graviton EC2, or the .metal node itself)
# See build_and_push.sh for SSM-based remote build on .metal node
bash scripts/build_and_push.sh

[Step 6: Deploy control plane + LiteLLM + Karpenter IAM]
# sandbox_domain is the subdomain root; control plane will be at api.<sandbox_domain>
# Example: sandbox_domain=sbx.example.com → api.sbx.example.com
#
# IMPORTANT: add create_ingress_nginx=false since Step 4 already installed it
cd terraform/stage2-control-plane && terraform init
ACCT=$(aws sts get-caller-identity --query Account --output text)
S3_BUCKET="my-sandbox-snapshots-${ACCT}"
aws s3 mb s3://${S3_BUCKET} --region us-east-1 2>/dev/null || true
terraform apply -auto-approve \
  -var="sandbox_image=public.ecr.aws/amazonlinux/amazonlinux:2023" \
  -var="control_plane_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/sandbox-control-plane:latest" \
  -var="node_agent_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/node-agent:latest" \
  -var="snapshot_s3_bucket=${S3_BUCKET}" \
  -var="enable_fargate=false" \
  -var="create_ingress_nginx=false" \
  -var="sandbox_domain=sbx.example.com"
# Terraform creates: IRSA roles, Karpenter worker node IAM role, K8s resources,
# and EKS Access Entry for karpenter_node role (EC2_LINUX type) —
# this is what allows Karpenter-provisioned nodes to join the cluster via TLS bootstrap
# Karpenter controller itself is installed manually in Step 7 (OCI Helm auth issues)

[Step 7: Install Karpenter manually]
# Remove Docker credential store first (needed for OCI registry access)
python3 -c "
import json, pathlib
cfg = pathlib.Path.home() / '.docker/config.json'
if cfg.exists():
    d = json.loads(cfg.read_text()); d.pop('credsStore', None)
    cfg.write_text(json.dumps(d)); print('credsStore removed')
"
ACCT=$(aws sts get-caller-identity --query Account --output text)
CLUSTER_ENDPOINT=$(aws eks describe-cluster --name claude-sbx --query 'cluster.endpoint' --output text)
KARPENTER_ROLE_ARN="arn:aws:iam::${ACCT}:role/claude-sbx-karpenter"

helm upgrade --install karpenter \
  oci://public.ecr.aws/karpenter/karpenter --version 1.3.3 \
  --namespace karpenter --create-namespace \
  --set "settings.clusterName=claude-sbx" \
  --set "settings.clusterEndpoint=${CLUSTER_ENDPOINT}" \
  --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=${KARPENTER_ROLE_ARN}" \
  --set "controller.resources.limits.memory=1Gi"
kubectl scale deployment karpenter -n karpenter --replicas=1
kubectl rollout status deployment/karpenter -n karpenter --timeout=120s

# Get node role name (fixed naming pattern, or query AWS)
KARPENTER_NODE_ROLE="claude-sbx-karpenter-node"
# Alternative: KARPENTER_NODE_ROLE=$(aws iam list-roles --query 'Roles[?contains(RoleName,`karpenter-node`)].RoleName' --output text)
#
# Use a QUOTED heredoc (<<'NODEPOOL') into a file so the local shell does not mangle the
# $VARs / backticks inside userData; substitute the role via sed afterwards.
# Kata is pre-installed at bootstrap by EC2NodeClass.userData (see Step 3 rationale).
# Measured: a fresh c6g.metal node from this NodePool reaches Ready in 30-60s, zero churn.
cat > /tmp/kata-metal.yaml <<'NODEPOOL'
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: kata-metal
spec:
  amiSelectorTerms:
    - alias: al2023@latest
  role: __KARPENTER_NODE_ROLE__
  subnetSelectorTerms:
    - tags:
        kubernetes.io/role/elb: "1"
  securityGroupSelectorTerms:
    - tags:
        kubernetes.io/cluster/claude-sbx: owned
  blockDeviceMappings:
    - deviceName: /dev/xvda
      ebs: {volumeSize: 200Gi, volumeType: gp3}
  # ── Plan A: pre-install Kata at bootstrap (before kubelet registers) ──
  userData: |
    #!/bin/bash
    set -euxo pipefail
    KATA_VERSION="3.31.0"; ARCH="arm64"
    cd /tmp
    # NOTE: the release artifact is .tar.zst (NOT .tar.xz); AL2023 ships zstd
    curl -fsSL "https://github.com/kata-containers/kata-containers/releases/download/${KATA_VERSION}/kata-static-${KATA_VERSION}-${ARCH}.tar.zst" -o kata.tar.zst
    tar --use-compress-program=unzstd -xf kata.tar.zst -C /   # paths inside: ./opt/kata/...
    # containerd 2.x (AL2023) uses the v2 path io.containerd.cri.v1.runtime; register kata-qemu only
    mkdir -p /opt/kata/containerd/config.d
    cat > /opt/kata/containerd/config.d/kata-deploy.toml <<'TOML'
    [plugins."io.containerd.cri.v1.runtime".containerd.runtimes.kata-qemu]
    runtime_type = "io.containerd.kata-qemu.v2"
    runtime_path = "/opt/kata/bin/containerd-shim-kata-v2"
    privileged_without_host_devices = true
    pod_annotations = ["io.katacontainers.*"]

    [plugins."io.containerd.cri.v1.runtime".containerd.runtimes.kata-qemu.options]
    ConfigPath = "/opt/kata/share/defaults/kata-containers/configuration-qemu.toml"
    TOML
    if ! grep -q "kata-deploy.toml" /etc/containerd/config.toml 2>/dev/null; then
      if grep -q "^imports" /etc/containerd/config.toml 2>/dev/null; then
        sed -i 's#^imports = \[#imports = ["/opt/kata/containerd/config.d/kata-deploy.toml", #' /etc/containerd/config.toml
      else
        sed -i '1i imports = ["/opt/kata/containerd/config.d/kata-deploy.toml"]' /etc/containerd/config.toml
      fi
    fi
    systemctl restart containerd && systemctl enable containerd
---
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: kata-metal
spec:
  template:
    metadata:
      labels:
        sandbox: "true"
        # Required: the kata-qemu RuntimeClass (Step 3) carries nodeSelector
        # katacontainers.io/kata-runtime=true. With Plan A, Kata is pre-installed via UserData
        # (not by kata-deploy), so this label must be declared here or Karpenter will refuse
        # to provision (NodePool "incompatible" with the RuntimeClass nodeSelector).
        katacontainers.io/kata-runtime: "true"
    spec:
      requirements:
        - {key: node.kubernetes.io/instance-type, operator: In, values: ["c6g.metal"]}
        - {key: kubernetes.io/arch, operator: In, values: ["arm64"]}
        - {key: karpenter.sh/capacity-type, operator: In, values: ["on-demand"]}
      taints:
        - {key: kata-dedicated, value: "true", effect: NoSchedule}
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: kata-metal
  disruption:
    consolidationPolicy: WhenEmpty
    consolidateAfter: 30m
NODEPOOL

sed -i.bak "s#__KARPENTER_NODE_ROLE__#${KARPENTER_NODE_ROLE}#" /tmp/kata-metal.yaml
kubectl apply -f /tmp/kata-metal.yaml
kubectl get nodepools && kubectl get ec2nodeclasses

[Step 8: Configure DNS for production API access]
NLB_HOST=$(kubectl get svc -n ingress-nginx ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
echo "Add DNS record: api.sbx.example.com CNAME $NLB_HOST"
# Or skip DNS and use --resolve flag for testing (see Step 9)

[Step 9: Run end-to-end tests]
# Wait for image pull to complete (ECR first pull ~1-3 min)
kubectl rollout status deployment/sandbox-control-plane -n sandbox-system --timeout=300s
kubectl rollout status deployment/litellm -n litellm --timeout=300s

# Note: stage2 now defaults LiteLLM to 4Gi + 1 replica (2Gi OOMKills; see litellm.tf).
#   If it still OOMs: kubectl set resources deployment/litellm -n litellm --limits=cpu=2,memory=4Gi

kubectl get pods -n sandbox-system   # control-plane 2/2 + node-agent (DaemonSet on kata-metal nodes)
kubectl get pods -n litellm           # litellm 1/1

# ── Recommended: local port-forward mode (no DNS/Ingress; measured ALL TESTS PASSED) ──
bash scripts/e2e_test.sh
# Expected: script ends with ALL TESTS PASSED (some tests skip depending on driver)

# ── Production Ingress (⚠️ does NOT work out of the box) ──
# ingress-nginx here uses the in-tree NLB (target=instance + preserve_client_ip). Combined
# with Karpenter kata-metal nodes (tainted, cross-AZ, no ingress pod) joining the NLB target
# group and cross-zone disabled by default, external HTTP returns empty replies (in-cluster
# ClusterIP access works fine). For real external Ingress, install the AWS Load Balancer
# Controller (target type=ip, pointing straight at pods), or restrict the NLB to system nodes.
# (--resolve command kept for reference):
# NLB_IP=$(dig +short $NLB_HOST | head -1)
# bash scripts/e2e_test.sh --api-url "http://api.sbx.example.com" --resolve "api.sbx.example.com:80:${NLB_IP}"

[Step 10: Use the API]
BASE_URL="http://api.sbx.example.com"   # or http://localhost:18000

# Create sandbox (idempotent)
curl -s $BASE_URL/sandboxes -X POST \
  -H "Content-Type: application/json" \
  -d '{"cpu":2,"mem_mib":4096,"tenant_id":"user-1","idempotency_key":"req-001"}'

# Wait for ready
curl "$BASE_URL/sandboxes/{id}/wait?state=running&timeout=30"

# Execute command
curl -s $BASE_URL/sandboxes/{id}/exec -X POST -d '{"cmd":"echo hello"}'

# Suspend (snapshot + free memory)
curl -s -X POST $BASE_URL/sandboxes/{id}/suspend

# Resume (~1.2s)
curl -s -X POST $BASE_URL/sandboxes/{id}/resume

# Destroy
curl -s -X DELETE $BASE_URL/sandboxes/{id}

[Cleanup]
ACCT=$(aws sts get-caller-identity --query Account --output text)
S3_BUCKET="my-sandbox-snapshots-${ACCT}"
cd terraform/stage2-control-plane && terraform destroy -auto-approve \
  -var="sandbox_image=public.ecr.aws/amazonlinux/amazonlinux:2023" \
  -var="control_plane_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/sandbox-control-plane:latest" \
  -var="node_agent_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/node-agent:latest" \
  -var="snapshot_s3_bucket=${S3_BUCKET}" \
  -var="enable_fargate=false" \
  -var="create_ingress_nginx=false"

# Delete the Karpenter NodePool first (so it reclaims all kata-metal nodes), then uninstall
# helm releases + delete the NLB created by ingress-nginx — otherwise leftover nodes/NLB
# hold public addresses on the subnets and phase3 destroy stalls with DependencyViolation:
kubectl delete nodepool kata-metal 2>/dev/null || true     # triggers Karpenter to reclaim .metal nodes
kubectl delete ec2nodeclass kata-metal 2>/dev/null || true
sleep 60  # wait for Karpenter to terminate the .metal instances
helm uninstall karpenter     -n karpenter     2>/dev/null || true
helm uninstall ingress-nginx -n ingress-nginx 2>/dev/null || true
for arn in $(aws elbv2 describe-load-balancers --region us-east-1 \
    --query 'LoadBalancers[?Type==`network`].LoadBalancerArn' --output text); do
  aws elbv2 delete-load-balancer --region us-east-1 --load-balancer-arn "$arn"
done
sleep 30  # wait for NLB ENIs to release

MY_IP=$(curl -s https://checkip.amazonaws.com)
cd ../phase3 && terraform destroy -auto-approve \
  -var="endpoint_public_access_cidrs=[\"${MY_IP}/32\"]"
cd ../stage1-dynamodb && terraform destroy -auto-approve

# Clean up leftovers that destroy won't remove but that block a future re-create:
aws logs delete-log-group --log-group-name /aws/eks/claude-sbx/cluster --region us-east-1 2>/dev/null || true
aws ecr delete-repository --repository-name claude-sbx --force --region us-east-1 2>/dev/null || true
```

### Key Benchmark Numbers

| Metric | Measured | Environment |
|---|---|---|
| microVM cold start | ~0.31s | c6g.metal, Firecracker v1.16 |
| Snapshot resume | **1.2s (cross-host) / 7ms (same host)** | Full snapshot, 4GB memory |
| Idle memory footprint | ~50 MB/VM | 512 MiB allocated |
| Max concurrent VMs (tested) | 60 (not the ceiling) | c6g.metal 128 GiB |
| npm install time | 18s (JuiceFS) / 4s (local ext4) | 7160 files, 8 deps |
| LiteLLM → Bedrock latency | ~1-2s | claude-haiku-4-5 |
| e2e test pass rate | **17/17 (ALL PASS)** | Cluster-deployed, Kata driver |

### Local Smoke Test (No AWS Required)

```bash
pip install "moto[dynamodb]" boto3 kubernetes
python3 sandbox-api/smoke_test.py
# Expected: 21/21 PASS
```

---

*This project is a production-grade reference implementation. Use it as a foundation for building your own agent sandbox platform on AWS.*
