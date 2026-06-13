# Phase 3 基础设施 —— EKS 集群 + Graviton .metal 托管节点组(验 H3:Kata 编排 + 任意端口)
#
# 目标:用 Terraform 管理 EKS 控制平面 + 一个 c6g.metal 节点组(打 sandbox=true label)。
#       Kata 安装、RuntimeClass、ingress-nginx、ACM、测试 Pod 是集群内操作(kubectl/helm),不归此处。
#
# ⚠️ 计费:EKS 控制平面 $0.10/hr + c6g.metal 节点 $2.32/hr。用完务必 destroy。
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

variable "metal_instance_type" {
  type    = string
  default = "c6g.metal" # Graviton 裸金属,KVM 可用,Kata 才能跑
}

variable "endpoint_public_access_cidrs" {
  type        = list(string)
  description = "允许访问 EKS 公网 API endpoint 的来源 CIDR(必填,无默认值以避免误开全网)。收窄到自己的 IP,apply 时传入:terraform apply -var='endpoint_public_access_cidrs=[\"'$(curl -s https://checkip.amazonaws.com)'/32\"]'"
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
  }

  # 托管节点组:Graviton .metal,打 sandbox=true label 供 nodeSelector 用
  eks_managed_node_groups = {
    metal_arm64 = {
      ami_type       = "AL2023_ARM_64_STANDARD"
      instance_types = [var.metal_instance_type]
      min_size       = 1
      max_size       = 2
      desired_size   = 1

      # .metal 根盘留足:Kata 镜像 + devmapper(若用 FC 后端)+ 沙盒镜像
      block_device_mappings = {
        xvda = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size = 200
            volume_type = "gp3"
          }
        }
      }

      labels = {
        sandbox = "true"
      }
    }
  }

  # 节点角色加 Bedrock 调用权限(沙盒走节点凭据链调 Bedrock;生产改 IRSA/出口代理)
  node_security_group_tags = {
    "kubernetes.io/cluster/${var.cluster_name}" = null
  }
}

# Bedrock 权限已迁移到 LiteLLM IRSA(terraform/stage2-control-plane/litellm.tf)
# 节点角色不再持有 Bedrock 权限 —— 沙盒内代码无法直接调 Bedrock(R8 凭据隔离落地)
# 沙盒走: Claude Code → ANTHROPIC_BASE_URL=http://litellm.litellm:4000 → LiteLLM Pod → Bedrock

# ---------- ECR(复用 Phase 1 的也行;这里独立声明便于单独 apply) ----------
data "aws_ecr_repository" "sbx" {
  name = "claude-sbx"
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
