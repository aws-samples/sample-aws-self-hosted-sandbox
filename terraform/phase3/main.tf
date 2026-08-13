# Phase 3 基础设施 —— EKS 集群 + system 节点组 + Firecracker 沙盒节点组 + 持久状态 EBS
#
# 目标:用 Terraform 管理 EKS 控制平面 + On-Demand Graviton system 节点组
#      + 沙盒数据节点组(打 sandbox=true label/taint)。
#       控制面 / node-agent / LiteLLM 等集群内资源由 stage2-control-plane 部署,不归此处。
#
# 节点组:
#   system_arm64          控制面/系统 Pod,固定 On-Demand Graviton,不受数据面变量影响
#   sandbox_<arch>        数据面 On-Demand 池(sandbox_od_node_count,默认 1)
#   sandbox_<arch>_spot   数据面 Spot 池(sandbox_spot_node_count,默认 0 不创建)
#   两个数据面池同 AZ、同 AMI、同 cloudinit、同持久状态 EBS,只差 capacity_type/机型/台数。
#   可选 ASG 预热池(sandbox_warm_pool_size)缩短节点替换时间,天然单 AZ 亲和。
#
# 架构:由 node_arch 变量控制 —— amd64(Intel x86 r8i.8xlarge,默认/推荐,显式开启
#       nested virtualization) 或 arm64(Graviton c6g.metal,备选)。
#       terraform apply                        # 默认即 Intel x86 r8i.8xlarge
#       terraform apply -var="node_arch=arm64" # 切回 Graviton 裸金属(备选路线)
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
  default     = "amd64"
  description = "沙盒数据节点 CPU 架构:amd64(Intel x86,默认/推荐) 或 arm64(Graviton,备选)。决定 AMI、默认实例、Firecracker/内核下载架构以及是否开启嵌套虚拟化。system 节点始终为 arm64,不受此变量影响。"
  validation {
    condition     = contains(["arm64", "amd64"], var.node_arch)
    error_message = "node_arch 仅支持 \"arm64\" 或 \"amd64\"。"
  }
}

variable "sandbox_instance_type" {
  type        = string
  default     = ""
  description = <<-EOT
    Firecracker 沙盒节点实例类型。留空时 amd64(默认)=r8i.8xlarge,arm64=c6g.metal。
    x86 可覆盖为任意 r8i.* 或 i7i.* 规格:
      - r8i.*(推荐,默认):无本地 NVMe,状态全落持久 EBS,同配置比 i7i 便宜。
      - i7i.*:带本地 NVMe instance store,但 spot 回收/停机即销毁,存不了状态;
        保留仅为复现既有 i7i 真机报告。
    规格建议不低于 8xlarge:更小规格的 EBS 吞吐受突发积分限制,只能短时冲峰,
    无法持续跑满,会拖长 spot 回收窗口内的疏散落盘(见 terraform/README.md)。
  EOT
  validation {
    condition = (
      var.sandbox_instance_type == "" ||
      (var.node_arch == "arm64" && var.sandbox_instance_type == "c6g.metal") ||
      (var.node_arch == "amd64" && can(regex("^(r8i|i7i)\\.", var.sandbox_instance_type)))
    )
    error_message = "arm64 当前仅支持 c6g.metal；amd64 实例必须属于 r8i 系列(推荐,例如 r8i.8xlarge)或 i7i 系列。"
  }
}

# ---------- 数据面双池:On-Demand 组 + Spot 组(system 控制面组不受影响) ----------
# 两个组共用同一份配置基底(同 AZ、同 AMI、同 cloudinit、同持久状态 EBS、同 taint/label),
# 只有 capacity_type / 实例列表 / 台数不同。控制面仍固定跑在 system_arm64(ON_DEMAND)上。
variable "sandbox_od_node_count" {
  type        = number
  default     = 1
  description = "On-Demand 沙盒节点常驻台数(min=max=desired)。承载不可中断的大客户业务,也是 spot 疏散的接收方。设 0 则不建 OD 数据面组。"
}

variable "sandbox_spot_node_count" {
  type        = number
  default     = 0
  description = "Spot 沙盒节点常驻台数。默认 0(不建 Spot 组);开启前请确认疏散链路已闭环(见 RECLAIM_AUTO_EVACUATE)。🔴 2026-08-13 真机:控制面 _pick_node() 只按 free_mem_mib 排序、不看 capacity-type label,实测 5/5 沙盒全落 OD 节点 —— 现在开 Spot 组不会被调度到,只是白付钱。"
}

variable "sandbox_spot_instance_types" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Spot 组的候选实例类型(多写几个能提高 Spot 容量命中率)。留空则复用与 OD 组相同的
    sandbox_instance_type。所有候选必须同架构且支持嵌套虚拟化:amd64 用 r8i.*/i7i.*,
    arm64 只有 c6g.metal。⚠️ 同 AZ 亲和是硬约束(持久状态 EBS 不能跨 AZ attach),
    所以只做机型分散、不做 AZ 分散。
  EOT
  validation {
    condition = alltrue([
      for t in var.sandbox_spot_instance_types :
      can(regex("^(r8i|i7i)\\.", t)) || t == "c6g.metal"
    ])
    error_message = "sandbox_spot_instance_types 只能包含 r8i.* / i7i.*(amd64)或 c6g.metal(arm64)。"
  }
}

# ---------- 单 AZ 亲和的节点预热池(ASG warm pool) ----------
variable "sandbox_warm_pool_size" {
  type        = number
  default     = 0
  description = <<-EOT
    沙盒数据面【节点】预热池台数。预热实例先启动一遍再进入 Stopped,ASG 需要替换节点时直接
    start,跳过大部分开机初始化。0=关闭。
    2026-08-13 真机实测:terminate 一台数据节点 → 新节点 Ready 用 44s(冷启动基线 5m03s),
    服务恢复 SLO ≤5min 达成。
    预热池继承 ASG 的子网,而 ASG 已钉死在 sandbox_az_index 单个 AZ → 天然单 AZ 亲和,
    预热出来的机器一定能 attach 同 AZ 的幸存状态卷。
    🔴 不要与 rootfs_images 同时用:ASG 把"EC2 running + 健康检查过"当作预热完成就停机,
    不等 cloud-init 跑完 → 预热出来的节点上 /opt/sbx/rootfs-{name}.ext4 全缺失,而 node-agent
    对缺失模板【静默回退 min】(image=claude-code 会返回 running 但 guest 里没有 CLI)。
    现阶段二选一:要命名模板就 rootfs_images= 且 sandbox_warm_pool_size=0,反之亦然。
    ⚠️ 成本:每台预热实例仍持有自己的根盘 + 400G 状态盘(delete_on_termination=false),
    停机不收 CPU 费但收 EBS 费;且这些卷在实例被替换后会残留,需按 tag 审核清理。
  EOT
  validation {
    condition     = var.sandbox_warm_pool_size >= 0
    error_message = "sandbox_warm_pool_size 不能为负。"
  }
}

variable "sandbox_warm_pool_target" {
  type        = string
  default     = "od"
  description = <<-EOT
    预热池挂在哪个数据面组上:od(默认)/ spot / both。
    默认挂 OD 组:被回收的是 Spot 节点,需要预热的是【接收疏散的 OD 容量】。
    Spot ASG 的 warm pool 属于 Stopped 状态复用,Spot 实例能否被 ASG 停机取决于 AWS 侧支持,
    本仓库未验证 —— 选 spot/both 时若 put-warm-pool 被拒,apply 会失败。
  EOT
  validation {
    condition     = contains(["od", "spot", "both"], var.sandbox_warm_pool_target)
    error_message = "sandbox_warm_pool_target 仅支持 \"od\"、\"spot\" 或 \"both\"。"
  }
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

variable "sandbox_az_index" {
  type        = number
  default     = 0
  description = "沙盒节点所在单一可用区索引:0/1/2 分别对应 VPC 的 region-a/b/c。遇到实例容量不足时切换索引。"
  validation {
    condition     = contains([0, 1, 2], var.sandbox_az_index)
    error_message = "sandbox_az_index 仅支持 0、1 或 2。"
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

# 自定义镜像(= 预打包运行环境):额外的命名 rootfs 模板(逗号分隔 name 列表)。节点从
# rootfs_s3_uri 同目录拉 rootfs-{name}.tar.gz 造 /opt/sbx/rootfs-{name}.ext4。
# 用 scripts/build-rootfs-image.sh <name> 构建上传。min 无需列出(即默认 rootfs)。
#
# 已内置预设:web(demo 站点)、claude-code(Node + Claude Code CLI)、openclaw(Node + OpenClaw)。
# 默认只装 web:每个模板在 pre-bootstrap 里要 dd+mkfs+解包一份 ext4,列太多会拉长节点引导时间
# (该阶段过长会打断 kubelet 心跳 → ASG 替换循环,见文件头注意事项)。要用重型镜像时显式传:
#   -var="rootfs_images=web,claude-code,openclaw"
# 未构建上传的 name 会被跳过(non-fatal),对应沙盒回退默认 min 模板。
variable "rootfs_images" {
  type        = string
  default     = "web"
  description = <<-EOT
    逗号分隔的命名 rootfs 模板列表(除 min 外),节点会各拉一份造 ext4 模板。
    内置预设:web / claude-code / openclaw(后两个是预打包运行环境,CLI 已烤进 rootfs)。
    模板 tarball 需先用 scripts/build-rootfs-image.sh 传到 rootfs_s3_uri 的同目录;
    claude-code / openclaw 必须在【同架构原生机器】上构建(Apple Silicon 跨架构构建
    npm install 会 qemu 段错误)。
    2026-08-13 真机:4 个模板的节点 pre-bootstrap 共 2m26s(每个模板 20-27s)。
    🔴 不要与 sandbox_warm_pool_size 同时用,原因见该变量说明。
  EOT
}

variable "rootfs_template_size_mib" {
  type        = number
  default     = 2048
  description = <<-EOT
    每个【命名】rootfs 模板 ext4 的大小(MiB)。默认 2048。
    预打包运行环境(claude-code/openclaw 含 Node 运行时)约占 0.7-1G,2G 尚有余量;
    要在 guest 里装更多东西就调大。注意:create 时会把模板整份复制到沙盒目录(跨文件系统,
    reflink 不生效),调大会同比增加每个沙盒的磁盘占用和 create 耗时。
  EOT
  validation {
    condition     = var.rootfs_template_size_mib >= 1024
    error_message = "rootfs_template_size_mib 至少 1024(小于 1G 装不下基础 python+sshd 层)。"
  }
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

# ---------- 数据面节点组:OD 池 + Spot 池共用的配置基底 ----------
locals {
  # 数据面 label(两个池共用);capacity-type 由各池追加,便于按池调度/统计。
  sandbox_labels = {
    role            = "sandbox"
    sandbox         = "true"
    "workload-tier" = "data"
  }

  # OD 组与 Spot 组共用:同一个 AZ、同 AMI、同 cloudinit、同持久状态 EBS、同 taint。
  # 只有 capacity_type / instance_types / 台数 / capacity-type label 由各池覆盖。
  sandbox_ng_base = {
    kubernetes_version = "1.31"
    ami_type           = local.node_arch_cfg.ami_type

    # Intel x86 虚拟化实例(r8i/i7i)必须显式打开 Nitro nested virtualization 才会暴露 /dev/kvm。
    # c6g.metal 直接使用宿主 KVM，不设置该选项。
    cpu_options = var.node_arch == "amd64" ? {
      nested_virtualization = "enabled"
    } : {}

    # 方案C:所有数据面节点(含 Spot 组、含预热池)必须【同一 AZ】—— EBS 状态卷不能跨 AZ attach。
    # 钉死到 sandbox_az_index 指定的单个 AZ,否则 EKS 会把节点分散到不同 AZ,
    # 导致 spot 疏散后状态卷无法 attach 到另一 AZ 的新节点。默认 0=region-a;
    # 若所选实例规格暂时无容量,可切到 1=region-b 或 2=region-c。
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
      # 方案C:独立【持久状态 EBS】挂 /var/lib/sbx —— 存所有 sandbox 的内存快照(base+diff)+ rootfs。
      # 高吞吐 gp3(1000MB/s)让 50 个 Diff 快照并发落盘 ~16s。
      # delete_on_termination=false → spot 强制终止后卷幸存,可 attach 到新机恢复(方案C核心)。
      # ⚠️ 该属性对 Spot 组和预热池同样生效:每台实例(包括停在预热池里的)都各持一块
      #    state_ebs_size_gb 的卷,实例被替换后卷会残留 → 按统一 tag 定期审核清理。
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

    # 本组只承载 node-agent + 裸 Firecracker microVM。NoSchedule 防止控制面、
    # CoreDNS、LiteLLM 等普通 Pod 在数据节点上落盘。
    taints = {
      dedicated_sandbox = {
        key    = "dedicated"
        value  = "sandbox"
        effect = "NO_SCHEDULE"
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
        # 识别:只选择 Amazon EBS,避免在带本地 NVMe instance store 的机型(如 i7i)上把它误当状态盘格式化。
        # r8i 无本地盘,这段判别对它是无害的恒真分支。
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

        # 命名 rootfs 模板(= 预打包运行环境):从 min-rootfs 同目录拉 rootfs-{name}.tar.gz,
        # 造 /opt/sbx/rootfs-{name}.ext4。node-agent 按沙盒 image 选模板(见 _rootfs_template_path)。
        # 由 build-rootfs-image.sh 构建上传(内置 web / claude-code / openclaw 预设);
        # 未上传的 name 会被跳过,对应沙盒回退默认 min,不影响节点启动。
        ROOTFS_PREFIX=$(dirname ${var.rootfs_s3_uri})   # s3://bucket/rootfs
        for IMG in $(echo "${var.rootfs_images}" | tr ',' ' '); do
          [ "$IMG" = "min" ] && continue   # min 即默认,上面已造
          aws s3 cp "$ROOTFS_PREFIX/rootfs-$IMG.tar.gz" /tmp/rootfs-$IMG.tar.gz --region ${var.region} 2>/dev/null && \
          dd if=/dev/zero of=/opt/sbx/rootfs-$IMG.ext4 bs=1M count=${var.rootfs_template_size_mib} status=none 2>/dev/null && \
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
  }

  # 池定义。OD 组的 key 保持历史值 sandbox_<arch>(不动已有 state 里的节点组);
  # Spot 组是新增的 sandbox_<arch>_spot。台数为 0 的池不创建。
  # system_arm64(控制面 On-Demand 组)完全不受这里影响。
  sandbox_pools = merge(
    var.sandbox_od_node_count > 0 ? {
      "sandbox_${var.node_arch}" = {
        tier           = "od"
        capacity_type  = "ON_DEMAND"
        node_count     = var.sandbox_od_node_count
        instance_types = [local.sandbox_instance_type]
      }
    } : {},
    var.sandbox_spot_node_count > 0 ? {
      "sandbox_${var.node_arch}_spot" = {
        tier          = "spot"
        capacity_type = "SPOT"
        node_count    = var.sandbox_spot_node_count
        instance_types = length(var.sandbox_spot_instance_types) > 0 ? (
          var.sandbox_spot_instance_types
        ) : [local.sandbox_instance_type]
      }
    } : {},
  )

  sandbox_node_groups = {
    for key, pool in local.sandbox_pools : key => merge(local.sandbox_ng_base, {
      capacity_type = pool.capacity_type
      # Spot 组可给多个候选机型提高容量命中率;OD 组只用 sandbox_instance_type。
      instance_types = pool.instance_types
      # 数据面不做自动伸缩:min=max=desired,容量变化走显式改变量 + apply。
      min_size     = pool.node_count
      max_size     = pool.node_count
      desired_size = pool.node_count
      labels       = merge(local.sandbox_labels, { "capacity-type" = pool.tier })
    })
  }

  # 需要挂预热池的数据面组(按 sandbox_warm_pool_target 过滤)。
  sandbox_warm_pool_targets = var.sandbox_warm_pool_size <= 0 ? {} : {
    for key, pool in local.sandbox_pools : key => pool
    if var.sandbox_warm_pool_target == "both" || var.sandbox_warm_pool_target == pool.tier
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
  # - system_arm64:固定 On-Demand Graviton，承载业务控制面和集群系统 Pod(不受数据面变量影响)。
  # - sandbox_<arch>:数据面 On-Demand 池,承载不可中断业务 + 接收 Spot 疏散。
  # - sandbox_<arch>_spot:数据面 Spot 池(sandbox_spot_node_count>0 时才创建)。
  #   两个数据面池由 local.sandbox_node_groups 从同一份基底派生,见上方 locals。
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
  }, local.sandbox_node_groups)

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
# OD 池和 Spot 池都要 patch(Spot 池同样是冷启动装 Firecracker/rootfs 才 Ready)。
resource "null_resource" "sandbox_asg_grace_period" {
  for_each = local.sandbox_node_groups

  # 节点组变化(如换机型/架构/池)时重新 patch
  triggers = {
    asg_name = module.eks.eks_managed_node_groups[each.key].node_group_autoscaling_group_names[0]
    region   = var.region
  }

  provisioner "local-exec" {
    command = <<-EOT
      aws autoscaling update-auto-scaling-group \
        --auto-scaling-group-name ${self.triggers.asg_name} \
        --health-check-grace-period 900 \
        --region ${self.triggers.region}
    EOT
  }
}

# ---------- 单 AZ 亲和的节点预热池(ASG warm pool) ----------
# 目的:把"新节点从零可调度"的 5-10 分钟(装 Firecracker + 内核 + rootfs 模板 + join 集群)
# 前移到预热阶段。预热实例启动一遍后进入 Warmed:Stopped,ASG 需要补节点时直接 start,
# 只剩 boot + kubelet 恢复,配合快照恢复才够 ≤5min 的整机恢复 SLO。
# 2026-08-13 真机(r8i.8xlarge):terminate 数据节点 → 新节点 Ready 44s
# (其中 warm-pool 实例 start 只花 7s),冷启动基线 5m03s。
#
# 单 AZ 亲和:预热池不能单独指定子网,它继承 ASG 的子网,而数据面 ASG 已被
# local.sandbox_ng_base.subnet_ids 钉死在 sandbox_az_index 这一个 AZ(方案C 硬约束:
# 持久状态 EBS 不能跨 AZ attach)→ 预热出来的机器一定和幸存状态卷同 AZ。
#
# 语义(以 `aws autoscaling put-warm-pool` 为准):
#   --min-size N                       预热池至少常备 N 台(绝对值,这是我们想要的)
#   --max-group-prepared-capacity D+N  ASG 非 Terminated 实例总上限 = 在役 D + 预热 N;
#                                      不设它时预热池大小 = ASG max - desired(本仓库 max=desired
#                                      → 会变成 0),所以必须显式给。
#   --pool-state Stopped               停机不收 CPU 费(EBS 照收,见下)
#   --instance-reuse-policy ReuseOnScaleIn=true  缩容时把机器放回预热池而不是销毁
#
# 🔴 2026-08-13 真机实测到的关键缺陷:ASG 把"EC2 running + 健康检查通过"当作预热完成就停机,
#    【不等 cloud-init/userdata 跑完】→ 预热出来的节点上 /opt/sbx/rootfs-{name}.ext4 全部缺失,
#    而 node-agent 对缺失模板【静默回退 min】→ image=claude-code 返回 running 但 guest 里没 CLI。
#    ⇒ 现阶段 sandbox_warm_pool_size>0 与 rootfs_images 非空【二选一】。
#    根治方向:模板缺失改报错/启动自愈补拉,或直接把模板烤进自定义 AMI。
# ✅ 已被真机否掉的担心:预热实例并不会产生 NotReady 的 Node 对象(实测全程 kubectl get nodes
#    只有正常节点数),不会污染节点数监控与告警。
# ⚠️ 仍未验证:Spot ASG 能否走 Stopped 预热池取决于 AWS 侧支持,故 sandbox_warm_pool_target 默认 od。
# ⚠️ 成本:每台预热实例各持一块 state_ebs_size_gb 的状态盘(delete_on_termination=false),
#    停机期间 EBS 照常计费,且实例被替换后卷会残留 → 按统一 tag 定期审核清理。
resource "null_resource" "sandbox_warm_pool" {
  for_each = local.sandbox_warm_pool_targets

  # grace period 先 patch 完再建预热池:预热/start 出来的机器同样要过 status check。
  depends_on = [null_resource.sandbox_asg_grace_period]

  triggers = {
    asg_name     = module.eks.eks_managed_node_groups[each.key].node_group_autoscaling_group_names[0]
    warm_size    = var.sandbox_warm_pool_size
    max_prepared = each.value.node_count + var.sandbox_warm_pool_size
    region       = var.region
  }

  provisioner "local-exec" {
    command = <<-EOT
      aws autoscaling put-warm-pool \
        --auto-scaling-group-name ${self.triggers.asg_name} \
        --min-size ${self.triggers.warm_size} \
        --max-group-prepared-capacity ${self.triggers.max_prepared} \
        --pool-state Stopped \
        --instance-reuse-policy ReuseOnScaleIn=true \
        --region ${self.triggers.region}
    EOT
  }

  # destroy provisioner 只能引用 self.triggers。--force-delete 会直接终止池内实例
  # (它们不承载任何沙盒,可安全终止);不删预热池会阻塞节点组/ASG 的销毁。
  provisioner "local-exec" {
    when       = destroy
    on_failure = continue
    command    = <<-EOT
      aws autoscaling delete-warm-pool \
        --auto-scaling-group-name ${self.triggers.asg_name} \
        --force-delete \
        --region ${self.triggers.region}
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

# 数据面所有池:{ od = "cluster:sandbox_amd64", spot = "cluster:sandbox_amd64_spot" }
output "sandbox_node_groups" {
  description = "数据面节点组:key 为池类型(od/spot),value 为托管节点组 ID。"
  value = {
    for key, pool in local.sandbox_pools :
    pool.tier => module.eks.eks_managed_node_groups[key].node_group_id
  }
}

# 兼容旧脚本:仍返回 On-Demand 数据面组(sandbox_od_node_count=0 时为 null)。
output "sandbox_node_group_name" {
  description = "数据面 On-Demand 节点组 ID(历史输出,新脚本请用 sandbox_node_groups)。"
  value       = try(module.eks.eks_managed_node_groups["sandbox_${var.node_arch}"].node_group_id, null)
}

output "sandbox_warm_pool" {
  description = "预热池配置摘要(size=0 表示未启用)。"
  value = {
    size    = var.sandbox_warm_pool_size
    target  = var.sandbox_warm_pool_target
    applied = keys(local.sandbox_warm_pool_targets)
  }
}
