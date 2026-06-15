# AWS Self-Hosted AI Agent Sandbox Platform

> Build your own Fly.io-style Firecracker microVM sandbox on AWS — lower cost, full control, data stays in your account.

**中文** · [English](README.en.md)

---

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

**最小配置月费（us-east-1 按需价，实际运行 1 台 c6g.metal）：**

| 资源 | 单价 | 月费（730h） |
|---|---|---|
| c6g.metal（64vCPU/128GiB）| $2.304/hr | ~$1,682 |
| EKS 控制面 | $0.10/hr | ~$73 |
| DynamoDB（PAY_PER_REQUEST）| 按写入量 | <$1 |
| S3 快照（方案B，~2GB/沙盒）| $0.023/GB | ~$2–10 |
| **合计（按需）** | | **~$1,756/月** |
| **合计（Savings Plan ~42% off）**| | **~$1,018/月** |

> 价格为 us-east-1 按需估算，仅供参考。生产环境建议购买 1 年期 Savings Plan 可降低约 42%。
> 实际价格请以 [AWS Pricing Calculator](https://calculator.aws) 为准。

**承载能力与摊算成本（单台 c6g.metal，128 GiB）：**

| 运行模式 | 每沙盒内存 | 可承载沙盒数 | 摊算成本（按需） |
|---|---|---|---|
| 24×7 活跃工作集 | 1.5 GiB | ~75 个 | **~$23/沙盒·月** |
| **快照空闲回收** | ~50 MB（空载驻留）| **400+ 个** | **~$4/沙盒·月** |
| Savings Plan + 快照回收 | — | 同上 | **~$2–3/沙盒·月** |

- **resume 延迟 1.2s 实测**，用户无感知，快照挂起对用户透明
- 单台机器即可支撑小规模 SaaS，多台横向扩展线性增长（节点间无共享状态）

> **超卖（vCPU/内存 Overcommit）可进一步摊薄成本：** Firecracker microVM 支持 vCPU 超售——空闲沙盒几乎不消耗 CPU，活跃沙盒又是突发型负载。实测空载每 VM 实际驻留仅 ~50 MB（远低于分配的 1.5 GiB），这意味着可以按"分配值"超配、以"实际驻留"来装箱。结合快照空闲回收，实际可承载的沙盒数远高于内存物理限制所推算的数字，每沙盒摊算成本可以进一步降低。具体超售比例取决于业务负载特征，建议通过压测确定。

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
│         api.sbx.<domain>  ←── 生产外部访问（POC 推荐 port-forward）  │
│                                                                      │
│  DynamoDB  LiteLLM(Bedrock代理)  Karpenter(.metal自动扩缩+预装kata)  │
└──────────────────────────────────────────────────────────────────────┘
```

---

### 快速开始（Agent 部署指南）

> 将以下提示词复制给 Claude Code / Cursor / 任意支持代码执行的 Agent，即可引导完整部署。
> 完整步骤手册见 **[docs/deploy.md](docs/deploy.md)**。

```
你是一名 AWS 基础设施部署工程师，负责在 AWS 上部署一套 AI Agent 沙盒平台。

任务：完整阅读并按顺序执行 docs/deploy.md 中的所有步骤（Step 0 ~ Step 9）。
遇到错误时先排查根因，修复后再继续，不要跳过任何步骤。

⚠️ 关键注意事项（执行前必读）：
1. 认证安全：Step 6 必须传入 api_keys 和 litellm_master_key（用 openssl rand -hex 32 生成），
   不能留空——控制面无 key 时所有受保护接口返回 503。
2. 不要安装 kata-deploy DaemonSet：Kata 由 Step 7 的 Karpenter EC2NodeClass.userData 预装，
   DaemonSet 方式会导致 c6g.metal 节点 hang 12 分钟并触发节点替换死循环。
3. arm64 镜像：Step 5 的 build_and_push.sh 须在 arm64 机器上执行（M 系列 Mac 或 .metal 节点）。
4. 计费：c6g.metal 约 $2.3/hr，测试完成后立即执行 docs/deploy.md 中的【清理】步骤。

开始前先确认：
- AWS CLI 已配置（需要 EKS / EC2 / IAM / DynamoDB / ECR / S3 权限）
- 已安装 kubectl, terraform (≥1.5), helm, git
- c6g.metal vCPU 配额已申请（64 vCPU，默认配额不足需提前提单）

确认就绪后，请打开 docs/deploy.md，从 Step 0 开始执行。

【详细步骤见 docs/deploy.md（Step 0 ~ 清理，共约 10 步）】

以下仅展示第一步，其余步骤请 Agent 直接读取 docs/deploy.md 执行：

【Step 0: 克隆代码库】
git clone https://github.com/teaguexiao/aws-self-hosted-sandbox.git
cd aws-self-hosted-sandbox
export AWS_REGION=us-east-1

# ↓ 后续步骤全部在 docs/deploy.md 中，请继续阅读并执行 Step 1 ~ Step 9 + 清理。

【Step 1: 创建 DynamoDB 状态表】
cd terraform/stage1-dynamodb
terraform init && terraform apply -auto-approve
# 验证：aws dynamodb list-tables --region us-east-1 | grep claude-sbx

# ── Step 2 ~ Step 9 + 清理 详见 docs/deploy.md ──
# （含：EKS集群/Kata RuntimeClass/ingress-nginx/ECR镜像构建/
#   控制面+LiteLLM/Karpenter NodePool/验证/API使用/资源清理）
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

*本项目是生产级参考实现，可作为在 AWS 上自建 Agent 沙盒平台的基础。*
