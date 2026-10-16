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

variable "test_id" {
  type        = string
  description = "Short identifier used for names, tags, and exact cleanup."

  validation {
    condition     = can(regex("^[a-z0-9-]{3,24}$", var.test_id))
    error_message = "test_id must be 3..24 lowercase letters, digits, or hyphens."
  }
}

variable "subnet_id" {
  type        = string
  description = "Subnet in which both benchmark instances are launched."
}

variable "security_group_id" {
  type        = string
  description = "Security group with outbound access for package installation and SSM."
}

variable "associate_public_ip_address" {
  type        = bool
  default     = false
  description = "Assign an ephemeral public IPv4 address when the subnet has an Internet Gateway but no NAT or SSM endpoints."
}

variable "instance_type" {
  type    = string
  default = "r8i.8xlarge"
}

variable "data_volume_size_gib" {
  type    = number
  default = 120
}

variable "data_volume_iops" {
  type    = number
  default = 8000
}

variable "data_volume_throughput" {
  type    = number
  default = 2000
}

data "aws_ssm_parameter" "al2023_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

locals {
  weightings = {
    default = "default"
    ebs1    = "ebs-1"
  }

  common_tags = {
    ManagedBy        = "terraform"
    Project          = "claude-sbx"
    Purpose          = "ebs-bandwidth-benchmark"
    EbsBenchmarkTest = var.test_id
  }
}

resource "aws_iam_role" "ssm" {
  name_prefix = "${var.test_id}-ssm-"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.ssm.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ssm" {
  name_prefix = "${var.test_id}-"
  role        = aws_iam_role.ssm.name

  tags = local.common_tags
}

resource "aws_launch_template" "bench" {
  for_each = local.weightings

  name_prefix   = "${var.test_id}-${each.key}-"
  image_id      = data.aws_ssm_parameter.al2023_ami.value
  instance_type = var.instance_type

  user_data = base64encode(join("\n", [
    "#!/bin/bash",
    "export BENCH_WEIGHTING='${each.value}'",
    "export BENCH_INSTANCE_TYPE='${var.instance_type}'",
    file("${path.module}/benchmark.sh"),
  ]))

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  iam_instance_profile {
    name = aws_iam_instance_profile.ssm.name
  }

  network_interfaces {
    device_index                = 0
    delete_on_termination       = true
    subnet_id                   = var.subnet_id
    security_groups             = [var.security_group_id]
    associate_public_ip_address = var.associate_public_ip_address
  }

  dynamic "network_performance_options" {
    for_each = each.value == "default" ? [] : [each.value]

    content {
      bandwidth_weighting = network_performance_options.value
    }
  }

  block_device_mappings {
    device_name = "/dev/xvda"

    ebs {
      volume_type           = "gp3"
      volume_size           = 30
      delete_on_termination = true
      encrypted             = true
    }
  }

  block_device_mappings {
    device_name = "/dev/sdf"

    ebs {
      volume_type           = "gp3"
      volume_size           = var.data_volume_size_gib
      iops                  = var.data_volume_iops
      throughput            = var.data_volume_throughput
      delete_on_termination = true
      encrypted             = true
    }
  }

  block_device_mappings {
    device_name = "/dev/sdg"

    ebs {
      volume_type           = "gp3"
      volume_size           = var.data_volume_size_gib
      iops                  = var.data_volume_iops
      throughput            = var.data_volume_throughput
      delete_on_termination = true
      encrypted             = true
    }
  }

  tag_specifications {
    resource_type = "instance"
    tags = merge(local.common_tags, {
      Name               = "${var.test_id}-${each.key}"
      BenchmarkWeighting = each.value
    })
  }

  tag_specifications {
    resource_type = "volume"
    tags = merge(local.common_tags, {
      Name               = "${var.test_id}-${each.key}"
      BenchmarkWeighting = each.value
    })
  }

  tag_specifications {
    resource_type = "network-interface"
    tags = merge(local.common_tags, {
      Name               = "${var.test_id}-${each.key}"
      BenchmarkWeighting = each.value
    })
  }

  tags = merge(local.common_tags, {
    BenchmarkWeighting = each.value
  })

  depends_on = [aws_iam_role_policy_attachment.ssm]
}

resource "aws_instance" "bench" {
  for_each = local.weightings

  launch_template {
    id      = aws_launch_template.bench[each.key].id
    version = tostring(aws_launch_template.bench[each.key].latest_version)
  }

  tags = merge(local.common_tags, {
    Name               = "${var.test_id}-${each.key}"
    BenchmarkWeighting = each.value
  })
}

output "instances" {
  value = {
    for key, instance in aws_instance.bench :
    key => {
      id         = instance.id
      private_ip = instance.private_ip
      weighting  = local.weightings[key]
    }
  }
}

output "result_path" {
  value = "/var/lib/ebs-bandwidth-benchmark/result.json"
}
