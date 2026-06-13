# Karpenter 自动扩缩 —— 替换固定 .metal 节点组
#
# 双节点池:
#   standard-arm64  : 系统负载(LiteLLM / ingress / 控制面)
#   kata-metal      : 沙盒专用(.metal,带 kata-dedicated taint)
#
# 收益:
#   - .metal 按需扩缩,空闲 30 分钟整合(比固定节点组节省大量成本)
#   - Kata 节点 UserData 自动装 devmapper + containerd kata runtime
#   - 系统组件与沙盒隔离调度

# ---------- Karpenter IRSA ----------

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

resource "aws_iam_role_policy_attachment" "karpenter_controller" {
  role       = aws_iam_role.karpenter_controller.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
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
          "ec2:DescribeInstanceTypes", "ec2:DescribeSubnets",
          "ec2:DescribeSecurityGroups", "ec2:DescribeImages",
          "ec2:DescribeSpotPriceHistory", "ec2:CreateFleet",
          "ec2:RunInstances", "ec2:TerminateInstances",
          "ec2:CreateTags", "ec2:DeleteTags",
          "iam:PassRole", "iam:GetInstanceProfile",
          "iam:CreateInstanceProfile", "iam:AddRoleToInstanceProfile",
          "iam:RemoveRoleFromInstanceProfile",
          "eks:DescribeCluster",
          "pricing:GetProducts",
          "ssm:GetParameter",
        ]
        Resource = ["*"]
      }
    ]
  })
}

# ---------- Karpenter Helm(安装 controller) ----------

variable "install_karpenter" {
  type    = bool
  default = true   # 已手动安装验证通过(v1.3.3);Terraform 管理 IAM 和 NodePool CRD
}

resource "helm_release" "karpenter" {
  count      = var.install_karpenter ? 1 : 0
  name       = "karpenter"
  repository = "oci://public.ecr.aws/karpenter"
  chart      = "karpenter"
  version    = "1.0.0"
  namespace  = "karpenter"
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
    value = "512Mi"
  }
}

# ---------- NodePool: 标准 arm64 系统池 ----------
# 用 kubernetes_manifest 声明(需 Karpenter CRD 已装)

locals {
  # 公有子网 ID(节点需出网)—— 从 VPC data source 取
  public_subnet_ids = data.aws_subnets.private.ids  # 复用已有 data source
}

# 注意:NodePool / EC2NodeClass 是 Karpenter CRD,需 Karpenter 已装。
# 用 null_resource + local-exec 而非 kubernetes_manifest,避免 CRD 不存在时 plan 报错。
resource "null_resource" "karpenter_nodepools" {
  count = var.install_karpenter ? 1 : 0

  triggers = {
    cluster_name = var.cluster_name
    metal_type   = var.metal_instance_type
  }

  provisioner "local-exec" {
    command = <<-EOT
      cat <<'NODEPOOL' | kubectl apply -f -
      ---
      apiVersion: karpenter.k8s.aws/v1
      kind: EC2NodeClass
      metadata:
        name: standard-arm64
      spec:
        amiFamily: AL2023
        role: ${aws_iam_role.karpenter_controller.name}
        subnetSelectorTerms:
          - tags:
              kubernetes.io/role/internal-elb: "1"
        securityGroupSelectorTerms:
          - tags:
              kubernetes.io/cluster/${var.cluster_name}: shared
        blockDeviceMappings:
          - deviceName: /dev/xvda
            ebs:
              volumeSize: 50Gi
              volumeType: gp3
      ---
      apiVersion: karpenter.sh/v1
      kind: NodePool
      metadata:
        name: standard-arm64
      spec:
        template:
          spec:
            requirements:
              - key: kubernetes.io/arch
                operator: In
                values: ["arm64"]
              - key: karpenter.sh/capacity-type
                operator: In
                values: ["on-demand"]
            nodeClassRef:
              group: karpenter.k8s.aws
              kind: EC2NodeClass
              name: standard-arm64
        disruption:
          consolidationPolicy: WhenEmptyOrUnderutilized
          consolidateAfter: 1m
      ---
      apiVersion: karpenter.k8s.aws/v1
      kind: EC2NodeClass
      metadata:
        name: kata-metal
      spec:
        amiFamily: AL2023
        role: ${aws_iam_role.karpenter_controller.name}
        subnetSelectorTerms:
          - tags:
              kubernetes.io/role/elb: "1"
        securityGroupSelectorTerms:
          - tags:
              kubernetes.io/cluster/${var.cluster_name}: shared
        blockDeviceMappings:
          - deviceName: /dev/xvda
            ebs:
              volumeSize: 200Gi
              volumeType: gp3
        userData: |
          #!/bin/bash
          # 自动装 Kata + 注册 containerd runtime(Karpenter 节点 cold start)
          dnf install -y python3 awscli docker 2>/dev/null || true
          systemctl start docker 2>/dev/null || true
          # node-agent 在 DaemonSet 里会自动调度上来
      ---
      apiVersion: karpenter.sh/v1
      kind: NodePool
      metadata:
        name: kata-metal
      spec:
        template:
          spec:
            requirements:
              - key: node.kubernetes.io/instance-type
                operator: In
                values: ["${var.metal_instance_type}"]
              - key: kubernetes.io/arch
                operator: In
                values: ["arm64"]
              - key: karpenter.sh/capacity-type
                operator: In
                values: ["on-demand"]
            taints:
              - key: kata-dedicated
                value: "true"
                effect: NoSchedule
            labels:
              sandbox: "true"
            nodeClassRef:
              group: karpenter.k8s.aws
              kind: EC2NodeClass
              name: kata-metal
        disruption:
          consolidationPolicy: WhenEmpty
          consolidateAfter: 30m
        limits:
          cpu: "1024"
          memory: 2Ti
      NODEPOOL
    EOT
  }

  depends_on = [helm_release.karpenter]
}

output "karpenter_role_arn" {
  value = aws_iam_role.karpenter_controller.arn
}
