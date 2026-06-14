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
| 24×7 常驻 | 1.5 GiB 有效 | ~$22/沙盒·月（c7g.metal 按需） |
| 快照空闲回收 | ~50 MB（空载驻留） | **~$4/沙盒·月** |
| 暖池（Savings Plan） | — | 再降 40-60% |

- 单台 `c7g.metal`（128 GiB）：空载可承载 **400+ 沙盒**，活跃工作集约 **75 沙盒**
- **resume 延迟 1.2s 实测**，用户无感知
- **~10000 并发估算：134 台 c7g.metal，Savings Plan 后约 $1,700-$4,500/月**

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

> 月费基于 c7g.metal Savings Plan（~-50%）+ 快照空闲回收估算，未含流量/存储费用。

---

### 架构概览

```
┌─ EKS cluster ─────────────────────────────────────────────────────┐
│                                                                      │
│  普通/Fargate 节点（系统）          c6g.metal 节点组（沙盒）          │
│  ┌──────────────────────────┐      ┌───────────────────────────┐   │
│  │ sandbox-control-plane    │ HTTP │  node-agent DaemonSet     │   │
│  │ (Deployment, IRSA)       │─────►│  Firecracker REST         │   │
│  │  KataDriver (k8s client) │      │  jailer / tap / snapshot  │   │
│  │  FirecrackerDriver       │      │  S3 upload/download       │   │
│  │  WarmPool                │      └───────────────────────────┘   │
│  │  无状态 → DynamoDB        │                                       │
│  └──────────────────────────┘                                       │
│         ↑ ingress-nginx (NLB)                                       │
│         api.sbx.<domain>  ←── 生产外部访问入口                      │
│                                                                      │
│  DynamoDB  LiteLLM(Bedrock代理)  Karpenter(.metal自动扩缩)           │
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
# .metal 节点冷启动约 10 分钟，等待 Ready
aws eks update-kubeconfig --name claude-sbx --region us-east-1
kubectl wait node --all --for=condition=Ready --timeout=900s

【Step 3: 安装 Kata Containers 3.31】
cd /tmp
curl -sL https://github.com/kata-containers/kata-containers/archive/refs/tags/3.31.0.tar.gz -o kata.tar.gz
tar -xzf kata.tar.gz kata-containers-3.31.0/tools/packaging/kata-deploy/helm-chart/
helm dependency build kata-containers-3.31.0/tools/packaging/kata-deploy/helm-chart/kata-deploy/
helm install kata-deploy \
  kata-containers-3.31.0/tools/packaging/kata-deploy/helm-chart/kata-deploy \
  --namespace kube-system
kubectl rollout status daemonset/kata-deploy -n kube-system --timeout=300s
kubectl get runtimeclass | grep kata-qemu   # 应能看到 kata-qemu

【Step 4: 安装 ingress-nginx（共享 NLB）】
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm install ingress-nginx ingress-nginx/ingress-nginx \
  --set controller.service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-type"=nlb \
  --set controller.ingressClassResource.default=true
# 等待 NLB 分配地址
kubectl get svc ingress-nginx-controller --watch

【Step 5: 创建 ECR 仓库并构建 arm64 镜像】
# 方式 A：在 .metal 节点上原生构建（推荐，无需 buildx）
ACCT=$(aws sts get-caller-identity --query Account --output text)
aws ecr create-repository --repository-name sandbox-control-plane --region us-east-1 2>/dev/null || true
aws ecr create-repository --repository-name node-agent --region us-east-1 2>/dev/null || true
bash scripts/build_and_push.sh   # 通过 SSM 在 .metal 节点上构建，自动推送

# 方式 B：本地 arm64 机器（M 系列 Mac 或 Graviton EC2）
# docker buildx build --platform linux/arm64 -t $ACCT.dkr.ecr.us-east-1.amazonaws.com/sandbox-control-plane:latest sandbox-api/
# docker buildx build --platform linux/arm64 -t $ACCT.dkr.ecr.us-east-1.amazonaws.com/node-agent:latest node-agent/

【Step 6: 部署控制面 + LiteLLM + Karpenter IAM】
cd terraform/stage2-control-plane
terraform init
ACCT=$(aws sts get-caller-identity --query Account --output text)
S3_BUCKET="my-sandbox-snapshots-${ACCT}"   # 替换为你的 S3 桶
aws s3 mb s3://${S3_BUCKET} --region us-east-1 2>/dev/null || true

terraform apply -auto-approve \
  -var="sandbox_image=public.ecr.aws/amazonlinux/amazonlinux:2023" \
  -var="control_plane_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/sandbox-control-plane:latest" \
  -var="node_agent_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/node-agent:latest" \
  -var="snapshot_s3_bucket=${S3_BUCKET}" \
  -var="enable_fargate=false" \
  -var="sandbox_domain=sbx.example.com"   # 替换为你的域名

【Step 7: 安装 Karpenter（手动，避免 Helm OCI 兼容问题）】
KARPENTER_ROLE_ARN="arn:aws:iam::${ACCT}:role/claude-sbx-karpenter"
CLUSTER_ENDPOINT=$(aws eks describe-cluster --name claude-sbx --query 'cluster.endpoint' --output text)
# 移除 Docker credsStore（Helm OCI 需要）
python3 -c "import json,pathlib; cfg=pathlib.Path.home()/'.docker/config.json'; d=json.loads(cfg.read_text()); d.pop('credsStore',None); cfg.write_text(json.dumps(d))"
helm upgrade --install karpenter \
  oci://public.ecr.aws/karpenter/karpenter --version 1.3.3 \
  --namespace karpenter --create-namespace \
  --set "settings.clusterName=claude-sbx" \
  --set "settings.clusterEndpoint=${CLUSTER_ENDPOINT}" \
  --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=${KARPENTER_ROLE_ARN}" \
  --set "controller.resources.limits.memory=1Gi"
# 单节点集群只需 1 副本
kubectl scale deployment karpenter -n karpenter --replicas=1
# 部署 NodePool
kubectl apply -f - <<'NODEPOOL'
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: kata-metal
spec:
  amiSelectorTerms:
    - alias: al2023@latest
  role: metal_arm64-eks-node-group-<suffix>   # 替换为实际节点角色名
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

【Step 8: 配置 DNS（生产访问控制面 API）】
NLB_HOST=$(kubectl get svc ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
echo "NLB DNS: $NLB_HOST"
# 在 Route53 或你的 DNS 提供商添加：
#   api.sbx.example.com  CNAME  $NLB_HOST
# POC 可跳过 DNS，直接用 --resolve 参数测试（见 Step 9）

【Step 9: 验证部署】
kubectl get pods -n sandbox-system    # 控制面 2/2 + node-agent 1/1
kubectl get pods -n litellm           # LiteLLM 2/2
kubectl get nodepools                 # kata-metal READY=True

# 生产 Ingress 模式测试（DNS 已配好）：
bash scripts/e2e_test.sh --api-url "http://api.sbx.example.com"

# 生产 Ingress 模式测试（DNS 未配，用 --resolve 绕过）：
NLB_IP=$(dig +short $NLB_HOST | head -1)
bash scripts/e2e_test.sh \
  --api-url "http://api.sbx.example.com" \
  --resolve "api.sbx.example.com:80:${NLB_IP}"

# 本地开发模式（自动 port-forward）：
bash scripts/e2e_test.sh
# 期望：17/17 ALL PASS

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
cd terraform/stage2-control-plane && terraform destroy -auto-approve ...
cd ../phase3 && terraform destroy -auto-approve -var="endpoint_public_access_cidrs=[\"0.0.0.0/0\"]"
cd ../stage1-dynamodb && terraform destroy -auto-approve
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
| microVM 启动延迟 | ~0.31s | c7g.metal，Firecracker v1.16 |
| 快照 resume 延迟 | **1.2s（跨机）/ 7ms（同机）** | Full 快照，4GB 内存 |
| 空载驻留内存 | ~50 MB/VM | 512 MiB 分配 |
| 单机最大并发 | 60 VM（测试截止，未到上限）| c7g.metal 128 GiB |
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

> Cost based on c7g.metal with Savings Plan (~-50%) + snapshot idle recovery. Actual depends on active ratio.

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
# .metal cold start takes ~10 minutes
aws eks update-kubeconfig --name claude-sbx --region us-east-1
kubectl wait node --all --for=condition=Ready --timeout=900s

[Step 3: Install Kata Containers 3.31]
cd /tmp
curl -sL https://github.com/kata-containers/kata-containers/archive/refs/tags/3.31.0.tar.gz -o kata.tar.gz
tar -xzf kata.tar.gz kata-containers-3.31.0/tools/packaging/kata-deploy/helm-chart/
helm dependency build kata-containers-3.31.0/tools/packaging/kata-deploy/helm-chart/kata-deploy/
helm install kata-deploy \
  kata-containers-3.31.0/tools/packaging/kata-deploy/helm-chart/kata-deploy \
  --namespace kube-system
kubectl rollout status daemonset/kata-deploy -n kube-system --timeout=300s
kubectl get runtimeclass | grep kata-qemu

[Step 4: Install ingress-nginx (shared NLB)]
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm install ingress-nginx ingress-nginx/ingress-nginx \
  --set controller.service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-type"=nlb \
  --set controller.ingressClassResource.default=true
kubectl get svc ingress-nginx-controller --watch   # wait for EXTERNAL-IP

[Step 5: Build and push arm64 images]
ACCT=$(aws sts get-caller-identity --query Account --output text)
aws ecr create-repository --repository-name sandbox-control-plane --region us-east-1 2>/dev/null || true
aws ecr create-repository --repository-name node-agent --region us-east-1 2>/dev/null || true
bash scripts/build_and_push.sh   # builds natively on .metal node via SSM

[Step 6: Deploy control plane + LiteLLM + Karpenter IAM]
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
  -var="sandbox_domain=sbx.example.com"

[Step 7: Install Karpenter 1.3.3]
KARPENTER_ROLE_ARN="arn:aws:iam::${ACCT}:role/claude-sbx-karpenter"
CLUSTER_ENDPOINT=$(aws eks describe-cluster --name claude-sbx --query 'cluster.endpoint' --output text)
python3 -c "import json,pathlib; cfg=pathlib.Path.home()/'.docker/config.json'; d=json.loads(cfg.read_text()); d.pop('credsStore',None); cfg.write_text(json.dumps(d))"
helm upgrade --install karpenter \
  oci://public.ecr.aws/karpenter/karpenter --version 1.3.3 \
  --namespace karpenter --create-namespace \
  --set "settings.clusterName=claude-sbx" \
  --set "settings.clusterEndpoint=${CLUSTER_ENDPOINT}" \
  --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=${KARPENTER_ROLE_ARN}" \
  --set "controller.resources.limits.memory=1Gi"
kubectl scale deployment karpenter -n karpenter --replicas=1

[Step 8: Configure DNS for production API access]
NLB_HOST=$(kubectl get svc ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
echo "Add DNS record: api.sbx.example.com CNAME $NLB_HOST"
# Or test without DNS using --resolve

[Step 9: Run end-to-end tests]
kubectl get pods -n sandbox-system   # control-plane 2/2 + node-agent 1/1
kubectl get pods -n litellm           # litellm 2/2

# Production Ingress mode (DNS configured):
bash scripts/e2e_test.sh --api-url "http://api.sbx.example.com"

# Production Ingress mode (no DNS, use --resolve):
NLB_IP=$(dig +short $NLB_HOST | head -1)
bash scripts/e2e_test.sh \
  --api-url "http://api.sbx.example.com" \
  --resolve "api.sbx.example.com:80:${NLB_IP}"

# Local dev mode (auto port-forward):
bash scripts/e2e_test.sh
# Expected: 17/17 ALL PASS

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
cd terraform/stage2-control-plane && terraform destroy -auto-approve ...
cd ../phase3 && terraform destroy -auto-approve -var="endpoint_public_access_cidrs=[\"0.0.0.0/0\"]"
cd ../stage1-dynamodb && terraform destroy -auto-approve
```

### Key Benchmark Numbers

| Metric | Measured | Environment |
|---|---|---|
| microVM cold start | ~0.31s | c7g.metal, Firecracker v1.16 |
| Snapshot resume | **1.2s (cross-host) / 7ms (same host)** | Full snapshot, 4GB memory |
| Idle memory footprint | ~50 MB/VM | 512 MiB allocated |
| Max concurrent VMs (tested) | 60 (not the ceiling) | c7g.metal 128 GiB |
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
