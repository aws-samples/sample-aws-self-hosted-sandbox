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

# 3) 登录主机,按 POC 文档 1.2–1.8 装 Firecracker、构建 rootfs、起 microVM
$(terraform output -raw ssh_command)

# 4) 用完即毁(.metal 按小时计费,较贵)
terraform destroy -var="my_ip_cidr=$(curl -s https://checkip.amazonaws.com)/32"
```

Phase 1 创建的资源:
- 1 台 `c7g.metal`(默认;`-var="metal_instance_type=m7g.metal"` 换大内存)+ 200 GiB gp3
- 主机 IAM 角色:`bedrock:InvokeModel*`(限 `anthropic.*` 模型与 `us.anthropic.*` inference profile)→ 沙盒走 IAM 凭据链调 Bedrock,无需长期 key
- 安全组:仅放行你的 IP 的 22 口(也挂了 SSM,可免 22 口用 Session Manager 登录)
- ECR 仓库 `claude-sbx`

## 前置:申请 .metal 配额

`.metal` 受 EC2 On-Demand vCPU 配额限制(代码 `L-1216C47A`)。`c7g.metal` = 64 vCPU。若 apply 报 `VcpuLimitExceeded`,到 Service Quotas 申请提额(可能需 1–2 天)。

## 鉴权说明

Phase 1 的 Terraform 给主机挂了 **Bedrock IAM 角色**(对应 POC 文档 1.8 方式 B),沙盒走宿主凭据链即可,无需把 key 写进代码或环境变量 —— 这也更接近"凭据不进沙盒"的生产形态。
若想用方式 A(Bedrock API key),在 guest 内 `export AWS_BEARER_TOKEN_BEDROCK=...` 即可,Terraform 不管 key。

> ⚠️ 上线前到 Bedrock 控制台 "Model access" 开通 Anthropic 模型,并复制准确的 inference profile ID(us-east-1 通常需 `us.` 跨区前缀)。

## Phase 3(待补)

EKS + `.metal` 托管节点组 + Kata、共享 ingress-nginx(单 NLB)、ACM 通配符证书。
建议确认 Phase 1(H1)通过、且 Kata+CH 在 arm64 验证可行后再写,避免提前固化未验证的选型。
