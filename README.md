# AWS 自建 Claude Code 沙盒平台

> 在 AWS 上复刻 Fly.io Firecracker microVM 架构，以更低成本、更高可控性运行 Claude Code 及各类 AI Agent。

---

## 项目简介

本项目提供一套完整的、可生产化的 AI Agent 沙盒平台，核心特性：

- **真实 microVM 隔离**：每个沙盒运行在独立的 Firecracker/Kata guest 内核中，与裸机行为完全一致
- **后端可插拔**：同一套 API，底层可切换 Kata-on-EKS（编排优先）或裸 Firecracker（成本优先）
- **快照驱动成本控制**：空闲沙盒快照挂起释放内存，访问时 ~7ms 恢复，实测 `resume_time=1.2s`
- **Fly Machines 风格 API**：create/wait/suspend/resume/exec/locate，幂等键、乐观锁、capability 模型
- **凭据零进沙盒**：Bedrock 凭据仅在 LiteLLM Pod 的 IRSA 角色，沙盒永远看不到真实 key

---

## 适用场景

| 场景 | 说明 |
|---|---|
| **Claude Code** | fork/exec 密集、文件监听重、嵌套进程 — microVM 保障与裸机一致的行为 |
| **OpenClaw / Hermes** | 会话式智能助理，需多租户隔离、按需扩缩 |
| **OpenAI Codex / 代码生成 Agent** | 任意代码执行，VM 级安全边界，防逃逸 |
| **长程 Agentic 任务** | 任务暂停恢复、工作流中断续跑、快照持久化 session 状态 |
| **SaaS 沙盒服务** | 向终端用户暴露隔离执行环境，多租户、按量计费 |
| **CI/CD 沙盒** | 隔离的构建/测试环境，npm install / docker build / 任意端口服务 |

---

## 核心优势

### 1. 裸机保真度（microVM 不是容器）

```
guest kernel: 6.18.28   ≠   node kernel: 6.1.172   ✅ 真独立内核
nproc: 3 (guest 配额)   ≠   宿主: 64              ✅ CPU 视图隔离
inotify 配额: 独立                                  ✅ 密集容器不会耗尽
root 可绑 80 端口、dnf 装包、嵌套 docker            ✅ 完整 root 无 seccomp 裁剪
```

### 2. 成本控制：快照 = 成本杠杆

| 模式 | 每沙盒内存占用 | 月均成本估算 |
|---|---|---|
| 24×7 常驻 | 1.5 GiB 有效 | ~$22/沙盒·月（c7g.metal 按需） |
| 快照空闲回收 | ~50 MB（空载驻留） | **~$4/沙盒·月**（实际活跃时间决定） |
| 暖池（Savings Plan） | — | 再降 40-60% |

- 单台 `c7g.metal`（128 GiB）：空载可承载 **400+ 沙盒**，活跃工作集可承载 **~75 沙盒**
- **resume 延迟 1.2s 实测**，用户无感知

### 3. 运维轻便性

- **Terraform 全套**：DynamoDB + EKS + Karpenter + LiteLLM + IRSA 一键部署
- **Karpenter 自动扩缩**：.metal 节点空闲 30 分钟自动整合，无手动干预
- **控制面无状态**：所有状态在 DynamoDB，Pod 崩了重启不丢数据
- **node-agent DaemonSet**：每节点自动调度，Firecracker/jailer/tap 管理全自动化

### 4. 安全性

- **VM 级隔离**：每沙盒独立 guest 内核，无共享宿主内核的 seccomp/cgroup 泄漏
- **凭据零进沙盒**：Bedrock 凭据只在 LiteLLM IRSA，沙盒看不到真实 key（R8 落地）
- **租户互不可信**：NetworkPolicy（规划中）+ microVM 双重隔离
- **控制面 Bearer token 认证**：`API_KEYS` env 配置，多 key 支持多租户

### 5. API 开发者友好性

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

---

## 与主流方案对比

| 维度 | 本方案（AWS 自建） | E2B | Fly.io Machines | AWS AgentCore |
|---|---|---|---|---|
| **隔离层** | Firecracker/Kata microVM（真 guest 内核） | Firecracker microVM | Firecracker microVM | 容器（共享内核） |
| **裸机保真度** | ✅ 最高（与 Fly 同构） | ✅ 高 | ✅ 高 | ❌ 容器行为偏差 |
| **自定义镜像** | ✅ 任意 ECR 镜像 | ✅ | ✅ | ❌ 受限 |
| **任意端口** | ✅ 通配符子域名 + 共享 NLB | ✅ | ✅ | ❌ |
| **24×7 长驻** | ✅ | ✅ | ✅ | ❌ 有 TTL |
| **快照 suspend/resume** | ✅ 实测 1.2s | ✅ | ✅ | ❌ |
| **凭据隔离** | ✅ LiteLLM IRSA（落地） | ✅ | ✅ | N/A |
| **数据主权** | ✅ 数据留 AWS 账号内 | ❌ 第三方 | ❌ 第三方 | ✅ |
| **成本可控性** | ✅ 自管节点 + RI/SP | 按用量付费 | 按用量付费 | 按用量付费 |
| **Kata-on-EKS 编排** | ✅ | ❌ | ❌ | ❌ |
| **K8s 生态集成** | ✅ 原生 | ❌ | ❌ | ❌ |
| **运维复杂度** | 中（Terraform 封装） | 低（托管服务） | 低（托管服务） | 低（托管服务） |
| **月费估算（1000 并发）** | ~$1,700-$4,500（Savings Plan 后） | ~$8,000+ | ~$6,000+ | 按调用计费 |

> 月费基于 `c7g.metal`（$2.32/hr 按需，Savings Plan 约 -50%）+ 快照空闲回收估算，实际取决于活跃比例。

---

## 架构概览

```
┌─ EKS cluster ─────────────────────────────────────────────────────┐
│                                                                      │
│  Fargate / 普通节点（系统）          c6g.metal 节点组（沙盒）          │
│  ┌──────────────────────┐           ┌────────────────────────────┐  │
│  │ sandbox-control-plane│  HTTP     │  node-agent DaemonSet      │  │
│  │ (2 replica, IRSA)    │──────────►│  - Firecracker REST        │  │
│  │  KataDriver          │           │  - jailer/tap/snapshot     │  │
│  │  FirecrackerDriver   │           │  - S3 快照 upload/download │  │
│  │  WarmPool            │           └────────────────────────────┘  │
│  │  无状态 → DynamoDB   │                                            │
│  └──────────────────────┘                                            │
│                                                                      │
│  DynamoDB（状态/lease/幂等/暖池）                                    │
│  LiteLLM（Bedrock 代理，凭据隔离）                                   │
│  ingress-nginx（共享 NLB，通配符子域名路由）                          │
│  Karpenter（.metal 节点自动扩缩）                                    │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 目录结构

```
sandbox-api/         控制面 API（FastAPI 风格，纯标准库）
  app.py             HTTP 服务入口
  driver.py          SandboxDriver Protocol（抽象接口）
  db.py              DynamoDB 封装（乐观锁/幂等/暖池）
  warm_pool.py       暖池（FC 预快照 / Kata SandboxWarmPool）
  drivers/
    firecracker.py   FC Driver → node-agent
    kata.py          Kata Driver → K8s Python client
  smoke_test.py      本地冒烟测试（moto mock，21/21）

node-agent/          on-host 执行手（跑在 .metal 节点）
  main.py            tap/jailer/FC REST/diff 快照/S3/vsock exec

terraform/
  phase1/            单 .metal 主机 + ECR（H1 验证）
  phase3/            EKS 集群 + Graviton .metal 节点组
  stage1-dynamodb/   DynamoDB 3 张表
  stage2-control-plane/  IRSA + K8s 资源 + LiteLLM + Karpenter

scripts/
  build_and_push.sh  构建并推送 ECR 镜像
  e2e_test.sh        端到端测试（17 项）
  setup-host.sh      .metal 主机 Firecracker 初始化

k8s/                 手动测试用 YAML（sandbox Pod/Service/Ingress）
```

---

## 快速开始（Agent 部署指南）

> 将以下内容复制给 Claude Code / Cursor / 任意支持代码执行的 Agent，即可引导完整部署。

```
你是一名 DevOps 工程师，需要在 AWS 上部署一套 AI Agent 沙盒平台。
请严格按照以下步骤执行，遇到错误先排查再继续。

【前提条件】
- AWS CLI 已配置（有权限创建 EKS/EC2/IAM/DynamoDB/ECR）
- kubectl, terraform(>=1.5), helm 已安装
- 工作目录：/Users/<you>/Documents/aws-claude-sandbox-poc

【Step 1: 创建 DynamoDB 状态表】
cd terraform/stage1-dynamodb
terraform init && terraform apply -auto-approve
# 验证：aws dynamodb list-tables | grep claude-sbx

【Step 2: 创建 EKS 集群 + .metal 节点组】
cd ../phase3
MY_IP=$(curl -s https://checkip.amazonaws.com)
terraform init && terraform apply -auto-approve \
  -var="endpoint_public_access_cidrs=[\"${MY_IP}/32\"]"
aws eks update-kubeconfig --name claude-sbx --region us-east-1
kubectl get nodes   # 等待 Ready（.metal 节点约 10 分钟）

【Step 3: 安装 Kata Containers】
# 下载 kata 3.31 helm chart
cd /tmp && curl -sL https://github.com/kata-containers/kata-containers/archive/refs/tags/3.31.0.tar.gz -o kata.tar.gz
tar -xzf kata.tar.gz kata-containers-3.31.0/tools/packaging/kata-deploy/helm-chart/
helm dependency build kata-containers-3.31.0/tools/packaging/kata-deploy/helm-chart/kata-deploy/
helm install kata-deploy kata-containers-3.31.0/tools/packaging/kata-deploy/helm-chart/kata-deploy --namespace kube-system
kubectl rollout status daemonset/kata-deploy -n kube-system --timeout=300s
kubectl get runtimeclass | grep kata-qemu   # 应能看到 kata-qemu

【Step 4: 安装 ingress-nginx】
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm install ingress-nginx ingress-nginx/ingress-nginx \
  --set controller.service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-type"=nlb \
  --set controller.ingressClassResource.default=true

【Step 5: 创建 ECR 仓库并构建镜像】
aws ecr create-repository --repository-name sandbox-control-plane --region us-east-1
aws ecr create-repository --repository-name node-agent --region us-east-1
# 在 .metal 节点上构建（或本地 arm64 机器）
bash scripts/build_and_push.sh

【Step 6: 部署控制面 + LiteLLM + Karpenter IAM】
cd terraform/stage2-control-plane
terraform init
ACCT=$(aws sts get-caller-identity --query Account --output text)
terraform apply -auto-approve \
  -var="sandbox_image=public.ecr.aws/amazonlinux/amazonlinux:2023" \
  -var="control_plane_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/sandbox-control-plane:latest" \
  -var="node_agent_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/node-agent:latest" \
  -var="snapshot_s3_bucket=<your-s3-bucket>" \
  -var="enable_fargate=false"

# 安装 Karpenter（手动，避免 Helm provider 兼容性问题）
KARPENTER_ROLE_ARN="arn:aws:iam::${ACCT}:role/claude-sbx-karpenter"
CLUSTER_ENDPOINT=$(aws eks describe-cluster --name claude-sbx --query 'cluster.endpoint' --output text)
helm upgrade --install karpenter \
  oci://public.ecr.aws/karpenter/karpenter --version 1.3.3 \
  --namespace karpenter --create-namespace \
  --set "settings.clusterName=claude-sbx" \
  --set "settings.clusterEndpoint=${CLUSTER_ENDPOINT}" \
  --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=${KARPENTER_ROLE_ARN}" \
  --set "controller.resources.limits.memory=1Gi"

【Step 7: 验证部署】
kubectl get pods -n sandbox-system   # 控制面 2/2 + node-agent 1/1
kubectl get pods -n litellm           # LiteLLM 2/2
bash scripts/e2e_test.sh             # 全部 PASS

【Step 8: 使用 API】
# port-forward 访问控制面
kubectl port-forward -n sandbox-system svc/sandbox-control-plane 18000:80 &

# 创建沙盒
curl -s http://localhost:18000/sandboxes \
  -X POST -H "Content-Type: application/json" \
  -d '{"cpu":2,"mem_mib":4096,"tenant_id":"user-1","services":[{"port":8080}]}'

# 等待就绪
curl "http://localhost:18000/sandboxes/{id}/wait?state=running"

# 执行命令
curl -s http://localhost:18000/sandboxes/{id}/exec \
  -X POST -d '{"cmd":"claude --version"}'

# 挂起（释放内存）
curl -s -X POST http://localhost:18000/sandboxes/{id}/suspend

# 恢复（~1.2s）
curl -s -X POST http://localhost:18000/sandboxes/{id}/resume

【清理（避免费用）】
cd terraform/stage2-control-plane && terraform destroy -auto-approve ...
cd ../phase3 && terraform destroy -auto-approve ...
cd ../stage1-dynamodb && terraform destroy -auto-approve
```

---

## 后期运维提示词

```
你是这套 AWS 沙盒平台的运维工程师。平台概况：
- EKS 集群 claude-sbx，c6g.metal 节点组，Kata 3.31 + kata-qemu runtime
- 控制面：sandbox-system namespace，2 副本 Deployment
- 状态存储：DynamoDB（claude-sbx-sandboxes / events / tap-idx）
- 凭据隔离：LiteLLM（litellm namespace）持有 Bedrock IRSA，沙盒无凭据
- 快照：S3 bucket，三件套（vm.mem + vm.snapshot + rootfs.ext4）

常见运维操作：
1. 查看沙盒状态：kubectl port-forward -n sandbox-system svc/sandbox-control-plane 18000:80 && curl http://localhost:18000/sandboxes
2. 查看 Karpenter 节点：kubectl get nodeclaims，kubectl get nodes
3. 查看 LiteLLM：kubectl logs -n litellm deployment/litellm
4. DynamoDB 直查：aws dynamodb scan --table-name claude-sbx-sandboxes --select COUNT
5. 镜像更新：bash scripts/build_and_push.sh，然后 kubectl rollout restart deployment/sandbox-control-plane -n sandbox-system
6. 扩容：修改 NodePool limits，Karpenter 自动调度新 .metal 节点
7. 成本优化：查看空闲沙盒 → POST /sandboxes/{id}/suspend，或调小 WARM_POOL_SIZE

监控关注点：
- node-agent free_mem_mib（GET http://<node-ip>:8002/health）
- DynamoDB 写入延迟（CloudWatch）
- Karpenter 节点利用率（kubectl top nodes）
```

---

## 本地冒烟测试

```bash
pip install "moto[dynamodb]" boto3 kubernetes
python3 sandbox-api/smoke_test.py
# 期望：21/21 PASS（含 DB/FC Driver/Kata Capability/API E2E/Auth 测试）
```

---

## 实测关键数据

| 指标 | 实测值 | 环境 |
|---|---|---|
| microVM 启动延迟 | ~0.31s | c7g.metal，Firecracker v1.16 |
| 快照 resume 延迟 | **1.2s（跨机）/ 7ms（同机）** | Full 快照，4GB 内存 |
| 空载驻留内存 | ~50 MB/VM | 512 MiB 分配 |
| 单机最大并发 | 60 VM（测试截止，未到上限） | c7g.metal 128 GiB |
| npm install 耗时 | 18s（JuiceFS）/ 4s（本地 ext4） | 7160 文件，8 依赖 |
| LiteLLM → Bedrock | ~1-2s | claude-haiku-4-5 |
| e2e 测试通过率 | **17/17（ALL PASS）** | 集群部署，Kata driver |

---

## 版权与许可

本项目为 POC 级别实现，适用于企业内部评估和自建沙盒平台参考。
