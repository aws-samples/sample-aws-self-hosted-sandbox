# Sandbox Control Plane

统一沙盒控制面 API —— 后端可插拔(Firecracker / Kata)。

## 目录结构

```
sandbox-api/
  app.py            # HTTP API 服务(Fly Machines 风格接口)
  driver.py         # SandboxDriver Protocol(抽象接口)
  db.py             # DynamoDB 封装(状态/lease/幂等/warm pool)
  warm_pool.py      # 暖池(FC: 预快照; Kata: SandboxWarmPool CRD)
  drivers/
    firecracker.py  # FirecrackerDriver → node-agent HTTP API
    kata.py         # KataDriver → kubectl / K8s API
  Dockerfile        # 控制面镜像(arm64)
  smoke_test.py     # 本地冒烟测试(moto mock,无需真实 AWS)

node-agent/
  main.py           # on-host 执行手(tap/jailer/FC snapshot/S3)
  Dockerfile        # node-agent 镜像(arm64)
```

## API 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /sandboxes | 创建沙盒(支持 idempotency_key) |
| GET | /sandboxes?tenant_id=x | 列出沙盒 |
| GET | /sandboxes/{id} | 查单个 |
| GET | /sandboxes/{id}/wait?state=running&timeout=30 | 等待状态 |
| DELETE | /sandboxes/{id} | 销毁 |
| POST | /sandboxes/{id}/suspend | 挂起+快照(FC only) |
| POST | /sandboxes/{id}/resume | 从快照恢复(FC only) |
| POST | /sandboxes/{id}/exec | 在沙盒内执行命令 |
| GET | /sandboxes/{id}/locate | 定位 VMM(调试) |
| GET | /capabilities | 当前 driver 能力 |

Kata driver 的 suspend/resume 返回 `501`(capability 模型)。

## 本地冒烟测试(无需 AWS)

```bash
pip install "moto[dynamodb]" boto3
cd <project-root>
python3 sandbox-api/smoke_test.py
# 期望: 19/19 PASS
```

## 部署到 EKS

### 1. 基础设施

```bash
# EKS 集群(若未建,见 terraform/phase3)
aws eks update-kubeconfig --name claude-sbx --region us-east-1

# DynamoDB 表
cd terraform/stage1-dynamodb && terraform apply
```

### 2. 构建并推送镜像

```bash
bash scripts/build_and_push.sh
# 输出 ECR URL 用于下一步
```

### 3. 部署控制面

```bash
cd terraform/stage2-control-plane && terraform init
terraform apply \
  -var="sandbox_image=<ACCT>.dkr.ecr.us-east-1.amazonaws.com/claude-sbx:poc" \
  -var="control_plane_image=<ACCT>.dkr.ecr.us-east-1.amazonaws.com/sandbox-control-plane:latest" \
  -var="node_agent_image=<ACCT>.dkr.ecr.us-east-1.amazonaws.com/node-agent:latest"
```

### 4. 端到端测试

```bash
bash scripts/e2e_test.sh
# Kata driver:
bash scripts/e2e_test.sh --driver kata
# FC driver(需 .metal 节点 + node-agent 就绪):
bash scripts/e2e_test.sh --driver firecracker
```

## 关键环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| SANDBOX_DRIVER | kata | `firecracker` \| `kata` |
| DYNAMODB_TABLE | sandboxes | 主状态表名 |
| AWS_REGION | us-east-1 | |
| SANDBOX_IMAGE | (必填) | 沙盒容器镜像 |
| LITELLM_URL | http://litellm... | LiteLLM 网关(凭据隔离) |
| SANDBOX_DOMAIN | sbx.example.com | 通配符子域名根 |
| SNAPSHOT_S3_BUCKET | | 快照存储桶(FC 模式) |
| FC_NODES | | node-agent IP 列表,逗号分隔 |
| WARM_POOL_SIZE | 5 | 暖池沙盒数 |
| NODE_AGENT_PORT | 8002 | node-agent 监听端口 |
