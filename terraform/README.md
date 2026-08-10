# Terraform —— Claude Code 沙盒 POC 基础设施

所有 AWS 基础设施用 Terraform 管理。按 POC 阶段分目录,逐阶段 apply。

## 目录结构

```
terraform/
├── phase1/   单 Graviton .metal 主机 + Bedrock IAM 权限 + ECR  → 验 H1(裸 Firecracker + Claude Code)
├── phase3/   EKS + Kata 节点组 + 共享 NLB/Ingress + ACM       → 验 H3(编排 + 任意端口)  [待补]
└── (Phase 2/5 复用上面资源,无独立基础设施)
```

> Firecracker 安装、guest 内核、rootfs 构建、microVM 启动是**主机内**操作,不归 Terraform 管 —— 见 `../POC-技术文档.md` 第 3 节。Terraform 只负责 AWS 侧资源。

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

## 选择 CPU 架构（Graviton / Intel x86）

所有阶段都支持 `node_arch`，部署时二选一：

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

切到 x86 时，这些会自动随架构变化：AMI、默认实例、Karpenter NodePool 的
`kubernetes.io/arch`、Firecracker 二进制与 CI 内核下载架构、JuiceFS 元数据 Redis 节点族
（`t4g`→`t3`）。rootfs 与容器镜像构建须设置 `PLATFORM=linux/amd64`。

> x86 部署前需确认目标区域提供所选 i7i 规格且 `SupportedFeatures` 包含
> `nested-virtualization`。完整命令见 `docs/deploy.md` Step 0.5。

`i7i.8xlarge` 已在 `us-east-1b` 完成真实 AWS 生命周期与鉴权 E2E；宿主 KVM、
amd64 guest、持久状态 EBS 和完整清理均通过。详见
[`docs/i7i-e2e-test-report-2026-08-10.md`](../docs/i7i-e2e-test-report-2026-08-10.md)。

## 前置：申请 EC2 vCPU 配额

`c6g.metal` 需要 64 vCPU，`i7i.8xlarge` 需要 32 vCPU。若 apply 报
`VcpuLimitExceeded`，到 Service Quotas 为对应实例类别申请提额。

## 鉴权说明

Phase 1 的 Terraform 给主机挂了 **Bedrock IAM 角色**(对应 POC 文档 1.8 方式 B),沙盒走宿主凭据链即可,无需把 key 写进代码或环境变量 —— 这也更接近"凭据不进沙盒"的生产形态。
若想用方式 A(Bedrock API key),在 guest 内 `export AWS_BEARER_TOKEN_BEDROCK=...` 即可,Terraform 不管 key。

> ⚠️ 上线前到 Bedrock 控制台 "Model access" 开通 Anthropic 模型,并复制准确的 inference profile ID(us-east-1 通常需 `us.` 跨区前缀)。

## Phase 3(待补)

EKS + `.metal` 托管节点组 + Kata、共享 ingress-nginx(单 NLB)、ACM 通配符证书。
建议确认 Phase 1(H1)通过、且 Kata+CH 在 arm64 验证可行后再写,避免提前固化未验证的选型。
