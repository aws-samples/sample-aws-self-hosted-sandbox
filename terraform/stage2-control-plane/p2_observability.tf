variable "enable_p2_observability" {
  type        = bool
  default     = false
  description = "Enable CloudWatch Logs, ADOT/X-Ray tracing, and managed Grafana provisioning."
}

variable "cloudwatch_log_retention_days" {
  type        = number
  default     = 30
  description = "Retention for centralized sandbox platform logs."
}

variable "fluent_bit_chart_version" {
  type        = string
  default     = "0.2.0"
  description = "Pinned aws-for-fluent-bit Helm chart version."
}

variable "adot_collector_image" {
  type        = string
  default     = "public.ecr.aws/aws-observability/aws-otel-collector:v0.43.1"
  description = "Pinned multi-architecture AWS Distro for OpenTelemetry collector image."
}

locals {
  p2_enabled             = var.enable_p2_observability
  platform_log_group     = "/aws/eks/${var.cluster_name}/sandbox-platform"
  otlp_http_endpoint     = "http://sandbox-adot-collector.monitoring.svc.cluster.local:4318"
  adot_service_account   = "sandbox-adot-collector"
  fluent_service_account = "sandbox-fluent-bit"
}

resource "terraform_data" "p2_requirements" {
  count = var.enable_p2_observability ? 1 : 0

  lifecycle {
    precondition {
      condition = (
        var.enable_observability_stack &&
        var.enable_amp_remote_write &&
        var.managed_grafana_workspace_id != ""
      )
      error_message = "enable_p2_observability requires the in-cluster stack, AMP remote-write, and an existing AMG workspace ID."
    }
  }
}

resource "aws_cloudwatch_log_group" "sandbox_platform" {
  count = local.p2_enabled ? 1 : 0

  name              = local.platform_log_group
  retention_in_days = var.cloudwatch_log_retention_days

  tags = {
    Name    = "${var.cluster_name}-sandbox-platform"
    Cluster = var.cluster_name
  }
}

resource "aws_iam_role" "fluent_bit" {
  count = local.p2_enabled ? 1 : 0

  name = "${var.cluster_name}-fluent-bit"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_issuer}:sub" = "system:serviceaccount:monitoring:${local.fluent_service_account}"
          "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "fluent_bit" {
  count = local.p2_enabled ? 1 : 0

  name = "cloudwatch-logs"
  role = aws_iam_role.fluent_bit[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "logs:CreateLogStream",
        "logs:DescribeLogStreams",
        "logs:PutLogEvents",
      ]
      Resource = "${aws_cloudwatch_log_group.sandbox_platform[0].arn}:*"
    }]
  })
}

resource "helm_release" "fluent_bit" {
  count = local.p2_enabled ? 1 : 0

  name       = "sandbox-fluent-bit"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-for-fluent-bit"
  version    = var.fluent_bit_chart_version
  namespace  = "monitoring"
  timeout    = 600
  wait       = true

  values = [yamlencode({
    fullnameOverride = "sandbox-fluent-bit"
    serviceAccount = {
      create = true
      name   = local.fluent_service_account
      annotations = {
        "eks.amazonaws.com/role-arn" = aws_iam_role.fluent_bit[0].arn
      }
    }
    cloudWatchLogs = {
      enabled         = true
      region          = var.region
      logGroupName    = aws_cloudwatch_log_group.sandbox_platform[0].name
      logStreamPrefix = "platform-"
      logKey          = "log"
      autoCreateGroup = false
    }
    additionalFilters = <<-EOT
      [FILTER]
          Name    grep
          Match   kube.*
          Regex   $kubernetes['namespace_name'] ^(sandbox-system|monitoring)$
    EOT
    tolerations = [{
      key      = "dedicated"
      operator = "Equal"
      value    = "sandbox"
      effect   = "NoSchedule"
    }]
    priorityClassName = "system-node-critical"
    resources = {
      requests = { cpu = "50m", memory = "64Mi" }
      limits   = { cpu = "250m", memory = "256Mi" }
    }
  })]

  depends_on = [
    helm_release.observability,
    aws_iam_role_policy.fluent_bit,
  ]
}

resource "aws_iam_role" "adot_collector" {
  count = local.p2_enabled ? 1 : 0

  name = "${var.cluster_name}-adot-collector"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_issuer}:sub" = "system:serviceaccount:monitoring:${local.adot_service_account}"
          "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "adot_collector" {
  count = local.p2_enabled ? 1 : 0

  name = "xray-write"
  role = aws_iam_role.adot_collector[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "xray:GetSamplingRules",
        "xray:GetSamplingTargets",
        "xray:GetSamplingStatisticSummaries",
      ]
      Resource = "*"
    }]
  })
}

resource "kubernetes_service_account" "adot_collector" {
  count = local.p2_enabled ? 1 : 0

  metadata {
    name      = local.adot_service_account
    namespace = "monitoring"
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.adot_collector[0].arn
    }
  }

  depends_on = [helm_release.observability]
}

resource "kubernetes_config_map" "adot_collector" {
  count = local.p2_enabled ? 1 : 0

  metadata {
    name      = "sandbox-adot-collector"
    namespace = "monitoring"
  }

  data = {
    "collector.yaml" = yamlencode({
      receivers = {
        otlp = {
          protocols = {
            http = { endpoint = "0.0.0.0:4318" }
          }
        }
      }
      processors = {
        batch = {}
        memory_limiter = {
          check_interval  = "1s"
          limit_mib       = 384
          spike_limit_mib = 96
        }
      }
      exporters = {
        awsxray = { region = var.region }
      }
      service = {
        pipelines = {
          traces = {
            receivers  = ["otlp"]
            processors = ["memory_limiter", "batch"]
            exporters  = ["awsxray"]
          }
        }
      }
    })
  }

  depends_on = [helm_release.observability]
}

resource "kubernetes_deployment" "adot_collector" {
  count = local.p2_enabled ? 1 : 0

  metadata {
    name      = "sandbox-adot-collector"
    namespace = "monitoring"
    labels    = { app = "sandbox-adot-collector" }
  }
  spec {
    replicas = 2
    selector { match_labels = { app = "sandbox-adot-collector" } }
    template {
      metadata { labels = { app = "sandbox-adot-collector" } }
      spec {
        service_account_name = kubernetes_service_account.adot_collector[0].metadata[0].name
        node_selector        = { "workload-tier" = "system" }
        container {
          name  = "collector"
          image = var.adot_collector_image
          args  = ["--config=/conf/collector.yaml"]
          port {
            name           = "otlp-http"
            container_port = 4318
          }
          resources {
            requests = { cpu = "100m", memory = "128Mi" }
            limits   = { cpu = "500m", memory = "512Mi" }
          }
          readiness_probe {
            tcp_socket { port = 4318 }
            initial_delay_seconds = 5
            period_seconds        = 10
          }
          volume_mount {
            name       = "config"
            mount_path = "/conf"
            read_only  = true
          }
        }
        volume {
          name = "config"
          config_map {
            name = kubernetes_config_map.adot_collector[0].metadata[0].name
          }
        }
      }
    }
  }
}

resource "kubernetes_service" "adot_collector" {
  count = local.p2_enabled ? 1 : 0

  metadata {
    name      = "sandbox-adot-collector"
    namespace = "monitoring"
  }
  spec {
    selector = { app = "sandbox-adot-collector" }
    port {
      name        = "otlp-http"
      port        = 4318
      target_port = 4318
    }
  }
}

resource "null_resource" "managed_grafana_configuration" {
  count = local.p2_enabled && var.managed_grafana_workspace_id != "" ? 1 : 0

  triggers = {
    workspace_id  = var.managed_grafana_workspace_id
    amp_endpoint  = aws_prometheus_workspace.sandbox[0].prometheus_endpoint
    dashboard_sha = sha256(jsonencode(local.sandbox_dashboard))
    script_sha    = filesha256("${path.module}/../../scripts/configure-managed-grafana.sh")
    region        = var.region
  }

  provisioner "local-exec" {
    command = "${path.module}/../../scripts/configure-managed-grafana.sh"
    environment = {
      AMG_WORKSPACE_ID = var.managed_grafana_workspace_id
      AMP_ENDPOINT     = aws_prometheus_workspace.sandbox[0].prometheus_endpoint
      AWS_REGION       = var.region
      DASHBOARD_FILE   = "sandbox-platform.json"
    }
  }

  depends_on = [
    aws_iam_role_policy.managed_grafana_amp_query,
    kubernetes_config_map.sandbox_grafana_dashboard,
  ]
}

output "cloudwatch_platform_log_group" {
  value = local.p2_enabled ? aws_cloudwatch_log_group.sandbox_platform[0].name : "disabled"
}

output "adot_otlp_http_endpoint" {
  value = local.p2_enabled ? local.otlp_http_endpoint : "disabled"
}
