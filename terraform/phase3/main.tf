# Phase 3 基础设施 —— EKS 集群 + system 节点组 + Firecracker 沙盒节点组 + 持久状态 EBS
#
# 目标:用 Terraform 管理 EKS 控制平面 + On-Demand Graviton system 节点组
#      + 沙盒数据节点组(打 sandbox=true label/taint)。
#       控制面 / node-agent / LiteLLM 等集群内资源由 stage2-control-plane 部署,不归此处。
#
# 架构:由 node_arch 变量控制 —— arm64(Graviton c6g.metal,默认) 或
#       amd64(Intel x86 R8i/M8i/C8i/I7i,显式开启 nested virtualization)。
#       terraform apply -var="node_arch=amd64"  # 切到 Intel x86
#
# ⚠️ 计费:EKS 控制平面 + 沙盒节点按小时计费。用完务必 destroy。
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
      version = ">= 6.0, < 7.0"
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
  description = "节点 CPU 架构:arm64(Graviton,默认) 或 amd64(Intel x86)。决定 AMI、默认实例、Firecracker/内核下载架构以及是否开启嵌套虚拟化。"
  validation {
    condition     = contains(["arm64", "amd64"], var.node_arch)
    error_message = "node_arch 仅支持 \"arm64\" 或 \"amd64\"。"
  }
}

variable "sandbox_instance_type" {
  type        = string
  default     = ""
  description = "Firecracker 沙盒节点实例类型。留空时 arm64=c6g.metal,amd64=r8i.8xlarge；x86 支持启用 nested virtualization 的 R8i/M8i/C8i/I7i。"
  validation {
    condition = (
      var.sandbox_instance_type == "" ||
      (var.node_arch == "arm64" && var.sandbox_instance_type == "c6g.metal") ||
      (
        var.node_arch == "amd64" &&
        can(regex("^(r8i|m8i|c8i|i7i)\\.", var.sandbox_instance_type))
      )
    )
    error_message = "arm64 当前仅支持 c6g.metal；amd64 实例必须属于 R8i/M8i/C8i/I7i 系列。"
  }
}

variable "sandbox_node_count" {
  type        = number
  default     = 1
  description = "沙盒节点常驻台数(min=max=desired)。成本优先默认 1 台；跨机快照/spot 疏散演示需设为 2。"
}

variable "system_instance_type" {
  type        = string
  default     = "m7g.large"
  description = "承载业务控制面、LiteLLM、Ingress 和集群系统 Pod 的 On-Demand Graviton 实例类型。"
  validation {
    condition     = can(regex("^(m|c|r)[0-9]+g[a-z]*\\.", var.system_instance_type))
    error_message = "system_instance_type 必须是 Graviton 实例，例如 m7g.large。"
  }
}

variable "system_node_count" {
  type        = number
  default     = 2
  description = "On-Demand system 节点数。POC/测试默认 2；生产建议 3 并跨 3 AZ。"
  validation {
    condition     = var.system_node_count >= 2
    error_message = "system_node_count 至少为 2，避免业务控制面与系统组件形成单节点故障域。"
  }
}

# M2:沙盒节点组计费类型。默认 ON_DEMAND(非破坏,行为不变)。
# 设 SPOT → 沙盒节点变抢占实例,node-agent 经 IMDS instance-life-cycle 自动上报 pool=spot,
# 控制面据此做受保护/抢占池放置(见 sandbox-api/app.py _placement_pool)。
# 注:要"受保护池 + 抢占池并存"需运行两个沙盒节点组(一 ON_DEMAND 一 SPOT);
# 当前受方案C(单 AZ 持久状态 EBS)约束,双节点组拓扑留作后续设计对齐项。
variable "sandbox_capacity_type" {
  type        = string
  default     = "ON_DEMAND"
  description = "沙盒节点组计费类型:ON_DEMAND(默认)或 SPOT。SPOT 时 node-agent 上报 pool=spot,控制面按池放置。"
  validation {
    condition     = contains(["ON_DEMAND", "SPOT"], var.sandbox_capacity_type)
    error_message = "sandbox_capacity_type 仅支持 \"ON_DEMAND\" 或 \"SPOT\"。"
  }
}

variable "sandbox_az_index" {
  type        = number
  default     = 0
  description = "沙盒节点所在单一可用区索引:0/1/2 分别对应 VPC 的 region-a/b/c。遇到实例容量不足时切换索引。"
  validation {
    condition     = contains([0, 1, 2], var.sandbox_az_index)
    error_message = "sandbox_az_index 仅支持 0、1 或 2。"
  }
}

variable "sandbox_ebs_bandwidth_weighting" {
  type        = string
  default     = "default"
  description = "实验项：支持该能力的 x86 实例使用的带宽权重。default 保持默认配比；ebs-1 在裸 EC2 有效，但 2026-09-02 实测 EKS Managed Node Group 最终实例仍为 default，生产必须检查实例实际值，不能只信 Terraform 配置。"
  validation {
    condition = contains(
      ["default", "ebs-1"],
      var.sandbox_ebs_bandwidth_weighting,
    )
    error_message = "sandbox_ebs_bandwidth_weighting 仅支持 default 或 ebs-1。"
  }
}

variable "recovery_standby_enabled" {
  type        = bool
  default     = false
  description = "是否创建同 AZ EBS 接管用的 On-Demand warm standby 节点组。默认关闭，避免改变现网成本。"
}

variable "recovery_standby_az_indices" {
  type        = set(number)
  default     = [0]
  description = "需要保留 warm standby 的 AZ 索引集合。生产三 AZ 可设 [0,1,2]；必须覆盖所有 Spot 数据节点所在 AZ。"
  validation {
    condition = alltrue([
      for index in var.recovery_standby_az_indices :
      contains([0, 1, 2], index)
    ])
    error_message = "recovery_standby_az_indices 仅支持 0、1、2。"
  }
}

variable "recovery_standby_count_per_az" {
  type        = number
  default     = 1
  description = "每个目标 AZ 常驻的空闲恢复节点数。"
  validation {
    condition     = var.recovery_standby_count_per_az >= 1
    error_message = "recovery_standby_count_per_az 至少为 1。"
  }
}

variable "recovery_max_claimed_hosts_per_az" {
  type        = number
  default     = 4
  description = "每个 standby 节点组允许同时承载的已接管主机数；为恢复后补充新 standby 预留 max_size。"
  validation {
    condition     = var.recovery_max_claimed_hosts_per_az >= 1
    error_message = "recovery_max_claimed_hosts_per_az 至少为 1。"
  }
}

locals {
  # 架构派生:AMI、默认实例、Firecracker/内核架构。
  arch_cfg = {
    arm64 = {
      ami_type         = "AL2023_ARM_64_STANDARD"
      default_instance = "c6g.metal"
      fc_arch          = "aarch64"
      rootfs_key       = "rootfs/rootfs-juicefs.tar.gz"
    }
    amd64 = {
      ami_type         = "AL2023_x86_64_STANDARD"
      default_instance = "r8i.8xlarge"
      fc_arch          = "x86_64"
      rootfs_key       = "rootfs/rootfs-juicefs-x86_64.tar.gz"
    }
  }
  node_arch_cfg         = local.arch_cfg[var.node_arch]
  sandbox_instance_type = var.sandbox_instance_type != "" ? var.sandbox_instance_type : local.node_arch_cfg.default_instance
  sandbox_network_performance_options = (
    var.node_arch == "amd64" &&
    var.sandbox_ebs_bandwidth_weighting != "default"
    ? {
      bandwidth_weighting = var.sandbox_ebs_bandwidth_weighting
    }
    : null
  )
  recovery_standby_az_indices = (
    var.recovery_standby_enabled
    ? var.recovery_standby_az_indices
    : toset([])
  )
}

variable "endpoint_public_access_cidrs" {
  type        = list(string)
  description = "允许访问 EKS 公网 API endpoint 的来源 CIDR(必填,无默认值以避免误开全网)。收窄到自己的 IP,apply 时传入:terraform apply -var='endpoint_public_access_cidrs=[\"'$(curl -s https://checkip.amazonaws.com)'/32\"]'"
}

# B2(FirecrackerDriver): 节点 userData 从此 S3 URI 拉取最小可启动 rootfs.tar.gz
variable "rootfs_s3_uri" {
  type        = string
  description = "S3 URI of the minimal bootable rootfs tarball matching node_arch (B2 FC mode)"
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
  validation {
    condition = (
      var.state_ebs_size_gb >= 1 &&
      var.state_ebs_size_gb <= 65536
    )
    error_message = "state_ebs_size_gb 必须在 gp3 支持的 1..65536 GiB。"
  }
}
variable "state_ebs_iops" {
  type        = number
  default     = 4000
  description = "状态 EBS 的 IOPS。gp3 支持 3000..80000；吞吐需满足 IOPS:MiB/s 至少 4:1。"
  validation {
    condition = (
      var.state_ebs_iops >= 3000 &&
      var.state_ebs_iops <= 80000
    )
    error_message = "state_ebs_iops 必须在 gp3 支持的 3000..80000。"
  }
}
variable "state_ebs_throughput" {
  type        = number
  default     = 1000
  description = "状态 EBS 吞吐(MiB/s)。当前 gp3 支持 125..2000；2000 MiB/s 至少需要 8000 IOPS。"
  validation {
    condition = (
      var.state_ebs_throughput >= 125 &&
      var.state_ebs_throughput <= 2000 &&
      var.state_ebs_throughput <= var.state_ebs_iops / 4
    )
    error_message = "state_ebs_throughput 必须在 125..2000 MiB/s，且不能超过 state_ebs_iops / 4。"
  }
}

# ---------- VPC(EKS 专用,3 AZ) ----------
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.0"

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

# ---------- EKS 集群 + Firecracker 沙盒节点组 ----------
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.0"

  name               = var.cluster_name
  kubernetes_version = "1.31"

  # EKS module v21 disables legacy self-managed add-on bootstrap. Declare the
  # managed add-ons explicitly so a new node has CNI before it joins.
  addons = {
    coredns    = {}
    kube-proxy = {}
    vpc-cni = {
      before_compute = true
    }
  }

  endpoint_public_access = true
  # 收窄到指定 CIDR;留空时模块默认 0.0.0.0/0(对全网开放)——生产/共享账号务必传入自己的 IP。
  endpoint_public_access_cidrs             = var.endpoint_public_access_cidrs
  enable_cluster_creator_admin_permissions = true

  vpc_id = module.vpc.vpc_id
  # 控制平面 ENI 放私有子网;节点组单独指定公有子网(见 node group subnet_ids)
  subnet_ids = module.vpc.private_subnets

  # system 与 sandbox 数据面分组:
  # - system_arm64:固定 On-Demand Graviton，承载业务控制面和集群系统 Pod。
  # - sandbox_*:只承载 node-agent + Firecracker microVM，带 NoSchedule 污点。
  eks_managed_node_groups = merge({
    system_arm64 = {
      kubernetes_version = "1.31"
      ami_type           = "AL2023_ARM_64_STANDARD"
      instance_types     = [var.system_instance_type]
      capacity_type      = "ON_DEMAND"

      min_size     = var.system_node_count
      max_size     = var.system_node_count
      desired_size = var.system_node_count

      # POC VPC 没有 NAT，system 节点暂放三个公有子网；生产应改私有子网 + VPC endpoints/NAT。
      subnet_ids = module.vpc.public_subnets

      labels = {
        role            = "system"
        "workload-tier" = "system"
      }

      block_device_mappings = {
        xvda = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size = 50
            volume_type = "gp3"
          }
        }
      }
    }

    "sandbox_${var.node_arch}" = {
      kubernetes_version = "1.31"
      ami_type           = local.node_arch_cfg.ami_type
      instance_types     = [local.sandbox_instance_type]
      # M2:默认 ON_DEMAND(非破坏);设 var.sandbox_capacity_type=SPOT 让沙盒池变抢占实例。
      capacity_type = var.sandbox_capacity_type

      # Intel R8i/M8i/C8i/I7i 虚拟实例必须显式打开 Nitro nested
      # virtualization 才会暴露 /dev/kvm。
      # c6g.metal 直接使用宿主 KVM，不设置该选项。
      cpu_options = var.node_arch == "amd64" ? {
        nested_virtualization = "enabled"
      } : {}

      network_performance_options = local.sandbox_network_performance_options

      # Firecracker 跨机快照演示需两台常驻(min=2);x86/arm64 由 node_arch 参数化。
      # 成本优先的单机 demo:降到 1 台(单机可测 create/exec/suspend/resume/destroy 全流程,
      # 仅跨机快照/spot 疏散演示需要 2 台)。由 sandbox_node_count 变量控制,默认 1。
      min_size     = var.sandbox_node_count
      max_size     = var.sandbox_node_count
      desired_size = var.sandbox_node_count

      # 方案C:两台节点必须【同一 AZ】—— EBS 状态卷不能跨 AZ attach。
      # 钉死到 sandbox_az_index 指定的单个 AZ,否则 EKS 会把两台分散到不同 AZ,
      # 导致 spot 疏散后状态卷无法 attach 到另一 AZ 的新节点。默认 0=region-a;
      # 若所选 i7i 规格暂时无容量,可切到 1=region-b 或 2=region-c。
      subnet_ids = [module.vpc.public_subnets[var.sandbox_az_index]]

      # B2: 节点 userData 需从 S3 拉 rootfs.tar.gz。
      iam_role_additional_policies = {
        s3_readonly = "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"
      }

      block_device_mappings = {
        xvda = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size = 200
            volume_type = "gp3"
          }
        }
        # 方案C:独立状态 EBS 挂 /var/lib/sbx,存快照与 rootfs。
        # 默认随普通终止删除，避免 bootstrap/健康检查失败留下孤儿卷；收到
        # Spot 回收信号后 node-agent 会在 checkpoint 前原子改成 false。
        sbxdata = {
          device_name = "/dev/sdf"
          ebs = {
            volume_size           = var.state_ebs_size_gb
            volume_type           = "gp3"
            iops                  = var.state_ebs_iops
            throughput            = var.state_ebs_throughput
            delete_on_termination = true
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
        # 正常情况下随实例删除；Spot checkpoint 开始时 node-agent 会先把
        # 当前 attachment 改成 delete_on_termination=false。
        # 识别:只选择 Amazon EBS,避免 i7i 上把本地 NVMe instance store 误当状态盘格式化。
        # EBS NVMe 盘的 MODEL 为 "Amazon Elastic Block Store"。首次为空盘 → mkfs;
        # 已有文件系统(从旧节点迁移来的幸存卷)→ 直接挂,不格式化(否则抹掉数据!)。
        SBX_DISK=""
        for dev in /dev/nvme*n1 /dev/sd[b-z] /dev/xvd[b-z]; do
          [ -b "$dev" ] || continue
          model=$(lsblk -ndo MODEL "$dev" 2>/dev/null | xargs)
          case "$model" in
            *"Elastic Block Store"*) ;;
            *) continue ;;
          esac
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
          # cloud-init shell parts may run in a transient mount namespace on AL2023.
          # Ask PID 1 to own the mount so it persists after this script exits.
          SBX_UUID=$(blkid -s UUID -o value "$SBX_DISK")
          cat >/etc/systemd/system/var-lib-sbx.mount <<UNIT
        [Unit]
        Description=Sandbox persistent state EBS
        Before=kubelet.service

        [Mount]
        What=UUID=$${SBX_UUID}
        Where=/var/lib/sbx
        Type=xfs
        Options=noatime

        [Install]
        WantedBy=multi-user.target
        UNIT
          systemctl daemon-reload
          systemctl enable --now var-lib-sbx.mount 2>/dev/null && \
            echo "[pre-bootstrap] state EBS $SBX_DISK -> /var/lib/sbx OK (systemd)" || \
            echo "[pre-bootstrap] state EBS systemd mount failed (non-fatal)"
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

        touch /opt/sbx/.bootstrap-complete
        echo "[pre-bootstrap] DONE $(date)"
      EOT
      }]

      # 本组只承载 node-agent + 裸 Firecracker microVM。NoSchedule 防止控制面、
      # CoreDNS、LiteLLM 等普通 Pod 在数据节点上落盘。
      labels = {
        role            = "sandbox"
        sandbox         = "true"
        "workload-tier" = "data"
      }
      taints = {
        dedicated_sandbox = {
          key    = "dedicated"
          value  = "sandbox"
          effect = "NO_SCHEDULE"
        }
      }
    }
    }, {
    for az_index in local.recovery_standby_az_indices :
    "sandbox_standby_${az_index}" => {
      name               = "${var.cluster_name}-recovery-${az_index}"
      use_name_prefix    = false
      kubernetes_version = "1.31"
      ami_type           = local.node_arch_cfg.ami_type
      instance_types     = [local.sandbox_instance_type]
      capacity_type      = "ON_DEMAND"

      cpu_options = var.node_arch == "amd64" ? {
        nested_virtualization = "enabled"
      } : {}

      network_performance_options = local.sandbox_network_performance_options

      min_size = var.recovery_standby_count_per_az
      max_size = (
        var.recovery_standby_count_per_az +
        var.recovery_max_claimed_hosts_per_az
      )
      desired_size = var.recovery_standby_count_per_az
      subnet_ids   = [module.vpc.public_subnets[az_index]]

      iam_role_additional_policies = {
        s3_readonly = "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"
      }

      # Standby 只有根盘；被认领后由恢复控制器把旧节点幸存的状态 EBS
      # attach 过来，node-agent 再挂载到 /var/lib/sbx。
      block_device_mappings = {
        xvda = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size = 200
            volume_type = "gp3"
          }
        }
      }

      cloudinit_pre_nodeadm = [{
        content_type = "text/x-shellscript; charset=\"us-ascii\""
        content      = <<-EOT
        #!/bin/bash
        set -u
        exec >> /var/log/userdata-recovery-standby.log 2>&1
        echo "[recovery-standby] START $(date)"

        mkdir -p /opt/sbx /var/lib/sbx

        ARCH=${local.node_arch_cfg.fc_arch}
        VER=$(curl -sf https://api.github.com/repos/firecracker-microvm/firecracker/releases/latest \
          | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])" 2>/dev/null || echo "v1.16.0")
        curl -sfL "https://github.com/firecracker-microvm/firecracker/releases/download/$${VER}/firecracker-$${VER}-$${ARCH}.tgz" \
          -o /tmp/fc.tgz 2>/dev/null && \
        tar -xzf /tmp/fc.tgz -C /tmp 2>/dev/null && \
        mv "/tmp/release-$${VER}-$${ARCH}/firecracker-$${VER}-$${ARCH}" /usr/local/bin/firecracker 2>/dev/null && \
        chmod +x /usr/local/bin/firecracker || true

        curl -sfL "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.10/${local.node_arch_cfg.fc_arch}/vmlinux-5.10.223" \
          -o /opt/sbx/vmlinux 2>/dev/null || true

        aws s3 cp ${var.rootfs_s3_uri} \
          /tmp/rootfs.tar.gz --region ${var.region} 2>/dev/null && \
        dd if=/dev/zero of=/opt/sbx/rootfs.ext4 bs=1M count=2048 status=none 2>/dev/null && \
        mkfs.ext4 /opt/sbx/rootfs.ext4 -q 2>/dev/null && \
        mkdir -p /tmp/rootfs_mount && \
        mount /opt/sbx/rootfs.ext4 /tmp/rootfs_mount 2>/dev/null && \
        tar -xzf /tmp/rootfs.tar.gz -C /tmp/rootfs_mount 2>/dev/null && \
        umount /tmp/rootfs_mount 2>/dev/null || true

        ROOTFS_PREFIX=$(dirname ${var.rootfs_s3_uri})
        for IMG in $(echo "${var.rootfs_images}" | tr ',' ' '); do
          [ "$IMG" = "min" ] && continue
          aws s3 cp "$ROOTFS_PREFIX/rootfs-$IMG.tar.gz" /tmp/rootfs-$IMG.tar.gz --region ${var.region} 2>/dev/null && \
          dd if=/dev/zero of=/opt/sbx/rootfs-$IMG.ext4 bs=1M count=2048 status=none 2>/dev/null && \
          mkfs.ext4 /opt/sbx/rootfs-$IMG.ext4 -q 2>/dev/null && \
          mkdir -p /tmp/rmnt-$IMG && mount /opt/sbx/rootfs-$IMG.ext4 /tmp/rmnt-$IMG 2>/dev/null && \
          tar -xzf /tmp/rootfs-$IMG.tar.gz -C /tmp/rmnt-$IMG 2>/dev/null && \
          umount /tmp/rmnt-$IMG 2>/dev/null || true
        done

        dnf install -y redis6 fuse3 2>/dev/null || true
        systemctl enable --now redis6 2>/dev/null || true
        curl -sSL https://d.juicefs.com/install | sh - 2>/dev/null || true
        sysctl -w net.ipv4.ip_forward=1 2>/dev/null || true
        touch /opt/sbx/.bootstrap-complete
        echo "[recovery-standby] DONE $(date)"
      EOT
      }]

      labels = {
        role                                    = "sandbox"
        sandbox                                 = "true"
        "workload-tier"                         = "data"
        "sandbox.memorion.ai/recovery-role"     = "standby"
        "sandbox.memorion.ai/recovery-group"    = "${var.cluster_name}-recovery-${az_index}"
        "sandbox.memorion.ai/recovery-az-index" = tostring(az_index)
      }
      taints = {
        dedicated_sandbox = {
          key    = "dedicated"
          value  = "sandbox"
          effect = "NO_SCHEDULE"
        }
      }
    }
  })

  # 节点角色加 Bedrock 调用权限(沙盒走节点凭据链调 Bedrock;生产改 IRSA/出口代理)
  # 保留节点安全组的集群标签，供 Karpenter 安全组选择器使用
  node_security_group_tags = {
    "kubernetes.io/cluster/${var.cluster_name}" = "owned"
  }
}

# Bedrock 权限已迁移到 LiteLLM IRSA(terraform/stage2-control-plane/litellm.tf)
# 节点角色不再持有 Bedrock 权限 —— 沙盒内代码无法直接调 Bedrock(R8 凭据隔离落地)
# 沙盒走: Claude Code → ANTHROPIC_BASE_URL=http://litellm.litellm:4000 → LiteLLM Pod → Bedrock

# ---------- 沙盒 ASG health check grace period 加长(防冷启动替换循环) ----------
# 根因:c6g.metal 裸金属过 EC2 status check 需 5-10 分钟,而 EKS 托管节点组建的 ASG
# 默认 grace period 仅 15s → 节点刚起就被判 unhealthy 替换 → 无限替换循环,节点永远
# 收敛不到全部 Ready(实测 07-07 重建时踩到,老 memory 误判为"暂态自愈")。
# EKS 托管节点组的 API/模块不暴露 ASG grace period,只能在节点组创建后 patch ASG。
resource "null_resource" "sandbox_asg_grace_period" {
  # 节点组变化(如换机型/架构)时重新 patch
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

output "system_node_group_name" {
  value = module.eks.eks_managed_node_groups["system_arm64"].node_group_id
}

output "sandbox_node_group_name" {
  value = module.eks.eks_managed_node_groups["sandbox_${var.node_arch}"].node_group_id
}

output "recovery_standby_node_group_names" {
  value = {
    for az_index in local.recovery_standby_az_indices :
    tostring(az_index) => module.eks.eks_managed_node_groups[
      "sandbox_standby_${az_index}"
    ].node_group_id
  }
}
