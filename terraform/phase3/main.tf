# Phase 3 基础设施 —— EKS 集群 + .metal 托管节点组(裸 Firecracker 沙盒节点 + 持久状态 EBS)
#
# 目标:用 Terraform 管理 EKS 控制平面 + 一个 .metal 节点组(打 sandbox=true label)。
#       控制面 / node-agent / LiteLLM 等集群内资源由 stage2-control-plane 部署,不归此处。
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
  default_tags {
    tags = {
      Project   = "claude-sbx-poc"
      ManagedBy = "terraform"
    }
  }
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

variable "metal_node_count" {
  type        = number
  default     = 1
  description = ".metal 节点常驻台数(min=max=desired)。成本优先默认 1 台(单机可测全生命周期);跨机快照/spot 疏散演示需设为 2。"
}

locals {
  # 架构派生:AMI 类型、默认 .metal 机型、Firecracker/内核下载用的架构标识(uname -m 风格)
  arch_cfg = {
    arm64 = {
      ami_type      = "AL2023_ARM_64_STANDARD"
      default_metal = "c6g.metal" # Graviton 裸金属
      fc_arch       = "aarch64"   # Firecracker 发行包 / CI vmlinux 的架构后缀
      rootfs_key    = "rootfs/rootfs-juicefs.tar.gz"
    }
    amd64 = {
      ami_type      = "AL2023_x86_64_STANDARD"
      default_metal = "c5n.metal" # 最便宜的主流 Intel x86 裸金属(72 vCPU/192GiB)
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

# 自定义镜像:额外的命名 rootfs 模板(逗号分隔 name 列表)。节点从 rootfs_s3_uri 同目录拉
# rootfs-{name}.tar.gz 造 /opt/sbx/rootfs-{name}.ext4。用 build-rootfs-image.sh <name> 构建上传。
# 默认含 web(自带 demo 站点)。min 无需列出(即默认 rootfs)。
variable "rootfs_images" {
  type        = string
  default     = "web"
  description = "逗号分隔的命名 rootfs 模板列表(除 min 外),节点会各拉一份造 ext4 模板。"
}

# ---------- 方案C:持久状态 EBS(挂 /var/lib/sbx,存快照+rootfs,spot 幸存) ----------
variable "state_ebs_size_gb" {
  type        = number
  default     = 400
  description = "每节点持久状态 EBS 容量(GB)。resume 时每 sandbox 峰值需 base(2G)+merged(2G)≈4G,50 个约 200G,再加 diff/rootfs/余量 → 400G。"
}
variable "state_ebs_iops" {
  type        = number
  default     = 4000
  description = "状态 EBS 的 IOPS(gp3,1000MB/s 吞吐至少需 4000 IOPS)。"
}
variable "state_ebs_throughput" {
  type        = number
  default     = 1000
  description = "状态 EBS 吞吐(MB/s)。1000=gp3 单卷上限,让 50 个 Diff 快照并发落盘 ~16s。"
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

  # 托管 .metal 节点组:既跑系统组件（控制面 / LiteLLM），也【承载 sandbox】——
  #   打 sandbox=true label，让 node-agent DaemonSet 调度上来，在本机直起裸 Firecracker microVM。
  #   挂持久状态 EBS（/dev/sdf → /var/lib/sbx），存内存快照（base + Diff），spot 疏散跨机恢复。
  eks_managed_node_groups = {
    "metal_${var.node_arch}" = {
      ami_type       = local.node_arch_cfg.ami_type
      instance_types = [local.metal_type]
      # Firecracker 跨机快照演示需两台常驻(min=2);x86/arm64 由 node_arch 参数化。
      # 成本优先的单机 demo:降到 1 台(单机可测 create/exec/suspend/resume/destroy 全流程,
      # 仅跨机快照/spot 疏散演示需要 2 台)。由 metal_node_count 变量控制,默认 1。
      min_size     = var.metal_node_count
      max_size     = var.metal_node_count
      desired_size = var.metal_node_count

      # 方案C:两台节点必须【同一 AZ】—— EBS 状态卷不能跨 AZ attach。
      # 钉死到单个 AZ 的公有子网(public_subnets[0] = azs[0] = us-east-1a),否则 EKS 会把两台
      # 分散到不同 AZ,导致 spot 疏散后状态卷无法 attach 到另一 AZ 的新节点。
      subnet_ids = [module.vpc.public_subnets[0]]

      block_device_mappings = {
        xvda = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size = 200
            volume_type = "gp3"
          }
        }
        # 方案C:独立【持久状态 EBS】挂 /var/lib/sbx —— 存所有 sandbox 的内存快照(base+diff)+ rootfs。
        # 高吞吐 gp3(1000MB/s)让 50 个 Diff 快照并发落盘 ~16s。
        # delete_on_termination=false → spot 强制终止后卷幸存,可 attach 到新机恢复(方案C核心)。
        sbxdata = {
          device_name = "/dev/sdf"
          ebs = {
            volume_size           = var.state_ebs_size_gb
            volume_type           = "gp3"
            iops                  = var.state_ebs_iops
            throughput            = var.state_ebs_throughput
            delete_on_termination = false
          }
        }
      }

      # AL2023(nodeadm)下用 cloudinit_pre_nodeadm 注入 shell 脚本 MIME 分段,
      # 在 nodeadm 引导前执行(pre_bootstrap_user_data 是 AL2 时代机制,AL2023 会静默忽略)。
      # Firecracker + Redis + JuiceFS + rootfs 预装,不重启 containerd → 不触发节点替换循环。
      cloudinit_pre_nodeadm = [{
        content_type = "text/x-shellscript; charset=\"us-ascii\""
        content      = <<-EOT
        #!/bin/bash
        # pre_bootstrap: kubelet 시작 전 실행
        # ⚠️ 긴 작업 금지 (docker/dnf 설치 금지 → kubelet heartbeat 중단 → 노드 교체 사이클)
        # 최소한만 — Firecracker 바이너리 + 커널 + 디렉토리만 설치
        exec >> /var/log/userdata-pre.log 2>&1
        echo "[pre-bootstrap] START $(date)"

        mkdir -p /opt/sbx /var/lib/sbx

        # 方案C:挂载【持久状态 EBS】到 /var/lib/sbx —— sandbox 快照(base+diff)+ rootfs 都落这块盘。
        # 它 delete_on_termination=false,spot 终止后幸存,可 attach 到新机恢复。
        # 识别:非根盘、无分区表、无挂载点的块设备(附加的数据卷)。首次为空盘 → mkfs;
        # 已有文件系统(从旧节点迁移来的幸存卷)→ 直接挂,不格式化(否则抹掉数据!)。
        SBX_DISK=""
        for dev in /dev/nvme*n1 /dev/sd[b-z] /dev/xvd[b-z]; do
          [ -b "$dev" ] || continue
          # 跳过根盘及其分区(有挂载点的)
          if lsblk -no MOUNTPOINT "$dev" 2>/dev/null | grep -q .; then continue; fi
          # 跳过有分区表的(根盘通常有 p1/p128)
          parts=$(lsblk -no NAME "$dev" 2>/dev/null | wc -l)
          [ "$parts" -gt 1 ] && continue
          SBX_DISK="$dev"; break
        done
        if [ -n "$SBX_DISK" ]; then
          # 已有 xfs 文件系统?幸存卷迁移场景 → 直接挂,保数据。空盘 → mkfs。
          if blkid "$SBX_DISK" 2>/dev/null | grep -q 'TYPE="xfs"'; then
            echo "[pre-bootstrap] state EBS $SBX_DISK has xfs, mounting (preserve data)"
          else
            echo "[pre-bootstrap] state EBS $SBX_DISK blank, mkfs.xfs"
            mkfs.xfs -f -m reflink=1 "$SBX_DISK" 2>/dev/null
          fi
          mount -o noatime "$SBX_DISK" /var/lib/sbx 2>/dev/null && \
            echo "[pre-bootstrap] state EBS $SBX_DISK -> /var/lib/sbx OK" || \
            echo "[pre-bootstrap] state EBS mount failed (non-fatal)"
        else
          echo "[pre-bootstrap] no state EBS found, /var/lib/sbx on root disk"
        fi

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

        # 命名 rootfs 模板(自定义镜像):从 min-rootfs 同目录拉 rootfs-{name}.tar.gz,
        # 造 /opt/sbx/rootfs-{name}.ext4。node-agent 按沙盒 image 选模板(见 _rootfs_template_path)。
        # 由 build-rootfs-image.sh 构建上传;未列出的 name 沙盒会回退默认 min,不影响启动。
        ROOTFS_PREFIX=$(dirname ${var.rootfs_s3_uri})   # s3://bucket/rootfs
        for IMG in $(echo "${var.rootfs_images}" | tr ',' ' '); do
          [ "$IMG" = "min" ] && continue   # min 即默认,上面已造
          aws s3 cp "$ROOTFS_PREFIX/rootfs-$IMG.tar.gz" /tmp/rootfs-$IMG.tar.gz --region ${var.region} 2>/dev/null && \
          dd if=/dev/zero of=/opt/sbx/rootfs-$IMG.ext4 bs=1M count=2048 status=none 2>/dev/null && \
          mkfs.ext4 /opt/sbx/rootfs-$IMG.ext4 -q 2>/dev/null && \
          mkdir -p /tmp/rmnt-$IMG && mount /opt/sbx/rootfs-$IMG.ext4 /tmp/rmnt-$IMG 2>/dev/null && \
          tar -xzf /tmp/rootfs-$IMG.tar.gz -C /tmp/rmnt-$IMG 2>/dev/null && \
          umount /tmp/rmnt-$IMG 2>/dev/null && \
          echo "[pre-bootstrap] rootfs template '$IMG' OK" || echo "[pre-bootstrap] rootfs template '$IMG' skipped (non-fatal)"
        done

        # Redis + JuiceFS 클라이언트 (설치 실패해도 계속)
        dnf install -y redis6 fuse3 2>/dev/null || true
        systemctl enable --now redis6 2>/dev/null || true
        curl -sSL https://d.juicefs.com/install | sh - 2>/dev/null || true

        # NAT
        sysctl -w net.ipv4.ip_forward=1 2>/dev/null || true

        echo "[pre-bootstrap] DONE $(date)"
      EOT
      }]

      # 本组承载 sandbox(裸 Firecracker microVM),打 sandbox=true 让 node-agent DaemonSet 调度上来。
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

# ---------- .metal ASG health check grace period 加长(防冷启动替换循环) ----------
# 根因:c6g.metal 裸金属过 EC2 status check 需 5-10 分钟,而 EKS 托管节点组建的 ASG
# 默认 grace period 仅 15s → 节点刚起就被判 unhealthy 替换 → 无限替换循环,节点永远
# 收敛不到全部 Ready(实测 07-07 重建时踩到,老 memory 误判为"暂态自愈")。
# EKS 托管节点组的 API/模块不暴露 ASG grace period,只能在节点组创建后 patch ASG。
resource "null_resource" "metal_asg_grace_period" {
  # 节点组变化(如换机型/架构)时重新 patch
  triggers = {
    asg_name = module.eks.eks_managed_node_groups["metal_${var.node_arch}"].node_group_autoscaling_group_names[0]
  }

  provisioner "local-exec" {
    command = <<-EOT
      aws autoscaling update-auto-scaling-group \
        --auto-scaling-group-name ${self.triggers.asg_name} \
        --health-check-grace-period 900 \
        --region ${var.region}
    EOT
  }
}

# ---------- ECR 仓库(直接创建,不依赖 phase1) ----------
resource "aws_ecr_repository" "sbx" {
  name                 = "claude-sbx"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
  # Project/ManagedBy 由 provider default_tags 统一注入
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
