# Terraform —— Claude Code 沙盒 POC 基础设施

所有 AWS 基础设施用 Terraform 管理。按阶段逐步 apply；当前主线是裸
Firecracker，并已把 EKS system 控制面节点与 sandbox 数据面节点分离。

## 目录结构

```
terraform/
├── phase1/                单机 Firecracker 实验环境
├── stage1-dynamodb/       sandbox / events / tap-idx / nodes / locks 状态表
├── phase3/                EKS + On-Demand system 节点组 + sandbox 节点组 + 持久 EBS
└── stage2-control-plane/  控制面、node-agent、LiteLLM、Ingress、可观测性与对应 IAM/K8s 资源
```

> Firecracker 安装、guest 内核、rootfs 构建、microVM 启动是**主机内**操作,不归 Terraform 管 —— 见
> [`docs/POC-技术文档.md`](../docs/POC-技术文档.md) 第 3 节。Terraform 只负责 AWS 侧资源。

## Phase 1 —— 立即可用

```bash
cd phase1

# 1) 准备 SSH key(若还没有)
ssh-keygen -t ed25519 -f ~/.ssh/claude-sbx-poc -N ""

# 2) init + apply(自动填入你的公网 IP 限制 SSH)
terraform init
terraform apply -var="my_ip_cidr=$(curl -s https://checkip.amazonaws.com)/32"

# 3) 登录主机(或用 SSM Session,免开 22 口)
$(terraform output -raw ssh_command)

# 4) 主机内一键准备:装 docker + 编 FUSE 内核 + 构建 rootfs(含 Claude Code+JuiceFS) + 配网
#    (scripts/setup-host.sh 已含全部步骤,幂等可重跑)
sudo bash setup-host.sh
#    设 SKIP_FUSE_KERNEL=1 可跳过编内核(仅本地 ext4 workspace、不挂 JuiceFS 时)

# 5) 起 microVM
sudo firecracker --no-api --config-file /opt/sbx/vmconfig.json

# 6) 用完即毁(.metal 按小时计费,较贵)
terraform destroy -var="my_ip_cidr=$(curl -s https://checkip.amazonaws.com)/32"
```

> ⚠️ **关键(实测坐实 R3):** Firecracker CI 默认内核【无 FUSE】,JuiceFS/s3fs/mountpoint
> 在 guest 内挂不上。`setup-host.sh` 默认调用 `scripts/build-fuse-kernel.sh` 编一个带 FUSE 的
> arm64 内核(c6g.metal 64 核 native 编译几分钟),vmconfig 指向 `/opt/sbx/vmlinux-fuse`。

Phase 1 创建的资源:
- 1 台 Firecracker 主机（默认 `c6g.metal`；x86 默认 `i7i.8xlarge`）+ 200 GiB gp3
- 主机 IAM 角色:`bedrock:InvokeModel*`(限 `anthropic.*` 模型与 `us.anthropic.*` inference profile)→ 沙盒走 IAM 凭据链调 Bedrock,无需长期 key
- 安全组:仅放行你的 IP 的 22 口(也挂了 SSM,可免 22 口用 Session Manager 登录)
- ECR 仓库 `claude-sbx`

## EKS 控制面 / 数据面分离

`phase3` 创建两个职责明确的托管节点组：

- `system_arm64`：默认 `2 × m7g.large`，固定 arm64、On-Demand，承载控制面、
  LiteLLM、Ingress 和系统 Pod。
- `sandbox_<arch>`：默认 1 台，只承载 node-agent 与 Firecracker microVM；
  带 `sandbox=true` label 和 `dedicated=sandbox:NoSchedule` taint。

控制面节点不使用 Spot。sandbox 节点未来可单独切 Spot，但应先完成中断消费、
快照疏散和跨节点恢复自动化；当前 Terraform 仍使用 On-Demand。

## 选择 sandbox 数据节点架构（Graviton / Intel x86）

`node_arch` 只描述 sandbox 数据节点，system 节点始终为 arm64：

| `node_arch` | 默认实例 | 虚拟化方式 | AMI |
|---|---|---|---|
| `arm64`（默认） | `c6g.metal` | Graviton 裸金属 KVM | AL2023 ARM64 |
| `amd64` | `i7i.8xlarge` | Nitro nested virtualization | AL2023 x86_64 |

x86 支持全部 `i7i.*` 规格，默认选择 `i7i.8xlarge`。Terraform 会显式开启
`cpu_options.nested_virtualization=enabled`：

```bash
# Intel x86 默认 i7i.8xlarge
terraform apply -var="node_arch=amd64" ...

# 覆盖为其他 i7i 规格
terraform apply \
  -var="node_arch=amd64" \
  -var="sandbox_instance_type=i7i.16xlarge" ...
```

Phase 3 默认把沙盒节点钉在 VPC 的第一个可用区，以保证持久状态 EBS 可在节点间挂载。
若 ASG 报 `InsufficientInstanceCapacity`，可用
`-var="sandbox_az_index=1"`（region-b）或 `2`（region-c）切换整个节点组所在 AZ。

切到 x86 时会变化的是 sandbox AMI、默认实例、Firecracker 二进制与 guest
内核/rootfs 架构。控制面和 LiteLLM 仍调度到 arm64 system 节点。镜像应分开构建：

```bash
bash scripts/build_and_push.sh \
  --control-plane-platform linux/arm64 \
  --node-agent-platform linux/amd64
```

> x86 部署前需确认目标区域提供所选 i7i 规格且 `SupportedFeatures` 包含
> `nested-virtualization`。完整命令见 `docs/deploy.md` Step 0.5。

最终验证在 `sandbox_az_index=2`（`us-east-1c`）完成：`2 × m7g.large`
system + `1 × i7i.8xlarge` sandbox 的调度隔离、Firecracker 生命周期、
恢复后 exec、LiteLLM Bedrock 调用、控制面 leader 故障转移和完整清理均通过。
详见[控制面与数据面分离 i7i 真机测试报告](../docs/控制面数据面分离-i7i真机测试报告-2026-08-11.md)。

## Stage 2 可观测性

`stage2-control-plane/observability.tf` 与 `p2_observability.tf` 提供三个显式开关：

| 开关 | 资源 |
|---|---|
| `enable_observability_stack=true` | 集群内 Prometheus、Alertmanager、Grafana、ServiceMonitor/PodMonitor、5 类告警、8 面板 Dashboard |
| `enable_amp_remote_write=true` | AMP workspace、Prometheus remote-write IRSA/SigV4；要求上一个开关同时为 `true` |
| `enable_p2_observability=true` | CloudWatch Logs、Fluent Bit、ADOT/X-Ray、AMG datasource/Dashboard 自动配置；要求前两个开关为 `true` |

可选传入已有 AMG workspace：

```hcl
managed_grafana_workspace_id       = "g-xxxxxxxxxx"
managed_grafana_vpc_id             = "vpc-..."
managed_grafana_subnet_ids         = ["subnet-a", "subnet-b"]
managed_grafana_security_group_id  = "sg-..."
```

Terraform 会给 AMG workspace role 增加最小 AMP 查询权限，并在其 VPC 创建
`aps-workspaces` Interface Endpoint；不会创建或删除 AMG workspace。启用 P2 后，
`configure-managed-grafana.sh` 使用 15 分钟 token 幂等配置 datasource/Dashboard 并立即清理。
完整参数和验证命令见
[`docs/deploy.md` Step 6.2](../docs/deploy.md#step-62-部署可观测性p1推荐)。

Prometheus 与 node-agent 的指标不使用 sandbox ID 标签。Grafana admin password 通过
Helm `set_sensitive` 注入，不放进普通 values。Kubernetes 和 Helm provider 使用
`aws eks get-token` exec credential，长时间 apply 可以刷新 EKS token；运行 Terraform
的环境必须能从 `PATH` 调用 AWS CLI。

真实 AWS 验证覆盖 29/29 targets、AMP remote-write、AMG datasource/query、
快照损坏告警和 Terraform 零漂移，见
[P1 可观测性真机测试报告](../docs/P1可观测性-真机测试报告-2026-08-12.md)。
P2 集中日志、tracing 与 AMG 自动化证据见
[P2 可观测性真机测试报告](../docs/P2可观测性-真机测试报告-2026-08-12.md)。

## 前置：申请 EC2 vCPU 配额

`c6g.metal` 需要 64 vCPU，`i7i.8xlarge` 需要 32 vCPU。若 apply 报
`VcpuLimitExceeded`，到 Service Quotas 为对应实例类别申请提额。

## 鉴权说明

Phase 1 的 Terraform 给主机挂了 **Bedrock IAM 角色**(对应 POC 文档 1.8 方式 B),沙盒走宿主凭据链即可,无需把 key 写进代码或环境变量 —— 这也更接近"凭据不进沙盒"的生产形态。
若想用方式 A(Bedrock API key),在 guest 内 `export AWS_BEARER_TOKEN_BEDROCK=...` 即可,Terraform 不管 key。

> ⚠️ 上线前到 Bedrock 控制台 "Model access" 开通 Anthropic 模型,并复制准确的 inference profile ID(us-east-1 通常需 `us.` 跨区前缀)。

## 推荐部署顺序

1. `stage1-dynamodb`：创建 5 张状态与协调表。
2. 构建对应数据节点架构的 rootfs 并上传 S3，作为节点初始化输入。
3. `phase3`：创建 EKS、system/sandbox 节点组和 sandbox 持久状态 EBS。
4. 分别构建 arm64 控制面镜像和匹配数据节点架构的 node-agent 镜像。
5. `stage2-control-plane`：部署控制面、node-agent、LiteLLM、可选 Ingress 和可选可观测性栈。

完整变量、验证和销毁步骤以 [`docs/deploy.md`](../docs/deploy.md) 为准。
