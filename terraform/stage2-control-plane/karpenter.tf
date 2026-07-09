# Karpenter 自动扩缩 —— .metal 节点按需扩缩，空闲 30 分钟整合
#
# 重要：Karpenter 需要一个专用的 EKS Worker Node IAM Role（非控制器 Role）
# 来启动节点。本文件创建该 Role 并与 EC2NodeClass 关联。
#
# 使用方式：
#   设 install_karpenter=true 时，Terraform 通过 helm 安装 controller，
#   并通过 null_resource local-exec 部署 NodePool/EC2NodeClass CRD。

# ---------- Karpenter Controller IRSA ----------

resource "aws_iam_role" "karpenter_controller" {
  name = "${var.cluster_name}-karpenter"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_issuer}:sub" = "system:serviceaccount:karpenter:karpenter"
          "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "karpenter_controller_inline" {
  name = "karpenter-controller"
  role = aws_iam_role.karpenter_controller.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ec2:CreateLaunchTemplate", "ec2:DeleteLaunchTemplate",
          "ec2:DescribeLaunchTemplates", "ec2:DescribeInstances",
          "ec2:DescribeInstanceTypes", "ec2:DescribeInstanceTypeOfferings",
          "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups",
          "ec2:DescribeImages", "ec2:DescribeAvailabilityZones",
          "ec2:DescribeSpotPriceHistory", "ec2:DescribeLaunchTemplateVersions",
          "ec2:DescribeKeyPairs", "ec2:CreateFleet",
          "ec2:RunInstances", "ec2:TerminateInstances",
          "ec2:CreateTags", "ec2:DeleteTags",
        ]
        Resource = ["*"]
      },
      {
        Effect = "Allow"
        Action = [
          "iam:PassRole",
          "iam:GetInstanceProfile",
          "iam:CreateInstanceProfile",
          "iam:DeleteInstanceProfile",
          "iam:AddRoleToInstanceProfile",
          "iam:RemoveRoleFromInstanceProfile",
          "iam:TagInstanceProfile",
        ]
        Resource = ["*"]
      },
      {
        Effect   = "Allow"
        Action   = ["eks:DescribeCluster", "pricing:GetProducts", "ssm:GetParameter"]
        Resource = ["*"]
      }
    ]
  })
}

# ---------- Karpenter Worker Node IAM Role ----------
# 这是 Karpenter 启动 EC2 实例所用的 IAM 角色（不是控制器自身的角色）
# 需要 EKS Worker Node 的标准策略，节点才能 join 集群

resource "aws_iam_role" "karpenter_node" {
  name = "${var.cluster_name}-karpenter-node"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "karpenter_node_worker" {
  role       = aws_iam_role.karpenter_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "karpenter_node_cni" {
  role       = aws_iam_role.karpenter_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_role_policy_attachment" "karpenter_node_ecr" {
  role       = aws_iam_role.karpenter_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

resource "aws_iam_role_policy_attachment" "karpenter_node_ssm" {
  role       = aws_iam_role.karpenter_node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# S3 快照访问（node-agent 上传/下载快照）
resource "aws_iam_role_policy" "karpenter_node_s3" {
  name = "karpenter-node-s3-snapshot"
  role = aws_iam_role.karpenter_node.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
      Resource = local.snapshot_bucket != "" ? [
        "arn:aws:s3:::${local.snapshot_bucket}",
        "arn:aws:s3:::${local.snapshot_bucket}/*",
      ] : ["arn:aws:s3:::placeholder"]
    }]
  })
}

# Karpenter controller 需要 PassRole 到 worker node role
resource "aws_iam_role_policy" "karpenter_controller_node_role" {
  name = "karpenter-grant-node-role"
  role = aws_iam_role.karpenter_controller.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["iam:PassRole"]
      Resource = [aws_iam_role.karpenter_node.arn]
    }]
  })
}

# ---------- EKS Access Entry —— Karpenter 节点 Role 授权 ----------
# Karpenter 启动的新节点使用 karpenter_node role。
# 没有这条 Access Entry，节点虽然有 IAM 权限，但 EKS 控制面不认它，
# kubelet 无法 join 集群（TLS bootstrap 会被拒绝）。
#
# EKS 1.28+ 推荐用 Access Entry API（不需要手改 aws-auth ConfigMap）。
# 等价的手动命令（apply 后若节点仍 NotReady 时调试用）：
#   aws eks create-access-entry \
#     --cluster-name claude-sbx \
#     --principal-arn arn:aws:iam::<acct>:role/claude-sbx-karpenter-node \
#     --type EC2_LINUX
resource "aws_eks_access_entry" "karpenter_node" {
  cluster_name  = var.cluster_name
  principal_arn = aws_iam_role.karpenter_node.arn
  # EC2_LINUX 类型自动附加 AmazonEKSWorkerNodePolicy 所需的系统组
  # (system:bootstrappers, system:nodes)，节点才能通过 TLS bootstrap join 集群
  type = "EC2_LINUX"
}

# ---------- Karpenter Helm（安装 controller）----------

variable "install_karpenter" {
  type    = bool
  default = false
  # Karpenter Helm OCI 在许多环境下需要 docker-credential-desktop，
  # 会导致 apply 报错。默认关闭，通过 README Step 7 手动安装。
  # 已验证可用命令：python3 -c "..." && helm upgrade --install karpenter oci://...
}

resource "helm_release" "karpenter" {
  count            = var.install_karpenter ? 1 : 0
  name             = "karpenter"
  repository       = "oci://public.ecr.aws/karpenter"
  chart            = "karpenter"
  version          = "1.3.3" # 与手动安装版本一致
  namespace        = "karpenter"
  create_namespace = true

  set {
    name  = "settings.clusterName"
    value = var.cluster_name
  }
  set {
    name  = "settings.clusterEndpoint"
    value = data.aws_eks_cluster.main.endpoint
  }
  set {
    name  = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
    value = aws_iam_role.karpenter_controller.arn
  }
  set {
    name  = "controller.resources.requests.cpu"
    value = "250m"
  }
  set {
    name  = "controller.resources.requests.memory"
    value = "256Mi"
  }
  set {
    name  = "controller.resources.limits.cpu"
    value = "1"
  }
  set {
    name  = "controller.resources.limits.memory"
    value = "1Gi"
  }
}

# ---------- NodePool / EC2NodeClass ----------
# Karpenter v1 API: amiFamily 已废弃，改用 amiSelectorTerms with alias
# 修复点：
#   - amiFamily → amiSelectorTerms[].alias: al2023@latest
#   - role → 使用 karpenter_node role（EKS worker，非 controller）
#   - standard-arm64 使用 public subnet（elb 标签），无 NAT 时节点也能出网
#   - securityGroupSelectorTerms 使用 owned 标签（与 phase3 SG 标签一致）

resource "null_resource" "karpenter_nodepools" {
  count = var.install_karpenter ? 1 : 0

  triggers = {
    cluster_name = var.cluster_name
    metal_type   = local.metal_type
    node_arch    = var.node_arch
    node_role    = aws_iam_role.karpenter_node.name
  }

  provisioner "local-exec" {
    command = <<-EOT
      cat <<'NODEPOOL' | kubectl apply -f -
      ---
      # EC2NodeClass for system workloads (non-.metal)
      apiVersion: karpenter.k8s.aws/v1
      kind: EC2NodeClass
      metadata:
        name: standard-${var.node_arch}
      spec:
        amiSelectorTerms:
          - alias: al2023@latest
        role: ${aws_iam_role.karpenter_node.name}
        subnetSelectorTerms:
          - tags:
              kubernetes.io/role/elb: "1"
        securityGroupSelectorTerms:
          - tags:
              kubernetes.io/cluster/${var.cluster_name}: owned
        blockDeviceMappings:
          - deviceName: /dev/xvda
            ebs:
              volumeSize: 50Gi
              volumeType: gp3
      ---
      apiVersion: karpenter.sh/v1
      kind: NodePool
      metadata:
        name: standard-${var.node_arch}
      spec:
        template:
          spec:
            requirements:
              - {key: kubernetes.io/arch, operator: In, values: ["${var.node_arch}"]}
              - {key: karpenter.sh/capacity-type, operator: In, values: ["on-demand"]}
            nodeClassRef:
              group: karpenter.k8s.aws
              kind: EC2NodeClass
              name: standard-${var.node_arch}
        disruption:
          consolidationPolicy: WhenEmptyOrUnderutilized
          consolidateAfter: 1m
      ---
      # NOTE: 承载 sandbox 的 .metal 节点当前由 phase3 的托管节点组提供
      #       (固定 desired，打 sandbox=true label，node-agent DaemonSet 直起裸 Firecracker)。
      #       因此这里【不再】定义 sandbox 的 metal NodePool。
      #
      #       历史上曾有一个 `kata-metal` NodePool(带 kata-dedicated 污点，供 Kata pod 调度)，
      #       随 Kata 后端移除已删除。
      #
      #       预留位:未来落地 spot 回收自动恢复(见 docs/spot-reclaim-recovery-design.md)时，
      #       可在此新增一个【FC-only】的 metal NodePool —— 用于在 spot 节点被回收后，
      #       同 AZ 快速拉起替补 .metal 节点承接跨机恢复(无 kata 污点，打 sandbox=true)。
      NODEPOOL
    EOT
  }

  depends_on = [helm_release.karpenter]
}

output "karpenter_role_arn" {
  value = aws_iam_role.karpenter_controller.arn
}

output "karpenter_node_role_name" {
  value       = aws_iam_role.karpenter_node.name
  description = "EC2NodeClass spec.role 的值，Karpenter 用此 Role 启动节点"
}
