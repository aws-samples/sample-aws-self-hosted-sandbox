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
- 1 台 `c6g.metal`(默认;`-var="metal_instance_type=m7g.metal"` 换大内存)+ 200 GiB gp3
- 主机 IAM 角色:`bedrock:InvokeModel*`(限 `anthropic.*` 模型与 `us.anthropic.*` inference profile)→ 沙盒走 IAM 凭据链调 Bedrock,无需长期 key
- 安全组:仅放行你的 IP 的 22 口(也挂了 SSM,可免 22 口用 Session Manager 登录)
- ECR 仓库 `claude-sbx`

## 选择 CPU 架构(Graviton / Intel x86)

所有阶段(phase1 / phase3 / stage2)都支持 `node_arch` 变量,二选一:

| `node_arch` | 默认 .metal 机型 | 规格 | 约价/hr | AMI |
|---|---|---|---|---|
| `arm64`(默认) | `c6g.metal` | 64 vCPU / 128 GiB | ~$2.32 | AL2023 ARM64 |
| `amd64` | `c5n.metal` | 72 vCPU / 192 GiB | ~$3.89 | AL2023 x86_64 |

`c5n.metal` 是当前最便宜的主流 Intel x86 裸金属。切换方式:

```bash
# Intel x86 节点(各阶段 apply 都加这个 -var,需保持一致)
terraform apply -var="node_arch=amd64" ...
# 也可显式覆盖机型,如更大内存的 m5zn.metal:
terraform apply -var="node_arch=amd64" -var="metal_instance_type=c5.metal" ...
```

切到 x86 时,这些会自动随架构变化:AMI 类型、默认 .metal 机型、Karpenter NodePool 的
`kubernetes.io/arch`、Firecracker 二进制与 CI 内核下载架构、JuiceFS 元数据 Redis 节点族
(`t4g`→`t3`)。引导脚本(`setup-host.sh` / `build-fuse-kernel.sh`)默认探测宿主架构,
x86 上自动用 `make ARCH=x86` 编未压缩 `vmlinux`。

> ⚠️ **x86 待办项(部署前确认):**
> 1. **沙盒/控制面镜像**:`scripts/build_and_push.sh` 默认 `--platform linux/arm64`,
>    x86 需 `PLATFORM=linux/amd64`(或多架构 `linux/arm64,linux/amd64`)重新构建推送。
> 2. **预构建 rootfs**:phase3 UserData 从 S3 拉 `rootfs/rootfs-juicefs-x86_64.tar.gz`,
>    需先在 x86 .metal 上用 `setup-host.sh` 产出并上传到该 key。
> 3. **首次务必验证 x86 guest kernel**:Firecracker CI 的 `x86_64/vmlinux` 路径与 FUSE 编译
>    在 x86 上需实测一遍(见下方"验证"小节的思路)。

## 前置:申请 .metal 配额

`.metal` 受 EC2 On-Demand vCPU 配额限制(代码 `L-1216C47A`)。`c6g.metal` = 64 vCPU,
`c5n.metal` = 72 vCPU。若 apply 报 `VcpuLimitExceeded`,到 Service Quotas 申请提额(可能需 1–2 天)。

## 鉴权说明

Phase 1 的 Terraform 给主机挂了 **Bedrock IAM 角色**(对应 POC 文档 1.8 方式 B),沙盒走宿主凭据链即可,无需把 key 写进代码或环境变量 —— 这也更接近"凭据不进沙盒"的生产形态。
若想用方式 A(Bedrock API key),在 guest 内 `export AWS_BEARER_TOKEN_BEDROCK=...` 即可,Terraform 不管 key。

> ⚠️ 上线前到 Bedrock 控制台 "Model access" 开通 Anthropic 模型,并复制准确的 inference profile ID(us-east-1 通常需 `us.` 跨区前缀)。

## Phase 3(待补)

EKS + `.metal` 托管节点组 + Kata、共享 ingress-nginx(单 NLB)、ACM 通配符证书。
建议确认 Phase 1(H1)通过、且 Kata+CH 在 arm64 验证可行后再写,避免提前固化未验证的选型。
