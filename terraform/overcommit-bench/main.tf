# 超分 + Spot 快照真机测试的临时辅助资源（与 phase3/stage2 解耦，测完整栈 destroy）
#
#   - 镜像构建机：amd64 AL2023 + docker/buildx，原生构建 node-agent(amd64)，
#     qemu 构建控制面(arm64)，经 SSM 驱动，构建上下文走 S3。
#   - FSx for Lustre（可选，fsx_enabled=true）：快照写共享并行文件系统的对照组。
#
# 所有资源带 Project=claude-sbx / Purpose=overcommit-spot-bench / BenchTest=<test_id>，
# 删除前按 BenchTest tag 审核。

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
  default_tags {
    tags = local.common_tags
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

variable "test_id" {
  type        = string
  description = "本轮测试 id，用于命名与清理 tag。"
  validation {
    condition     = can(regex("^[a-z0-9-]{3,24}$", var.test_id))
    error_message = "test_id must be 3..24 lowercase letters, digits, or hyphens."
  }
}

variable "sandbox_node_group_name" {
  type        = string
  description = "现有 sandbox 节点组，取其子网（单 AZ）作为构建机/FSx 所在子网。"
}

variable "artifact_bucket" {
  type        = string
  description = "存放构建上下文与测试结果的 S3 桶（复用快照桶）。"
}

variable "build_host_enabled" {
  type    = bool
  default = true
}

variable "build_instance_type" {
  type    = string
  default = "c7i.4xlarge"
}

variable "fsx_enabled" {
  type    = bool
  default = false
}

variable "fsx_storage_capacity_gib" {
  type        = number
  default     = 2400
  description = "PERSISTENT_2 SSD 容量（1200 GiB 起，2400 GiB 递增）。"
}

variable "fsx_throughput_per_tib" {
  type        = number
  default     = 1000
  description = "PERSISTENT_2 每 TiB 吞吐（125/250/500/1000 MB/s/TiB）。"
}

data "aws_caller_identity" "current" {}

data "aws_eks_cluster" "this" {
  name = var.cluster_name
}

data "aws_eks_node_group" "sandbox" {
  cluster_name    = var.cluster_name
  node_group_name = var.sandbox_node_group_name
}

data "aws_subnet" "sandbox" {
  id = one(data.aws_eks_node_group.sandbox.subnet_ids)
}

data "aws_vpc" "this" {
  id = data.aws_eks_cluster.this.vpc_config[0].vpc_id
}

data "aws_ssm_parameter" "al2023_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

locals {
  common_tags = {
    ManagedBy = "terraform"
    Project   = "claude-sbx"
    Purpose   = "overcommit-spot-bench"
    BenchTest = var.test_id
  }
}

# ---------- 构建机 ----------

resource "aws_iam_role" "build" {
  count       = var.build_host_enabled ? 1 : 0
  name_prefix = "sbx-bench-build-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "build_ssm" {
  count      = var.build_host_enabled ? 1 : 0
  role       = aws_iam_role.build[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "build_artifacts" {
  count = var.build_host_enabled ? 1 : 0
  name  = "bench-artifacts-and-ecr-push"
  role  = aws_iam_role.build[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = ["arn:aws:s3:::${var.artifact_bucket}", "arn:aws:s3:::${var.artifact_bucket}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload",
          "ecr:DescribeRepositories", "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload",
          "ecr:PutImage", "ecr:UploadLayerPart",
        ]
        Resource = [
          "arn:aws:ecr:${var.region}:${data.aws_caller_identity.current.account_id}:repository/node-agent",
          "arn:aws:ecr:${var.region}:${data.aws_caller_identity.current.account_id}:repository/sandbox-control-plane",
        ]
      },
    ]
  })
}

resource "aws_iam_instance_profile" "build" {
  count       = var.build_host_enabled ? 1 : 0
  name_prefix = "sbx-bench-build-"
  role        = aws_iam_role.build[0].name
}

resource "aws_security_group" "bench" {
  name_prefix = "sbx-bench-${var.test_id}-"
  description = "overcommit/spot bench: egress for build host, Lustre within VPC"
  vpc_id      = data.aws_vpc.this.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Lustre: 988 + 1018-1023（客户端与文件系统同 SG / VPC 内互通）
  ingress {
    from_port   = 988
    to_port     = 988
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.this.cidr_block]
  }
  ingress {
    from_port   = 1018
    to_port     = 1023
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.this.cidr_block]
  }
}

resource "aws_instance" "build" {
  count                  = var.build_host_enabled ? 1 : 0
  ami                    = data.aws_ssm_parameter.al2023_ami.value
  instance_type          = var.build_instance_type
  subnet_id              = data.aws_subnet.sandbox.id
  vpc_security_group_ids = [aws_security_group.bench.id]
  iam_instance_profile   = aws_iam_instance_profile.build[0].name

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 100
    delete_on_termination = true
    encrypted             = true
  }

  user_data = <<-EOF
    #!/bin/bash
    set -euxo pipefail
    dnf install -y docker git tar gzip jq
    systemctl enable --now docker
    mkdir -p /usr/local/lib/docker/cli-plugins
    curl -sfL -o /usr/local/lib/docker/cli-plugins/docker-buildx \
      https://github.com/docker/buildx/releases/download/v0.19.3/buildx-v0.19.3.linux-amd64
    chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx
    docker run --privileged --rm tonistiigi/binfmt --install arm64 || true
    touch /var/lib/bench-build-ready
  EOF

  tags = {
    Name = "sbx-bench-build-${var.test_id}"
  }
  volume_tags = merge(local.common_tags, {
    Name = "sbx-bench-build-${var.test_id}"
  })

  depends_on = [aws_iam_role_policy_attachment.build_ssm]
}

# ---------- FSx for Lustre（可选对照组）----------

resource "aws_fsx_lustre_file_system" "bench" {
  count                       = var.fsx_enabled ? 1 : 0
  deployment_type             = "PERSISTENT_2"
  storage_type                = "SSD"
  storage_capacity            = var.fsx_storage_capacity_gib
  per_unit_storage_throughput = var.fsx_throughput_per_tib
  subnet_ids                  = [data.aws_subnet.sandbox.id]
  security_group_ids          = [aws_security_group.bench.id]
  data_compression_type       = "NONE"
  file_system_type_version    = "2.15"

  tags = {
    Name = "sbx-bench-fsx-${var.test_id}"
  }
}

output "build_instance_id" {
  value = var.build_host_enabled ? aws_instance.build[0].id : ""
}

output "bench_security_group_id" {
  value = aws_security_group.bench.id
}

output "subnet_id" {
  value = data.aws_subnet.sandbox.id
}

output "availability_zone" {
  value = data.aws_subnet.sandbox.availability_zone
}

output "fsx_dns_name" {
  value = var.fsx_enabled ? aws_fsx_lustre_file_system.bench[0].dns_name : ""
}

output "fsx_mount_name" {
  value = var.fsx_enabled ? aws_fsx_lustre_file_system.bench[0].mount_name : ""
}

output "cleanup_tag_filter" {
  value = "Name=tag:BenchTest,Values=${var.test_id}"
}
