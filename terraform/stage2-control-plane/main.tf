# Stage 2 — 统一控制面部署
#
# 在 Phase 3 的 EKS 集群上叠加:
#   - DynamoDB 表(引用 stage1-dynamodb 的 outputs)
#   - IRSA IAM 角色(控制面 + node-agent)
#   - Fargate Profile(sandbox-system namespace 用 Fargate 跑控制面)
#   - K8s 资源:Namespace / ConfigMap / Secret / RBAC /
#              控制面 Deployment+Service / node-agent DaemonSet
#
# 前提:
#   1. terraform/phase3 已 apply(EKS 集群存在)
#   2. terraform/stage1-dynamodb 已 apply(DynamoDB 表存在)
#   3. 镜像已推到 ECR:claude-sbx:poc(sandbox), sandbox-control-plane:latest, node-agent:latest
#
# 用法:
#   cd terraform/stage2-control-plane
#   terraform init
#   terraform apply \
#     -var="cluster_name=claude-sbx" \
#     -var="region=us-east-1" \
#     -var="sandbox_image=<acct>.dkr.ecr.us-east-1.amazonaws.com/claude-sbx:poc" \
#     -var="control_plane_image=<acct>.dkr.ecr.us-east-1.amazonaws.com/sandbox-control-plane:latest" \
#     -var="node_agent_image=<acct>.dkr.ecr.us-east-1.amazonaws.com/node-agent:latest" \
#     -var="litellm_url=http://litellm.litellm.svc.cluster.local:4000" \
#     -var="snapshot_s3_bucket=<your-bucket>"

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws        = { source = "hashicorp/aws",        version = "~> 5.0" }
    kubernetes = { source = "hashicorp/kubernetes",  version = "~> 2.0" }
    helm       = { source = "hashicorp/helm",        version = "~> 2.0" }
    null       = { source = "hashicorp/null",        version = "~> 3.0" }
  }
}

# ---------- Variables ----------

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "cluster_name" {
  type    = string
  default = "claude-sbx"
}

variable "sandbox_image" {
  type        = string
  description = "ECR URL for claude-sbx sandbox image"
}

variable "control_plane_image" {
  type        = string
  description = "ECR URL for sandbox-control-plane image"
}

variable "node_agent_image" {
  type        = string
  description = "ECR URL for node-agent image"
}

# B2(FirecrackerDriver): driver 选择,默认 kata;FC 模式传 firecracker
variable "sandbox_driver" {
  type        = string
  description = "Sandbox backend driver: kata | firecracker"
  default     = "kata"
}

# B2: FC 模式下控制面访问 node-agent 的节点内网 IP(逗号分隔)
variable "fc_nodes" {
  type        = string
  description = "Comma-separated private IPs of metal nodes running node-agent (firecracker mode)"
  default     = ""
}

variable "litellm_url" {
  type    = string
  default = "http://litellm.litellm.svc.cluster.local:4000"
}

variable "snapshot_s3_bucket" {
  type    = string
  default = ""
}

variable "sandbox_domain" {
  type    = string
  default = "sbx.example.com"
}

variable "warm_pool_size" {
  type    = number
  default = 3
}

variable "control_plane_replicas" {
  type    = number
  default = 2
}

variable "node_arch" {
  type        = string
  default     = "arm64"
  description = "节点 CPU 架构:arm64(Graviton,默认) 或 amd64(Intel x86)。需与 phase3 的 node_arch 一致。"
  validation {
    condition     = contains(["arm64", "amd64"], var.node_arch)
    error_message = "node_arch 仅支持 \"arm64\" 或 \"amd64\"。"
  }
}

variable "metal_instance_type" {
  type        = string
  default     = "" # 留空时按 node_arch 选默认机型(arm64→c6g.metal / amd64→c5n.metal)
  description = ".metal 实例类型(沙盒节点池)。留空则由 node_arch 决定:arm64=c6g.metal,amd64=c5n.metal。"
}

locals {
  # 架构 → 默认 .metal 机型(amd64 取最便宜的 Intel x86 裸金属 c5n.metal)
  default_metal_by_arch = {
    arm64 = "c6g.metal"
    amd64 = "c5n.metal"
  }
  metal_type = var.metal_instance_type != "" ? var.metal_instance_type : local.default_metal_by_arch[var.node_arch]
}

# ---------- Providers ----------

provider "aws" { region = var.region }

data "aws_eks_cluster" "main" { name = var.cluster_name }
data "aws_eks_cluster_auth" "main" { name = var.cluster_name }

provider "kubernetes" {
  host                   = data.aws_eks_cluster.main.endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.main.certificate_authority[0].data)
  token                  = data.aws_eks_cluster_auth.main.token
}

provider "helm" {
  kubernetes {
    host                   = data.aws_eks_cluster.main.endpoint
    cluster_ca_certificate = base64decode(data.aws_eks_cluster.main.certificate_authority[0].data)
    token                  = data.aws_eks_cluster_auth.main.token
  }
}

# ---------- Data sources ----------

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# OIDC provider(EKS 모듈이 자동 생성)
data "aws_iam_openid_connect_provider" "eks" {
  url = data.aws_eks_cluster.main.identity[0].oidc[0].issuer
}

# DynamoDB 表名(直接用变量,不跨 state 引用以降低耦合)
locals {
  account_id          = data.aws_caller_identity.current.account_id
  oidc_provider_arn   = data.aws_iam_openid_connect_provider.eks.arn
  # OIDC issuer URL 去掉 https://
  oidc_issuer         = replace(data.aws_eks_cluster.main.identity[0].oidc[0].issuer, "https://", "")
  dynamodb_table      = "${var.cluster_name}-sandboxes"
  dynamodb_events     = "${var.cluster_name}-sandbox-events"
  dynamodb_tap_idx    = "${var.cluster_name}-tap-idx"
}

# ---------- Snapshot S3 Bucket(optional, 若未提供则跳过) ----------

resource "aws_s3_bucket" "snapshots" {
  count  = var.snapshot_s3_bucket == "" ? 1 : 0
  bucket = "${var.cluster_name}-snapshots-${local.account_id}"
  tags   = { Project = "claude-sbx-poc" }
}

locals {
  snapshot_bucket = var.snapshot_s3_bucket != "" ? var.snapshot_s3_bucket : (
    length(aws_s3_bucket.snapshots) > 0 ? aws_s3_bucket.snapshots[0].id : ""
  )
}

resource "aws_s3_bucket_lifecycle_configuration" "snapshots" {
  count  = length(aws_s3_bucket.snapshots)
  bucket = aws_s3_bucket.snapshots[0].id
  rule {
    id     = "expire-old-snapshots"
    status = "Enabled"
    filter { prefix = "sbx/" }
    expiration { days = 30 }
  }
}

# ---------- IRSA: 控制面 IAM 角色 ----------

resource "aws_iam_role" "control_plane" {
  name = "${var.cluster_name}-control-plane"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_issuer}:sub" = "system:serviceaccount:sandbox-system:sandbox-control-plane"
          "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "control_plane" {
  name = "control-plane-policy"
  role = aws_iam_role.control_plane.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # DynamoDB 读写
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem",
                    "dynamodb:DeleteItem","dynamodb:Query","dynamodb:Scan"]
        Resource = [
          "arn:aws:dynamodb:${var.region}:${local.account_id}:table/${local.dynamodb_table}",
          "arn:aws:dynamodb:${var.region}:${local.account_id}:table/${local.dynamodb_table}/index/*",
          "arn:aws:dynamodb:${var.region}:${local.account_id}:table/${local.dynamodb_events}",
          "arn:aws:dynamodb:${var.region}:${local.account_id}:table/${local.dynamodb_tap_idx}",
        ]
      },
      # EC2 DescribeInstances(节点 IP → 实例 ID)
      {
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances"]
        Resource = ["*"]
      },
      # SSM SendCommand(备用:通过 SSM 调 node-agent)
      {
        Effect   = "Allow"
        Action   = ["ssm:SendCommand","ssm:GetCommandInvocation"]
        Resource = ["*"]
      },
      # S3 快照读写
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:ListBucket"]
        Resource = local.snapshot_bucket != "" ? [
          "arn:aws:s3:::${local.snapshot_bucket}",
          "arn:aws:s3:::${local.snapshot_bucket}/*",
        ] : ["arn:aws:s3:::placeholder"]
      },
    ]
  })
}

# ---------- IRSA: node-agent IAM 角色(.metal 节点上的 DaemonSet) ----------

resource "aws_iam_role" "node_agent" {
  name = "${var.cluster_name}-node-agent"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_issuer}:sub" = "system:serviceaccount:sandbox-system:node-agent"
          "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "node_agent" {
  name = "node-agent-policy"
  role = aws_iam_role.node_agent.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # S3 快照上传/下载
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:ListBucket"]
        Resource = local.snapshot_bucket != "" ? [
          "arn:aws:s3:::${local.snapshot_bucket}",
          "arn:aws:s3:::${local.snapshot_bucket}/*",
        ] : ["arn:aws:s3:::placeholder"]
      },
      # ECR 拉镜像(rootfs 构建产物)
      {
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken","ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer"]
        Resource = ["*"]
      },
    ]
  })
}

# ---------- Kubernetes: Namespace ----------

resource "kubernetes_namespace" "sandbox_system" {
  metadata {
    name = "sandbox-system"
    labels = { "app.kubernetes.io/managed-by" = "terraform" }
  }
}

# ---------- Kubernetes: ServiceAccounts ----------

resource "kubernetes_service_account" "control_plane" {
  metadata {
    name      = "sandbox-control-plane"
    namespace = kubernetes_namespace.sandbox_system.metadata[0].name
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.control_plane.arn
    }
  }
}

resource "kubernetes_service_account" "node_agent" {
  metadata {
    name      = "node-agent"
    namespace = kubernetes_namespace.sandbox_system.metadata[0].name
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.node_agent.arn
    }
  }
}

# ---------- Kubernetes: RBAC ----------
# 控制面需要操作 default namespace 的 Pod/Service/Ingress

resource "kubernetes_cluster_role" "control_plane" {
  metadata { name = "sandbox-control-plane" }
  rule {
    api_groups = [""]
    resources  = ["pods","services","configmaps","secrets"]
    verbs      = ["get","list","watch","create","update","patch","delete","deletecollection"]
  }
  rule {
    # pods/exec 是独立子资源,exec 走 websocket 需要 get+create
    api_groups = [""]
    resources  = ["pods/exec"]
    verbs      = ["get","create"]
  }
  rule {
    api_groups = ["networking.k8s.io"]
    resources  = ["ingresses"]
    verbs      = ["get","list","watch","create","update","patch","delete","deletecollection"]
  }
  rule {
    api_groups = ["agents.x-k8s.io"]
    resources  = ["sandboxes","sandboxtemplates","sandboxwarmpools","sandboxclaims"]
    verbs      = ["get","list","watch","create","update","patch","delete"]
  }
}

resource "kubernetes_cluster_role_binding" "control_plane" {
  metadata { name = "sandbox-control-plane" }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role.control_plane.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account.control_plane.metadata[0].name
    namespace = kubernetes_namespace.sandbox_system.metadata[0].name
  }
}

# ---------- Kubernetes: Fargate Profile(可选,默认关闭) ----------
# 控制面 Pod 默认跑在普通节点(不依赖 Fargate)
# 生产开启:创建 Fargate 执行角色后设 enable_fargate=true

variable "enable_fargate" {
  type    = bool
  default = false
  description = "是否为 sandbox-system namespace 创建 Fargate Profile"
}

# 从集群 VPC 取公有子网(节点池所在)
data "aws_vpc" "cluster" {
  tags = { Name = "${var.cluster_name}-vpc" }
}

data "aws_subnets" "private" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.cluster.id]
  }
  tags = { "kubernetes.io/role/internal-elb" = "1" }
}

# Fargate 执行角色(enable_fargate=true 时需要存在)
resource "aws_iam_role" "fargate_pod_execution" {
  count = var.enable_fargate ? 1 : 0
  name  = "${var.cluster_name}-fargate"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "eks-fargate-pods.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "fargate_pod_execution" {
  count      = var.enable_fargate ? 1 : 0
  role       = aws_iam_role.fargate_pod_execution[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSFargatePodExecutionRolePolicy"
}

resource "aws_eks_fargate_profile" "sandbox_system" {
  count                  = var.enable_fargate ? 1 : 0
  cluster_name           = var.cluster_name
  fargate_profile_name   = "sandbox-system"
  pod_execution_role_arn = aws_iam_role.fargate_pod_execution[0].arn
  subnet_ids             = data.aws_subnets.private.ids

  selector {
    namespace = "sandbox-system"
    labels    = { "fargate" = "true" }
  }

  tags = { Project = "claude-sbx-poc" }
  depends_on = [aws_iam_role_policy_attachment.fargate_pod_execution]
}

# ---------- Kubernetes: ConfigMap(控制面配置) ----------

# ---------- API Keys Secret（控制面鉴权）----------
# api_keys 为空字符串时控制面启动后拒绝所有受保护请求（安全失败）
# 生产必须传入真实的随机密钥，例如:
#   -var='api_keys=sk-abc123,sk-def456'
variable "api_keys" {
  type        = string
  default     = ""
  sensitive   = true
  description = "逗号分隔的 Bearer token 列表，注入控制面 API_KEYS env（生产必填）"
}

resource "kubernetes_secret" "control_plane_api_keys" {
  metadata {
    name      = "control-plane-api-keys"
    namespace = kubernetes_namespace.sandbox_system.metadata[0].name
  }
  data = {
    API_KEYS = var.api_keys
  }
  type = "Opaque"
}

resource "kubernetes_config_map" "control_plane" {
  metadata {
    name      = "sandbox-control-plane"
    namespace = kubernetes_namespace.sandbox_system.metadata[0].name
  }
  data = {
    SANDBOX_DRIVER          = var.sandbox_driver   # B2: 可传 firecracker
    DYNAMODB_TABLE          = local.dynamodb_table
    DYNAMODB_EVENTS_TABLE   = local.dynamodb_events
    DYNAMODB_TAPIDX_TABLE   = local.dynamodb_tap_idx
    AWS_REGION              = var.region
    SANDBOX_IMAGE           = var.sandbox_image
    LITELLM_URL             = var.litellm_url
    SANDBOX_DOMAIN          = var.sandbox_domain
    KATA_RUNTIME_CLASS      = "kata-qemu"
    K8S_NAMESPACE           = "default"
    SNAPSHOT_S3_BUCKET      = local.snapshot_bucket
    WARM_POOL_SIZE          = tostring(var.warm_pool_size)
    WARM_POOL_REFILL_S      = "60"
    LISTEN_PORT             = "8000"
    LISTEN_HOST             = "0.0.0.0"
    NODE_AGENT_PORT         = "8002"
    # B2(FirecrackerDriver): 控制面靠 FC_NODES(逗号分隔的节点内网 IP)找 node-agent
    FC_NODES                = var.fc_nodes
    FC_KERNEL_PATH          = "/opt/sbx/vmlinux"
  }
}

# ---------- Kubernetes: 控制面 Deployment ----------

resource "kubernetes_deployment" "control_plane" {
  # wait_for_rollout=false: 避免首次拉取 ECR 镜像时 apply 超时
  # 用 kubectl rollout status -n sandbox-system deployment/sandbox-control-plane 确认就绪
  wait_for_rollout = false

  metadata {
    name      = "sandbox-control-plane"
    namespace = kubernetes_namespace.sandbox_system.metadata[0].name
    labels    = { app = "sandbox-control-plane", fargate = "true" }
  }
  spec {
    replicas = var.control_plane_replicas
    selector { match_labels = { app = "sandbox-control-plane" } }
    template {
      metadata { labels = { app = "sandbox-control-plane", fargate = "true" } }
      spec {
        service_account_name = kubernetes_service_account.control_plane.metadata[0].name
        container {
          name  = "api"
          image = var.control_plane_image
          command = ["python3", "-m", "sandbox_api.app"]
          port { container_port = 8000 }
          env_from {
            config_map_ref { name = kubernetes_config_map.control_plane.metadata[0].name }
          }
          # API_KEYS 从 Secret 注入（不进 ConfigMap 避免明文暴露）
          env_from {
            secret_ref { name = kubernetes_secret.control_plane_api_keys.metadata[0].name }
          }
          resources {
            requests = { cpu = "250m", memory = "512Mi" }
            limits   = { cpu = "1",    memory = "1Gi"   }
          }
          readiness_probe {
            http_get {
              path = "/"
              port = 8000
            }
            initial_delay_seconds = 5
            period_seconds        = 10
          }
          liveness_probe {
            http_get {
              path = "/"
              port = 8000
            }
            initial_delay_seconds = 15
            period_seconds        = 20
          }
        }
      }
    }
  }
}

# ---------- Kubernetes: 控制面 Service ----------

resource "kubernetes_service" "control_plane" {
  metadata {
    name      = "sandbox-control-plane"
    namespace = kubernetes_namespace.sandbox_system.metadata[0].name
  }
  spec {
    selector = { app = "sandbox-control-plane" }
    port {
      port        = 80
      target_port = 8000
      protocol    = "TCP"
    }
    type = "ClusterIP"
  }
}

# ---------- Kubernetes: node-agent DaemonSet(.metal 节点专用) ----------

resource "kubernetes_daemon_set_v1" "node_agent" {
  metadata {
    name      = "node-agent"
    namespace = kubernetes_namespace.sandbox_system.metadata[0].name
  }
  spec {
    selector { match_labels = { app = "node-agent" } }
    template {
      metadata { labels = { app = "node-agent" } }
      spec {
        service_account_name = kubernetes_service_account.node_agent.metadata[0].name
        # 只调度到 .metal 沙盒节点
        node_selector = { sandbox = "true" }
        # 需要 hostNetwork + hostPID 才能操作 tap/Firecracker
        host_network = true
        host_pid     = true
        # 需要特权容器操作 KVM/tap/iptables
        container {
          name              = "agent"
          image             = var.node_agent_image
          image_pull_policy = "Always"
          command           = ["python3", "/app/main.py"]
          port {
            container_port = 8002
            host_port      = 8002
          }
          env {
            name  = "NODE_AGENT_PORT"
            value = "8002"
          }
          env {
            name  = "SBX_BASE"
            value = "/var/lib/sbx"
          }
          env {
            name  = "FC_BIN"
            value = "/usr/local/bin/firecracker"
          }
          env {
            name  = "JAILER_BIN"
            value = "/usr/local/bin/firecracker-jailer"
          }
          env {
            name  = "AWS_REGION"
            value = var.region
          }
          env {
            name = "NODE_ID"
            value_from {
              field_ref { field_path = "spec.nodeName" }
            }
          }
          env {
            name  = "SNAPSHOT_S3_BUCKET"
            value = local.snapshot_bucket
          }
          security_context {
            privileged = true
          }
          volume_mount {
            name       = "dev"
            mount_path = "/dev"
          }
          volume_mount {
            name       = "sbx-data"
            mount_path = "/var/lib/sbx"
          }
          volume_mount {
            name       = "fc-bins"
            mount_path = "/usr/local/bin"
            read_only  = true
          }
          # B2: rootfs 模板 + guest kernel 在宿主 /opt/sbx,node-agent 需挂入做 CoW 源
          volume_mount {
            name       = "fc-assets"
            mount_path = "/opt/sbx"
          }
          resources {
            requests = { cpu = "100m", memory = "256Mi" }
            limits   = { cpu = "2",    memory = "4Gi"   }
          }
        }
        volume {
          name = "dev"
          host_path { path = "/dev" }
        }
        volume {
          name = "sbx-data"
          host_path {
            path = "/var/lib/sbx"
            type = "DirectoryOrCreate"
          }
        }
        volume {
          name = "fc-bins"
          host_path { path = "/usr/local/bin" }
        }
        volume {
          name = "fc-assets"
          host_path {
            path = "/opt/sbx"
            type = "DirectoryOrCreate"
          }
        }
        toleration {
          key      = "kata-dedicated"
          operator = "Exists"
          effect   = "NoSchedule"
        }
      }
    }
  }
}

# ---------- Kubernetes: ingress-nginx(共享 NLB) ----------
# 若 phase3 已装则设 create_ingress_nginx=false 跳过

variable "create_ingress_nginx" {
  type    = bool
  default = true
}

resource "helm_release" "ingress_nginx" {
  count      = var.create_ingress_nginx ? 1 : 0
  name       = "ingress-nginx"
  repository = "https://kubernetes.github.io/ingress-nginx"
  chart      = "ingress-nginx"
  namespace  = "ingress-nginx"
  create_namespace = true
  set {
    name  = "controller.service.annotations.service\\.beta\\.kubernetes\\.io/aws-load-balancer-type"
    value = "nlb"
  }
  set {
    name  = "controller.ingressClassResource.default"
    value = "true"
  }
}

# ---------- 控制面 Ingress(生产外部访问) ----------
# 通过 ingress-nginx 暴露控制面 API，支持从集群外直接访问。
# 访问地址: https://api.sbx.<sandbox_domain>
# 生产应配 ACM 证书 + Route53；POC 用 HTTP 或 kubectl port-forward。

variable "expose_control_plane" {
  type    = bool
  default = false
  description = "是否通过 Ingress 对外暴露控制面 API（生产需同时配置 TLS + API_KEYS Secret）"
}

resource "kubernetes_ingress_v1" "control_plane" {
  count = var.expose_control_plane ? 1 : 0

  metadata {
    name      = "sandbox-control-plane"
    namespace = kubernetes_namespace.sandbox_system.metadata[0].name
    annotations = {
      "nginx.ingress.kubernetes.io/rewrite-target" = "/"
      # ⚠️  生产必须开启 TLS，否则 API_KEYS 在传输中明文暴露：
      #   "nginx.ingress.kubernetes.io/ssl-redirect"    = "true"
      #   "nginx.ingress.kubernetes.io/force-ssl-redirect" = "true"
      #   "cert-manager.io/cluster-issuer"              = "letsencrypt-prod"
    }
  }

  spec {
    ingress_class_name = "nginx"
    rule {
      # sandbox_domain 应传入完整子域，例如 sbx.example.com
      # 则控制面访问地址为 api.sbx.example.com
      host = "api.${var.sandbox_domain}"
      http {
        path {
          path      = "/"
          path_type = "Prefix"
          backend {
            service {
              name = kubernetes_service.control_plane.metadata[0].name
              port { number = 80 }
            }
          }
        }
      }
    }
  }
}

# ---------- Outputs ----------

output "control_plane_service" {
  value = "http://${kubernetes_service.control_plane.metadata[0].name}.${kubernetes_namespace.sandbox_system.metadata[0].name}.svc.cluster.local"
}

output "control_plane_ingress_host" {
  value = var.expose_control_plane ? "https://api.${var.sandbox_domain} (需配 DNS CNAME → NLB + TLS + API_KEYS Secret)" : "disabled (use kubectl port-forward)"
}

output "sandbox_control_plane_role_arn" {
  value = aws_iam_role.control_plane.arn
}

output "node_agent_role_arn" {
  value = aws_iam_role.node_agent.arn
}

output "snapshot_bucket" {
  value = local.snapshot_bucket
}
