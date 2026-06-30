# Phase 1 基础设施 —— 单台 Graviton .metal 主机 + Bedrock 权限 + ECR
# 目标:把验证 H1(Claude Code 在 Firecracker microVM 内原生跑通)所需的最小 AWS 资源
#       全部用 Terraform 管理。Firecracker / rootfs / microVM 启动是主机内操作(见 POC 文档第 3 节)。
#
# 用法:
#   terraform init
#   terraform apply -var="my_ip_cidr=$(curl -s https://checkip.amazonaws.com)/32"
#   terraform output ssh_command
#
# 销毁(.metal 按小时计费,务必用完即毁):
#   terraform destroy

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

# ---------- 变量 ----------
variable "region" {
  type    = string
  default = "us-east-1"
}

variable "az" {
  type    = string
  default = "us-east-1a"
}

variable "node_arch" {
  type        = string
  default     = "arm64"
  description = "节点 CPU 架构:arm64(Graviton,默认) 或 amd64(Intel x86)。决定 AMI 与默认 .metal 机型。"
  validation {
    condition     = contains(["arm64", "amd64"], var.node_arch)
    error_message = "node_arch 仅支持 \"arm64\" 或 \"amd64\"。"
  }
}

variable "metal_instance_type" {
  type        = string
  default     = "" # 留空时按 node_arch 选默认机型(arm64→c6g.metal / amd64→c5n.metal)
  description = ".metal 实例类型。留空则由 node_arch 决定:arm64=c6g.metal,amd64=c5n.metal(最便宜 Intel x86 裸金属)。"
}

locals {
  # 架构 → (默认机型, AL2023 SSM 路径里的架构后缀)
  default_metal_by_arch = {
    arm64 = "c6g.metal"
    amd64 = "c5n.metal"
  }
  ssm_arch_suffix = {
    arm64 = "arm64"
    amd64 = "x86_64"
  }
  metal_type = var.metal_instance_type != "" ? var.metal_instance_type : local.default_metal_by_arch[var.node_arch]
}

variable "my_ip_cidr" {
  type        = string
  description = "允许 SSH 的来源 IP/CIDR,例如 1.2.3.4/32。apply 时传入。"
}

variable "public_key_path" {
  type        = string
  description = "本地 SSH 公钥路径"
  default     = "~/.ssh/claude-sbx-poc.pub"
}

variable "root_volume_gb" {
  type    = number
  default = 200 # rootfs 构建 + workspace 本地 ext4(Phase 1 全本地盘)
}

# ---------- 网络:用默认 VPC,最省事 ----------
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "availability-zone"
    values = [var.az]
  }
}

# ---------- 最新 AL2023 AMI(架构随 node_arch) ----------
data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-${local.ssm_arch_suffix[var.node_arch]}"
}

# ---------- 安全组:仅放行你的 IP 的 SSH ----------
resource "aws_security_group" "sbx" {
  name        = "claude-sbx-sg"
  description = "Claude sandbox POC host"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "SSH from my IP"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.my_ip_cidr]
  }

  egress {
    description = "all outbound (Bedrock / npm / git)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "claude-sbx-sg" }
}

# ---------- SSH key ----------
resource "aws_key_pair" "sbx" {
  key_name   = "claude-sbx-poc"
  public_key = file(pathexpand(var.public_key_path))
}

# ---------- IAM:主机角色,授予 Bedrock 调用权限(方式 B / IAM Role 鉴权) ----------
# 这样沙盒走宿主凭据链即可调 Bedrock,无需把长期 key 放进环境变量。
data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sbx_host" {
  name               = "claude-sbx-host-role"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

data "aws_iam_policy_document" "bedrock_invoke" {
  statement {
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    # 仅限 Anthropic 基础模型 + 跨区 inference profile
    resources = [
      "arn:aws:bedrock:*::foundation-model/anthropic.*",
      "arn:aws:bedrock:*:*:inference-profile/us.anthropic.*",
    ]
  }
}

resource "aws_iam_role_policy" "bedrock" {
  name   = "bedrock-invoke"
  role   = aws_iam_role.sbx_host.id
  policy = data.aws_iam_policy_document.bedrock_invoke.json
}

# ECR 推送权限:让主机能把构建好的 arm64 沙盒镜像推到 ECR(Phase 3 部署要用)
resource "aws_iam_role_policy" "ecr_push" {
  name = "ecr-push"
  role = aws_iam_role.sbx_host.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ecr:GetAuthorizationToken",
        "ecr:BatchCheckLayerAvailability",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage",
        "ecr:BatchGetImage",
      ]
      Resource = "*" # GetAuthorizationToken 必须 *;其余可收窄到具体 repo ARN
    }]
  })
}

# 便于排障:允许 SSM Session Manager 登录(可选,免开 22 口也能进)
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.sbx_host.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "sbx_host" {
  name = "claude-sbx-host-profile"
  role = aws_iam_role.sbx_host.name
}

# ---------- ECR:存沙盒镜像(Phase 3 推镜像用;Phase 1 也可本地构建) ----------
resource "aws_ecr_repository" "sbx" {
  name                 = "claude-sbx"
  image_tag_mutability = "MUTABLE"
  force_delete         = true # POC 方便清理
}

# ---------- .metal 主机 ----------
resource "aws_instance" "host" {
  ami                    = data.aws_ssm_parameter.al2023.value
  instance_type          = local.metal_type
  key_name               = aws_key_pair.sbx.key_name
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.sbx.id]
  iam_instance_profile   = aws_iam_instance_profile.sbx_host.name

  root_block_device {
    volume_size = var.root_volume_gb
    volume_type = "gp3"
  }

  # 强制 IMDSv2(http_tokens=required):阻断 SSRF 经 IMDSv1 窃取实例凭据
  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  # 开机装好 docker/git 依赖。.metal 的 cloud-init 偶发不可靠(实测有一次没装上 docker),
  # 故加重试 + 落完成标记;setup-host.sh 里也会再次防御性安装 docker(双保险)。
  user_data = <<-EOF
    #!/bin/bash
    set -ux
    for i in 1 2 3 4 5; do
      dnf install -y docker git && break || sleep 15
    done
    systemctl enable --now docker || true
    for i in $(seq 1 15); do docker info >/dev/null 2>&1 && break; sleep 3; done
    docker version > /var/log/userdata-docker.log 2>&1 || echo "WARN: docker 未就绪(setup-host.sh 会补装)"
    # 校验 KVM(关键前提)
    ls -l /dev/kvm > /var/log/userdata-kvm.log 2>&1 || echo "WARN: /dev/kvm missing — 选错实例?必须是 .metal"
    touch /var/log/userdata-done
  EOF

  tags = { Name = "claude-sbx-host" }
}

# ---------- 输出 ----------
output "instance_id" {
  value = aws_instance.host.id
}

output "public_ip" {
  value = aws_instance.host.public_ip
}

output "ssh_command" {
  value = "ssh -i ~/.ssh/claude-sbx-poc ec2-user@${aws_instance.host.public_ip}"
}

output "ecr_repo_url" {
  value = aws_ecr_repository.sbx.repository_url
}
