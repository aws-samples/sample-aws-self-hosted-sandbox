# Phase 3 基础设施 —— EKS 集群 + .metal 托管节点组(验 H3:Kata 编排 + 任意端口)
#
# 目标:用 Terraform 管理 EKS 控制平面 + 一个 .metal 节点组(打 sandbox=true label)。
#       Kata 安装、RuntimeClass、ingress-nginx、ACM、测试 Pod 是集群内操作(kubectl/helm),不归此处。
#
# 架构:由 node_arch 变量控制 —— arm64(Graviton c6g.metal,默认) 或 amd64(Intel x86 c5n.metal)。
#       terraform apply -var="node_arch=amd64"  # 切到 Intel x86
#
# ⚠️ 计费:EKS 控制平面 $0.10/hr + .metal 节点(c6g.metal≈$2.32/hr,c5n.metal≈$3.89/hr)。用完务必 destroy。
#
# 用法:
#   terraform init
#   terraform apply -var='endpoint_public_access_cidrs=["'$(curl -s https://checkip.amazonaws.com)'/32"]'
#   aws eks update-kubeconfig --name claude-sbx --region us-east-1
#   kubectl get nodes
#
# 销毁:terraform destroy

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "cluster_name" {
  type    = string
  default = "claude-sbx"
}

variable "node_arch" {
  type        = string
  default     = "arm64"
  description = "节点 CPU 架构:arm64(Graviton,默认) 或 amd64(Intel x86)。决定 AMI 类型、默认 .metal 机型、Firecracker/内核下载架构。"
  validation {
    condition     = contains(["arm64", "amd64"], var.node_arch)
    error_message = "node_arch 仅支持 \"arm64\" 或 \"amd64\"。"
  }
}

variable "metal_instance_type" {
  type        = string
  default     = "" # 留空时按 node_arch 选默认机型(arm64→c6g.metal / amd64→c5n.metal)
  description = ".metal 实例类型。留空则由 node_arch 决定:arm64=c6g.metal,amd64=c5n.metal(最便宜的 Intel x86 裸金属)。"
}

locals {
  # 架构派生:AMI 类型、默认 .metal 机型、Firecracker/内核下载用的架构标识(uname -m 风格)
  arch_cfg = {
    arm64 = {
      ami_type      = "AL2023_ARM_64_STANDARD"
      default_metal = "c6g.metal"  # Graviton 裸金属
      fc_arch       = "aarch64"    # Firecracker 发行包 / CI vmlinux 的架构后缀
      rootfs_key    = "rootfs/rootfs-juicefs.tar.gz"
    }
    amd64 = {
      ami_type      = "AL2023_x86_64_STANDARD"
      default_metal = "c5n.metal"  # 最便宜的主流 Intel x86 裸金属(72 vCPU/192GiB)
      fc_arch       = "x86_64"
      rootfs_key    = "rootfs/rootfs-juicefs-x86_64.tar.gz" # 需另行构建 x86 rootfs(见 setup-host.sh)
    }
  }
  node_arch_cfg = local.arch_cfg[var.node_arch]
  metal_type    = var.metal_instance_type != "" ? var.metal_instance_type : local.node_arch_cfg.default_metal
}

variable "endpoint_public_access_cidrs" {
  type        = list(string)
  description = "允许访问 EKS 公网 API endpoint 的来源 CIDR(必填,无默认值以避免误开全网)。收窄到自己的 IP,apply 时传入:terraform apply -var='endpoint_public_access_cidrs=[\"'$(curl -s https://checkip.amazonaws.com)'/32\"]'"
}

# B2(FirecrackerDriver): 节点 userData 从此 S3 URI 拉取最小可启动 rootfs.tar.gz
variable "rootfs_s3_uri" {
  type        = string
  description = "S3 URI of the minimal bootable arm64 rootfs tarball (B2 FC mode)"
  default     = ""
}

# ---------- VPC(EKS 专用,3 AZ;裸金属在多 AZ 提高可得性) ----------
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = "${var.cluster_name}-vpc"
  cidr = "10.0.0.0/16"

  azs             = ["${var.region}a", "${var.region}b", "${var.region}c"]
  private_subnets = ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]
  public_subnets  = ["10.0.101.0/24", "10.0.102.0/24", "10.0.103.0/24"]

  # POC:禁用 NAT(此共享账号 EIP 配额已被占满,AllocateAddress 会失败)。
  # 节点组改放公有子网 + 自动分配公网 IP,直接出网,无需 NAT。
  enable_nat_gateway      = false
  enable_dns_hostnames    = true
  map_public_ip_on_launch = true

  # EKS + NLB(ingress)所需子网标签
  public_subnet_tags = {
    "kubernetes.io/role/elb"                    = "1"
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
  }
  private_subnet_tags = {
    "kubernetes.io/role/internal-elb"           = "1"
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
  }
}

# ---------- EKS 集群 + Graviton .metal 节点组 ----------
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = var.cluster_name
  cluster_version = "1.31"

  cluster_endpoint_public_access = true
  # 收窄到指定 CIDR;留空时模块默认 0.0.0.0/0(对全网开放)——生产/共享账号务必传入自己的 IP。
  cluster_endpoint_public_access_cidrs     = var.endpoint_public_access_cidrs
  enable_cluster_creator_admin_permissions = true

  vpc_id = module.vpc.vpc_id
  # 控制平面 ENI 放私有子网;节点组单独指定公有子网(见 node group subnet_ids)
  subnet_ids = module.vpc.private_subnets

  # POC:节点放公有子网,拿公网 IP 直接出网(无 NAT)
  eks_managed_node_group_defaults = {
    subnet_ids = module.vpc.public_subnets
    # B2: 节点 userData 需从 S3 拉 rootfs.tar.gz → 给节点角色 S3 只读
    iam_role_additional_policies = {
      s3_readonly = "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"
    }
  }

  # 托管节点组:集群【系统节点】—— 跑控制面 / ingress-nginx / LiteLLM / Karpenter controller。
  #
  # ⚠️ 架构说明（方案 A）：本节点组【不】承载 sandbox，因此【不打 sandbox=true label】。
  #    sandbox 由 Karpenter 的 kata-metal NodePool（Step 7，UserData 预装 Kata）承载。
  #    控制面 KataDriver 创建的 sandbox pod 用 nodeSelector sandbox=true，只会落到
  #    Karpenter 起的、带 sandbox=true + katacontainers.io/kata-runtime=true 的 .metal 节点。
  #
  #    本组用 .metal 仅因 POC 早期需要它引导集群；纯系统节点其实无需裸金属，
  #    可把 metal_instance_type 改小（如 c6g.xlarge）显著省成本——改小后系统组件照常运行。
  eks_managed_node_groups = {
    "metal_${var.node_arch}" = {
      ami_type       = local.node_arch_cfg.ami_type
      instance_types = [local.metal_type]
      # Firecracker 跨机快照演示需两台常驻(min=2);x86/arm64 由 node_arch 参数化
      min_size       = 2
      max_size       = 2
      desired_size   = 2

      block_device_mappings = {
        xvda = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size = 200
            volume_type = "gp3"
          }
        }
      }

      # pre_bootstrap_user_data: kubelet 시작 전 실행
      # Firecracker + Redis + JuiceFS + rootfs 를 사전 설치
      # → containerd 재시작 없음 → EKS 헬스체크 미트리거 → 노드 교체 사이클 없음
      pre_bootstrap_user_data = <<-EOT
        #!/bin/bash
        # pre_bootstrap: kubelet 시작 전 실행
        # ⚠️ 긴 작업 금지 (docker/dnf 설치 금지 → kubelet heartbeat 중단 → 노드 교체 사이클)
        # 최소한만 — Firecracker 바이너리 + 커널 + 디렉토리만 설치
        exec >> /var/log/userdata-pre.log 2>&1
        echo "[pre-bootstrap] START $(date)"

        mkdir -p /opt/sbx /var/lib/sbx

        # Firecracker (바이너리만, 빠름) —— 架构由 Terraform node_arch 注入
        ARCH=${local.node_arch_cfg.fc_arch}
        VER=$(curl -sf https://api.github.com/repos/firecracker-microvm/firecracker/releases/latest \
          | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])" 2>/dev/null || echo "v1.16.0")
        curl -sfL "https://github.com/firecracker-microvm/firecracker/releases/download/$${VER}/firecracker-$${VER}-$${ARCH}.tgz" \
          -o /tmp/fc.tgz 2>/dev/null && \
        tar -xzf /tmp/fc.tgz -C /tmp 2>/dev/null && \
        mv "/tmp/release-$${VER}-$${ARCH}/firecracker-$${VER}-$${ARCH}" /usr/local/bin/firecracker 2>/dev/null && \
        chmod +x /usr/local/bin/firecracker && \
        echo "[pre-bootstrap] Firecracker OK" || echo "[pre-bootstrap] Firecracker install failed (non-fatal)"

        # 커널 (16MB, 빠름) —— 架构由 Terraform node_arch 注入
        curl -sfL "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.10/${local.node_arch_cfg.fc_arch}/vmlinux-5.10.223" \
          -o /opt/sbx/vmlinux 2>/dev/null && echo "[pre-bootstrap] Kernel OK" || true

        # rootfs: S3 에서 tar.gz 다운로드 → ext4 생성
        # (Firecracker 快照方案: 最小可启动 rootfs, 由 build-min-rootfs.sh 构建上传到用户桶,
        #  路径经 var.rootfs_s3_uri 传入; node_arch 决定构建时的 --platform)
        aws s3 cp ${var.rootfs_s3_uri} \
          /tmp/rootfs.tar.gz --region ${var.region} 2>/dev/null && \
        dd if=/dev/zero of=/opt/sbx/rootfs.ext4 bs=1M count=2048 status=none 2>/dev/null && \
        mkfs.ext4 /opt/sbx/rootfs.ext4 -q 2>/dev/null && \
        mkdir -p /tmp/rootfs_mount && \
        mount /opt/sbx/rootfs.ext4 /tmp/rootfs_mount 2>/dev/null && \
        tar -xzf /tmp/rootfs.tar.gz -C /tmp/rootfs_mount 2>/dev/null && \
        umount /tmp/rootfs_mount 2>/dev/null && \
        echo "[pre-bootstrap] rootfs OK" || echo "[pre-bootstrap] rootfs setup failed (non-fatal)"

        # Redis + JuiceFS 클라이언트 (설치 실패해도 계속)
        dnf install -y redis6 fuse3 2>/dev/null || true
        systemctl enable --now redis6 2>/dev/null || true
        curl -sSL https://d.juicefs.com/install | sh - 2>/dev/null || true

        # NAT
        sysctl -w net.ipv4.ip_forward=1 2>/dev/null || true

        echo "[pre-bootstrap] DONE $(date)"
      EOT

      # B2(FirecrackerDriver 模式): 本组承载 sandbox(裸 Firecracker microVM),
      # 打 sandbox=true 让 node-agent DaemonSet 调度上来。
      # (kata 模式下本组是纯系统节点不打此 label;B2 改为兼作沙盒节点)
      labels = {
        role    = "system"
        sandbox = "true"
      }
    }
  }

  # 节点角色加 Bedrock 调用权限(沙盒走节点凭据链调 Bedrock;生产改 IRSA/出口代理)
  # 保留节点安全组的集群标签，供 Karpenter 安全组选择器使用
  node_security_group_tags = {
    "kubernetes.io/cluster/${var.cluster_name}" = "owned"
  }
}

# Bedrock 权限已迁移到 LiteLLM IRSA(terraform/stage2-control-plane/litellm.tf)
# 节点角色不再持有 Bedrock 权限 —— 沙盒内代码无法直接调 Bedrock(R8 凭据隔离落地)
# 沙盒走: Claude Code → ANTHROPIC_BASE_URL=http://litellm.litellm:4000 → LiteLLM Pod → Bedrock

# ---------- ECR 仓库(直接创建,不依赖 phase1) ----------
resource "aws_ecr_repository" "sbx" {
  name                 = "claude-sbx"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  tags = { Project = "claude-sbx-poc" }
}

data "aws_ecr_repository" "sbx" {
  name       = aws_ecr_repository.sbx.name
  depends_on = [aws_ecr_repository.sbx]
}

# ---------- 输出 ----------
output "cluster_name" {
  value = module.eks.cluster_name
}

output "configure_kubectl" {
  value = "aws eks update-kubeconfig --name ${module.eks.cluster_name} --region ${var.region}"
}

output "ecr_repo_url" {
  value = data.aws_ecr_repository.sbx.repository_url
}
