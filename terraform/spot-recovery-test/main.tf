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

variable "source_node_group_name" {
  type        = string
  description = "Existing sandbox node group used only as the bootstrap/IAM/network reference."
}

variable "test_id" {
  type        = string
  description = "Short unique test run id used in names and cleanup tags."
  validation {
    condition     = can(regex("^[a-z0-9-]{3,20}$", var.test_id))
    error_message = "test_id must be 3..20 lowercase letters, digits, or hyphens."
  }
}

variable "active_instance_type" {
  type    = string
  default = "r8i.8xlarge"
}

variable "active_capacity_type" {
  type        = string
  default     = "SPOT"
  description = "SPOT exercises interruption recovery; ON_DEMAND provides a deterministic full-load throughput baseline."
  validation {
    condition = contains(
      ["SPOT", "ON_DEMAND"],
      var.active_capacity_type,
    )
    error_message = "active_capacity_type must be SPOT or ON_DEMAND."
  }
}

variable "standby_instance_type" {
  type        = string
  default     = ""
  description = "Defaults to active_instance_type so the standby can restore the full source memory load."
}

variable "state_ebs_size_gb" {
  type    = number
  default = 400
}

variable "state_ebs_iops" {
  type    = number
  default = 8000
  validation {
    condition = (
      var.state_ebs_iops >= 3000 &&
      var.state_ebs_iops <= 80000
    )
    error_message = "state_ebs_iops must be in 3000..80000."
  }
}

variable "state_ebs_throughput" {
  type    = number
  default = 2000
  validation {
    condition = (
      var.state_ebs_throughput >= 125 &&
      var.state_ebs_throughput <= 2000 &&
      var.state_ebs_throughput <= var.state_ebs_iops / 4
    )
    error_message = "state_ebs_throughput must be 125..2000 MiB/s and <= state_ebs_iops / 4."
  }
}

variable "ebs_bandwidth_weighting" {
  type    = string
  default = "default"
  validation {
    condition = contains(
      ["default", "ebs-1"],
      var.ebs_bandwidth_weighting,
    )
    error_message = "ebs_bandwidth_weighting must be default or ebs-1."
  }
}

variable "standby_max_size" {
  type    = number
  default = 4
}

data "aws_eks_cluster" "this" {
  name = var.cluster_name
}

data "aws_caller_identity" "current" {}

data "aws_eks_node_group" "source" {
  cluster_name    = var.cluster_name
  node_group_name = var.source_node_group_name
}

data "aws_launch_template" "source" {
  id = data.aws_eks_node_group.source.launch_template[0].id
}

locals {
  standby_instance_type = (
    var.standby_instance_type != ""
    ? var.standby_instance_type
    : var.active_instance_type
  )
  active_group_name  = "sbx-spot-test-${var.test_id}"
  standby_group_name = "sbx-standby-test-${var.test_id}"
  common_tags = {
    Project          = "claude-sbx"
    Purpose          = "spot-recovery-test"
    SpotRecoveryTest = var.test_id
    ManagedBy        = "terraform"
  }
  network_performance_options = (
    var.ebs_bandwidth_weighting == "default"
    ? []
    : [{ bandwidth_weighting = var.ebs_bandwidth_weighting }]
  )
}

resource "aws_launch_template" "active" {
  name_prefix            = "${local.active_group_name}-"
  update_default_version = true
  user_data              = data.aws_launch_template.source.user_data
  vpc_security_group_ids = data.aws_launch_template.source.vpc_security_group_ids

  cpu_options {
    nested_virtualization = "enabled"
  }

  dynamic "network_performance_options" {
    for_each = local.network_performance_options
    content {
      bandwidth_weighting = network_performance_options.value.bandwidth_weighting
    }
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 1
    http_tokens                 = "required"
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      delete_on_termination = true
      volume_size           = 200
      volume_type           = "gp3"
    }
  }

  block_device_mappings {
    device_name = "/dev/sdf"
    ebs {
      # Ordinary failed launch/replacement must not leak a 400 GiB volume.
      # node-agent flips only an interrupted, checkpointed attachment to false.
      delete_on_termination = true
      iops                  = var.state_ebs_iops
      throughput            = var.state_ebs_throughput
      volume_size           = var.state_ebs_size_gb
      volume_type           = "gp3"
    }
  }

  tag_specifications {
    resource_type = "instance"
    tags          = merge(local.common_tags, { Name = local.active_group_name })
  }

  tag_specifications {
    resource_type = "network-interface"
    tags          = merge(local.common_tags, { Name = local.active_group_name })
  }

  tag_specifications {
    resource_type = "volume"
    tags          = merge(local.common_tags, { Name = local.active_group_name })
  }

  tags = local.common_tags
}

resource "aws_launch_template" "standby" {
  name_prefix            = "${local.standby_group_name}-"
  update_default_version = true
  user_data              = data.aws_launch_template.source.user_data
  vpc_security_group_ids = data.aws_launch_template.source.vpc_security_group_ids

  cpu_options {
    nested_virtualization = "enabled"
  }

  dynamic "network_performance_options" {
    for_each = local.network_performance_options
    content {
      bandwidth_weighting = network_performance_options.value.bandwidth_weighting
    }
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 1
    http_tokens                 = "required"
  }

  # No dedicated state disk: the recovery controller attaches the old Spot
  # host's surviving EBS volume after the checkpoint finishes.
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      delete_on_termination = true
      volume_size           = 200
      volume_type           = "gp3"
    }
  }

  tag_specifications {
    resource_type = "instance"
    tags          = merge(local.common_tags, { Name = local.standby_group_name })
  }

  tag_specifications {
    resource_type = "network-interface"
    tags          = merge(local.common_tags, { Name = local.standby_group_name })
  }

  tag_specifications {
    resource_type = "volume"
    tags          = merge(local.common_tags, { Name = local.standby_group_name })
  }

  tags = local.common_tags
}

resource "aws_eks_node_group" "active" {
  cluster_name    = var.cluster_name
  node_group_name = local.active_group_name
  node_role_arn   = data.aws_eks_node_group.source.node_role_arn
  subnet_ids      = [one(data.aws_eks_node_group.source.subnet_ids)]
  version         = data.aws_eks_cluster.this.version
  ami_type        = "AL2023_x86_64_STANDARD"
  capacity_type   = var.active_capacity_type
  instance_types  = [var.active_instance_type]

  launch_template {
    id      = aws_launch_template.active.id
    version = tostring(aws_launch_template.active.latest_version)
  }

  scaling_config {
    desired_size = 1
    max_size     = 1
    min_size     = 1
  }

  update_config {
    max_unavailable = 1
  }

  labels = {
    role                                 = "sandbox"
    sandbox                              = "true"
    "workload-tier"                      = "spot-recovery-test"
    "sandbox.memorion.ai/recovery-role"  = "active"
    "sandbox.memorion.ai/recovery-group" = local.standby_group_name
  }

  taint {
    key    = "dedicated"
    value  = "sandbox"
    effect = "NO_SCHEDULE"
  }

  force_update_version = true
  tags                 = local.common_tags
}

resource "aws_eks_node_group" "standby" {
  cluster_name    = var.cluster_name
  node_group_name = local.standby_group_name
  node_role_arn   = data.aws_eks_node_group.source.node_role_arn
  subnet_ids      = [one(data.aws_eks_node_group.source.subnet_ids)]
  version         = data.aws_eks_cluster.this.version
  ami_type        = "AL2023_x86_64_STANDARD"
  capacity_type   = "ON_DEMAND"
  instance_types  = [local.standby_instance_type]

  launch_template {
    id      = aws_launch_template.standby.id
    version = tostring(aws_launch_template.standby.latest_version)
  }

  scaling_config {
    desired_size = 1
    max_size     = var.standby_max_size
    min_size     = 1
  }

  update_config {
    max_unavailable = 1
  }

  labels = {
    role                                    = "sandbox"
    sandbox                                 = "true"
    "workload-tier"                         = "spot-recovery-test"
    "sandbox.memorion.ai/recovery-role"     = "standby"
    "sandbox.memorion.ai/recovery-group"    = local.standby_group_name
    "sandbox.memorion.ai/recovery-az-index" = "0"
  }

  taint {
    key    = "dedicated"
    value  = "sandbox"
    effect = "NO_SCHEDULE"
  }

  force_update_version = true
  tags                 = local.common_tags
}

resource "aws_iam_role" "fis_spot_interruption" {
  name_prefix = "fis-${var.test_id}-"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "fis.amazonaws.com"
      }
      Action = "sts:AssumeRole"
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = data.aws_caller_identity.current.account_id
        }
        ArnLike = {
          "aws:SourceArn" = "arn:aws:fis:${var.region}:${data.aws_caller_identity.current.account_id}:experiment/*"
        }
      }
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy" "fis_spot_interruption" {
  name = "interrupt-tagged-test-spot-instance"
  role = aws_iam_role.fis_spot_interruption.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:SendSpotInstanceInterruptions"]
        Resource = "arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = {
          StringEquals = {
            "ec2:ResourceTag/SpotRecoveryTest" = var.test_id
          }
        }
      },
    ]
  })
}

resource "aws_fis_experiment_template" "spot_interruption" {
  description = "Interrupt exactly one running Spot instance for ${var.test_id}"
  role_arn    = aws_iam_role.fis_spot_interruption.arn

  stop_condition {
    source = "none"
  }

  target {
    name           = "oneTestSpotInstance"
    resource_type  = "aws:ec2:spot-instance"
    selection_mode = "COUNT(1)"

    resource_tag {
      key   = "SpotRecoveryTest"
      value = var.test_id
    }

    filter {
      path   = "State.Name"
      values = ["running"]
    }
  }

  action {
    name      = "interruptSpotInstance"
    action_id = "aws:ec2:send-spot-instance-interruptions"

    parameter {
      key   = "durationBeforeInterruption"
      value = "PT2M"
    }

    target {
      key   = "SpotInstances"
      value = "oneTestSpotInstance"
    }
  }

  tags = merge(local.common_tags, {
    Name = "fis-${var.test_id}-spot-interruption"
  })

  depends_on = [aws_iam_role_policy.fis_spot_interruption]
}

output "test_id" {
  value = var.test_id
}

output "active_node_group_name" {
  value = aws_eks_node_group.active.node_group_name
}

output "standby_node_group_name" {
  value = aws_eks_node_group.standby.node_group_name
}

output "subnet_id" {
  value = one(data.aws_eks_node_group.source.subnet_ids)
}

output "cleanup_volume_filter" {
  value = "Name=tag:SpotRecoveryTest,Values=${var.test_id}"
}

output "fis_experiment_template_id" {
  value = aws_fis_experiment_template.spot_interruption.id
}
