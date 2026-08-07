# Phase 3 基础设施 —— EKS 集群 + 承载沙盒的托管节点组(裸 Firecracker microVM 节点)
#
# 目标:用 Terraform 管理 EKS 控制平面 + 一个承载 sandbox 的托管节点组(打 sandbox=true label)。
#       控制面 / node-agent / LiteLLM 等集群内资源由 stage2-control-plane 部署,不归此处。
#
# 【默认机型:i7i(虚拟化实例 + 嵌套虚拟化)】
#   过去用 .metal 裸金属跑 Firecracker;现默认改用 i7i —— 虚拟化 Intel 实例,通过【嵌套虚拟化】
#   (CpuOptions.NestedVirtualization=enabled,需 AWS provider ≥6.33 + EKS 模块 v21)向 guest 暴露
#   /dev/kvm 来跑 Firecracker microVM。相比 .metal:启动更快(1-2 分钟 vs 5-10 分钟)、更便宜、
#   带本地 NVMe 实例存储(快照可落本地盘,再走 S3 作为权威副本)。
#
#   仍想用 .metal(方案C:持久 EBS 存快照、spot 疏散跨机恢复)?切三个变量即可:
#     -var="instance_type=c6g.metal" -var="enable_nested_virtualization=false" -var="use_instance_store=false"
#   (.metal 是裸金属,天然有 KVM,不能也不需要设 NestedVirtualization。)
#
# 架构:由 node_arch 变量控制 —— amd64(Intel x86,i7i 默认) 或 arm64(Graviton,仅 .metal 用)。
#       i7i 是 x86,嵌套虚拟化仅 Intel 支持,故默认 node_arch=amd64。
#
# ⚠️ 计费:EKS 控制平面 $0.10/hr + 节点(i7i.4xlarge≈$1.5/hr)。用完务必 destroy。
#
# 用法:
#   terraform init   # 首次会拉 AWS provider 6.x + EKS 模块 v21 + VPC 模块 v6
#   terraform apply -var='endpoint_public_access_cidrs=["'$(curl -s https://checkip.amazonaws.com)'/32"]' \
#     -var="rootfs_s3_uri=s3://<bucket>/rootfs/min-rootfs.tar.gz"
#   aws eks update-kubeconfig --name claude-sbx --region us-east-1
#   kubectl get nodes
#
# 销毁:terraform destroy

terraform {
  required_version = ">= 1.5.7" # EKS 模块 v21 要求
  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 需 ≥6.33 才有 aws_launch_template.cpu_options.nested_virtualization;
      # EKS 模块 v21 自身要求 ≥6.52。锁 6.x 大版本。
      version = "~> 6.52"
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
  default     = "amd64" # i7i 是 x86;嵌套虚拟化仅 Intel 支持。用 .metal Graviton 时传 arm64。
  description = "节点 CPU 架构:amd64(Intel x86,i7i 默认) 或 arm64(Graviton,仅 .metal)。决定 AMI 类型、Firecracker/内核下载架构。"
  validation {
    condition     = contains(["arm64", "amd64"], var.node_arch)
    error_message = "node_arch 仅支持 \"arm64\" 或 \"amd64\"。"
  }
}

variable "instance_type" {
  type        = string
  default     = "i7i.4xlarge" # 虚拟化 Intel 实例,支持嵌套虚拟化跑 Firecracker;带本地 NVMe 实例存储。
  description = "沙盒承载节点的实例类型。默认 i7i.4xlarge(嵌套虚拟化 + 本地 NVMe)。用 .metal 裸金属请传 c6g.metal / c5n.metal 等,并同时设 enable_nested_virtualization=false、use_instance_store=false。"
}

variable "enable_nested_virtualization" {
  type        = bool
  default     = true
  description = "是否给节点开启嵌套虚拟化(CpuOptions.NestedVirtualization=enabled)。i7i 等虚拟化实例跑 Firecracker【必须】开启;.metal 裸金属天然有 KVM,必须设为 false(裸金属不接受该 CPU 选项)。"
}

variable "use_instance_store" {
  type        = bool
  default     = true
  description = "true(i7i 默认):把节点本地 NVMe 实例存储挂到 /var/lib/sbx 作沙盒快照/rootfs 暂存盘(临时,随实例销毁,快照权威副本走 S3)。false(.metal 方案C):改挂一块持久 EBS(delete_on_termination=false),spot 终止后幸存、可跨机恢复。"
}

variable "node_count" {
  type        = number
  default     = 1
  description = "承载节点常驻台数(min=max=desired)。成本优先默认 1 台(单机可测全生命周期);跨机快照/spot 疏散演示需设为 2。"
}

locals {
  # 架构派生:AMI 类型、Firecracker/内核下载用的架构标识(uname -m 风格)
  arch_cfg = {
    arm64 = {
      ami_type = "AL2023_ARM_64_STANDARD"
      fc_arch  = "aarch64" # Firecracker 发行包 / CI vmlinux 的架构后缀
    }
    amd64 = {
      ami_type = "AL2023_x86_64_STANDARD"
      fc_arch  = "x86_64"
    }
  }
  node_arch_cfg = local.arch_cfg[var.node_arch]
}

variable "endpoint_public_access_cidrs" {
  type        = list(string)
  description = "允许访问 EKS 公网 API endpoint 的来源 CIDR(必填,无默认值以避免误开全网)。收窄到自己的 IP,apply 时传入:terraform apply -var='endpoint_public_access_cidrs=[\"'$(curl -s https://checkip.amazonaws.com)'/32\"]'"
}

# B2(FirecrackerDriver): 节点 userData 从此 S3 URI 拉取最小可启动 rootfs.tar.gz
variable "rootfs_s3_uri" {
  type        = string
  description = "S3 URI of the minimal bootable rootfs tarball (B2 FC mode)。i7i(amd64)须用 x86_64 构建的 rootfs(build-min-rootfs.sh 在 x86 机器上跑,或 ARCH=amd64 build-rootfs-image.sh)。"
  default     = ""
}

# 自定义镜像:额外的命名 rootfs 模板(逗号分隔 name 列表)。节点从 rootfs_s3_uri 同目录拉
# rootfs-{name}.tar.gz 造 /opt/sbx/rootfs-{name}.ext4。用 build-rootfs-image.sh <name> 构建上传。
# 默认含 web(自带 demo 站点)+ openclaw(预装 Node+OpenClaw,开机自起 Gateway :18789)。
variable "rootfs_images" {
  type        = string
  default     = "web,openclaw"
  description = "逗号分隔的命名 rootfs 模板列表(除 min 外),节点会各拉一份造 ext4 模板。openclaw=预装 Node+OpenClaw 的会话式 AI Agent 基础镜像(开机自起 Gateway :18789)。i7i 上须用 ARCH=amd64 构建这些模板。"
}

# ---------- 方案C(仅 use_instance_store=false / .metal 时生效):持久状态 EBS ----------
variable "state_ebs_size_gb" {
  type        = number
  default     = 400
  description = "(仅 .metal 方案C)每节点持久状态 EBS 容量(GB)。resume 时每 sandbox 峰值需 base(2G)+merged(2G)≈4G。"
}
variable "state_ebs_iops" {
  type        = number
  default     = 4000
  description = "(仅 .metal 方案C)状态 EBS 的 IOPS(gp3,1000MB/s 吞吐至少需 4000 IOPS)。"
}
variable "state_ebs_throughput" {
  type        = number
  default     = 1000
  description = "(仅 .metal 方案C)状态 EBS 吞吐(MB/s)。1000=gp3 单卷上限。"
}

# ---------- VPC(EKS 专用,3 AZ) ----------
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.0" # 与 AWS provider 6.x 对齐(v5 卡 <6.0.0)

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

# ---------- EKS 集群 + 承载沙盒的托管节点组 ----------
# 注意:EKS 模块 v21 起,集群级变量去掉了 cluster_ 前缀(name / kubernetes_version /
#       endpoint_public_access* 等),且要求 AWS provider ≥6.0。见 docs/UPGRADE-21.0。
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.0"

  name               = var.cluster_name # v20: cluster_name
  kubernetes_version = "1.31"           # v20: cluster_version

  endpoint_public_access = true # v20: cluster_endpoint_public_access
  # 收窄到指定 CIDR;生产/共享账号务必传入自己的 IP。
  endpoint_public_access_cidrs             = var.endpoint_public_access_cidrs # v20: cluster_endpoint_public_access_cidrs
  enable_cluster_creator_admin_permissions = true

  vpc_id = module.vpc.vpc_id
  # 控制平面 ENI 放私有子网;节点组单独指定公有子网(见 node group subnet_ids)
  subnet_ids = module.vpc.private_subnets

  # 承载 sandbox 的托管节点组(裸 Firecracker microVM),打 sandbox=true 让 node-agent DaemonSet 调度上来。
  eks_managed_node_groups = {
    "sandbox_${var.node_arch}" = {
      ami_type       = local.node_arch_cfg.ami_type
      instance_types = [var.instance_type]

      # 成本优先单机 demo 默认 1 台;跨机快照/spot 疏散演示需 2 台(node_count 变量)。
      min_size     = var.node_count
      max_size     = var.node_count
      desired_size = var.node_count

      # 节点放公有子网直接出网(无 NAT)。
      # ⚠️ .metal 方案C(持久 EBS)要求同一 AZ(EBS 不能跨 AZ attach),故钉死到 public_subnets[0]。
      #    i7i + 本地 NVMe 无此约束,但保持单 AZ 简化(多台时同 AZ,便于演示)。
      subnet_ids = [module.vpc.public_subnets[0]]

      # B2: 节点 userData 需从 S3 拉 rootfs.tar.gz → S3 只读;SSM 供排障。
      iam_role_additional_policies = {
        s3_readonly = "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"
        ssm_core    = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
      }

      # 【i7i 关键】嵌套虚拟化:向 guest 暴露 /dev/kvm 才能跑 Firecracker。
      #   需 provider ≥6.33 + EKS 模块 v21。.metal 裸金属设 enable_nested_virtualization=false(此处置 null)。
      cpu_options = var.enable_nested_virtualization ? { nested_virtualization = "enabled" } : null

      # v21 IMDS 默认 hop_limit=1(v20 是 2)。node-agent 走 IRSA 不依赖 IMDS,但保持 2 以贴合旧行为、
      # 避免 host/pod 访问 IMDS 的意外(略放宽,已知取舍)。
      metadata_options = {
        http_endpoint               = "enabled"
        http_tokens                 = "required"
        http_put_response_hop_limit = 2
      }

      # 存储盘:
      #   use_instance_store=true(i7i 默认):仅根盘;沙盒暂存盘用本地 NVMe 实例存储
      #     (Nitro 自动挂载为 /dev/nvme1n1,下方 pre-bootstrap 探测并 mkfs+挂到 /var/lib/sbx)。
      #     临时盘,随实例销毁;快照权威副本走 S3。
      #   use_instance_store=false(.metal 方案C):额外挂一块持久 EBS(delete_on_termination=false),
      #     存快照+rootfs,spot 终止后幸存、可跨机恢复。
      block_device_mappings = merge(
        {
          xvda = {
            device_name = "/dev/xvda"
            ebs = {
              volume_size = 200
              volume_type = "gp3"
            }
          }
        },
        var.use_instance_store ? {} : {
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
      )

      # AL2023(nodeadm)下用 cloudinit_pre_nodeadm 注入 shell 脚本,在 nodeadm 引导前执行。
      # Firecracker 二进制 + 内核 + rootfs 预装,不重启 containerd → 不触发节点替换循环。
      cloudinit_pre_nodeadm = [{
        content_type = "text/x-shellscript; charset=\"us-ascii\""
        content      = <<-EOT
        #!/bin/bash
        # pre_bootstrap: kubelet 启动前执行
        # ⚠️ 禁止长任务(docker/dnf 大安装 → kubelet 心跳中断 → 节点替换循环)
        # 只装:Firecracker 二进制 + 内核 + rootfs + 挂沙盒暂存盘
        exec >> /var/log/userdata-pre.log 2>&1
        echo "[pre-bootstrap] START $(date)"

        mkdir -p /opt/sbx /var/lib/sbx

        # 沙盒暂存盘挂到 /var/lib/sbx —— sandbox 快照(base+diff)+ rootfs 落这块盘。
        #   i7i:本地 NVMe 实例存储(临时,随实例销毁;快照权威副本在 S3)。
        #   .metal 方案C:持久 EBS(delete_on_termination=false,spot 终止后幸存,可 attach 到新机恢复)。
        # 识别:非根盘、无分区表、无挂载点的块设备(附加数据卷 / 实例存储)。空盘 → mkfs;
        # 已有 xfs 文件系统(方案C 幸存卷迁移来)→ 直接挂,不格式化(否则抹掉数据!)。
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
          # 已有 xfs?方案C 幸存卷迁移场景 → 直接挂,保数据。空盘(含 i7i 实例存储)→ mkfs。
          if blkid "$SBX_DISK" 2>/dev/null | grep -q 'TYPE="xfs"'; then
            echo "[pre-bootstrap] sbx disk $SBX_DISK has xfs, mounting (preserve data)"
          else
            echo "[pre-bootstrap] sbx disk $SBX_DISK blank, mkfs.xfs"
            mkfs.xfs -f -m reflink=1 "$SBX_DISK" 2>/dev/null
          fi
          mount -o noatime "$SBX_DISK" /var/lib/sbx 2>/dev/null && \
            echo "[pre-bootstrap] sbx disk $SBX_DISK -> /var/lib/sbx OK" || \
            echo "[pre-bootstrap] sbx disk mount failed (non-fatal)"
        else
          echo "[pre-bootstrap] no dedicated sbx disk found, /var/lib/sbx on root disk"
        fi

        # Firecracker (二进制,快) —— 架构由 Terraform node_arch 注入
        ARCH=${local.node_arch_cfg.fc_arch}
        VER=$(curl -sf https://api.github.com/repos/firecracker-microvm/firecracker/releases/latest \
          | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])" 2>/dev/null || echo "v1.16.0")
        curl -sfL "https://github.com/firecracker-microvm/firecracker/releases/download/$${VER}/firecracker-$${VER}-$${ARCH}.tgz" \
          -o /tmp/fc.tgz 2>/dev/null && \
        tar -xzf /tmp/fc.tgz -C /tmp 2>/dev/null && \
        mv "/tmp/release-$${VER}-$${ARCH}/firecracker-$${VER}-$${ARCH}" /usr/local/bin/firecracker 2>/dev/null && \
        chmod +x /usr/local/bin/firecracker && \
        echo "[pre-bootstrap] Firecracker OK" || echo "[pre-bootstrap] Firecracker install failed (non-fatal)"

        # 内核 (16MB, 快) —— 架构由 Terraform node_arch 注入
        curl -sfL "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.10/${local.node_arch_cfg.fc_arch}/vmlinux-5.10.223" \
          -o /opt/sbx/vmlinux 2>/dev/null && echo "[pre-bootstrap] Kernel OK" || true

        # rootfs: S3 下载 tar.gz → 造 ext4(最小可启动 rootfs,含 vsock-exec-agent)
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
        # 造 /opt/sbx/rootfs-{name}.ext4。node-agent 按沙盒 image 选模板。
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

        # Redis + JuiceFS 客户端 (装失败也继续)
        dnf install -y redis6 fuse3 2>/dev/null || true
        systemctl enable --now redis6 2>/dev/null || true
        curl -sSL https://d.juicefs.com/install | sh - 2>/dev/null || true

        # NAT
        sysctl -w net.ipv4.ip_forward=1 2>/dev/null || true

        echo "[pre-bootstrap] DONE $(date)"
      EOT
      }]

      # 本组承载 sandbox,打 sandbox=true 让 node-agent DaemonSet 调度上来。
      labels = {
        role    = "system"
        sandbox = "true"
      }
    }
  }
}

# Bedrock 权限已迁移到 LiteLLM IRSA(terraform/stage2-control-plane/litellm.tf)
# 沙盒走: Claude Code → ANTHROPIC_BASE_URL=http://litellm.litellm:4000 → LiteLLM Pod → Bedrock

# ---------- ASG health check grace period 加长(防冷启动替换循环) ----------
# 主要针对 .metal(过 EC2 status check 需 5-10 分钟,而 EKS 建的 ASG 默认 grace 仅 15s → 无限替换)。
# i7i 是虚拟化实例,1-2 分钟即就绪,不需要这么长;但设长无害,统一保留。
resource "null_resource" "asg_grace_period" {
  triggers = {
    asg_name = module.eks.eks_managed_node_groups["sandbox_${var.node_arch}"].node_group_autoscaling_group_names[0]
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
