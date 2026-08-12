variable "enable_observability_stack" {
  type        = bool
  default     = false
  description = "Install Prometheus, Alertmanager, Grafana, sandbox scrape targets, and P1 alert rules."
}

variable "observability_chart_version" {
  type        = string
  default     = "88.3.0"
  description = "Pinned kube-prometheus-stack chart version."
}

variable "grafana_admin_password" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Grafana admin password. Required when enable_observability_stack=true."
}

variable "enable_amp_remote_write" {
  type        = bool
  default     = false
  description = "Create an Amazon Managed Service for Prometheus workspace and remote-write cluster metrics to it."
}

variable "managed_grafana_workspace_id" {
  type        = string
  default     = ""
  description = "Existing Amazon Managed Grafana workspace ID granted read access to the managed Prometheus workspace."
}

variable "managed_grafana_vpc_id" {
  type        = string
  default     = ""
  description = "VPC used by the managed Grafana workspace. When set, create an AMP query interface endpoint in this VPC."
}

variable "managed_grafana_subnet_ids" {
  type        = list(string)
  default     = []
  description = "Subnets for the managed Grafana AMP interface endpoint."
}

variable "managed_grafana_security_group_id" {
  type        = string
  default     = ""
  description = "Security group attached to managed Grafana ENIs and allowed to reach the AMP interface endpoint."
}

resource "aws_prometheus_workspace" "sandbox" {
  count = var.enable_amp_remote_write ? 1 : 0

  alias = "${var.cluster_name}-observability"
  tags = {
    Name    = "${var.cluster_name}-observability"
    Cluster = var.cluster_name
  }

  lifecycle {
    precondition {
      condition     = var.enable_observability_stack
      error_message = "enable_amp_remote_write requires enable_observability_stack=true."
    }
  }
}

resource "aws_iam_role" "prometheus_remote_write" {
  count = var.enable_amp_remote_write ? 1 : 0

  name = "${var.cluster_name}-prometheus-remote-write"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_issuer}:sub" = "system:serviceaccount:monitoring:sandbox-monitoring-prometheus"
          "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "prometheus_remote_write" {
  count = var.enable_amp_remote_write ? 1 : 0

  name = "amp-remote-write"
  role = aws_iam_role.prometheus_remote_write[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["aps:RemoteWrite"]
      Resource = aws_prometheus_workspace.sandbox[0].arn
    }]
  })
}

data "aws_grafana_workspace" "managed" {
  count = var.enable_amp_remote_write && var.managed_grafana_workspace_id != "" ? 1 : 0

  workspace_id = var.managed_grafana_workspace_id
}

resource "aws_iam_role_policy" "managed_grafana_amp_query" {
  count = var.enable_amp_remote_write && var.managed_grafana_workspace_id != "" ? 1 : 0

  name = "${var.cluster_name}-amp-query"
  role = basename(data.aws_grafana_workspace.managed[0].role_arn)
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "aps:GetLabels",
        "aps:GetMetricMetadata",
        "aps:GetSeries",
        "aps:QueryMetrics",
      ]
      Resource = aws_prometheus_workspace.sandbox[0].arn
    }]
  })
}

data "aws_vpc" "managed_grafana" {
  count = var.enable_amp_remote_write && var.managed_grafana_vpc_id != "" ? 1 : 0

  id = var.managed_grafana_vpc_id
}

resource "aws_security_group" "managed_grafana_amp_endpoint" {
  count = var.enable_amp_remote_write && var.managed_grafana_vpc_id != "" ? 1 : 0

  name        = "${var.cluster_name}-amg-amp-endpoint"
  description = "Allow Amazon Managed Grafana to query AMP through PrivateLink"
  vpc_id      = var.managed_grafana_vpc_id

  ingress {
    description     = "HTTPS from the managed Grafana workspace"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [var.managed_grafana_security_group_id]
    cidr_blocks     = [data.aws_vpc.managed_grafana[0].cidr_block]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "${var.cluster_name}-amg-amp-endpoint"
    Cluster = var.cluster_name
  }

  lifecycle {
    precondition {
      condition     = length(var.managed_grafana_subnet_ids) > 0 && var.managed_grafana_security_group_id != ""
      error_message = "managed_grafana_subnet_ids and managed_grafana_security_group_id are required with managed_grafana_vpc_id."
    }
  }
}

resource "aws_vpc_endpoint" "managed_grafana_amp" {
  count = var.enable_amp_remote_write && var.managed_grafana_vpc_id != "" ? 1 : 0

  vpc_id              = var.managed_grafana_vpc_id
  service_name        = "com.amazonaws.${var.region}.aps-workspaces"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = var.managed_grafana_subnet_ids
  security_group_ids  = [aws_security_group.managed_grafana_amp_endpoint[0].id]
  private_dns_enabled = true

  tags = {
    Name    = "${var.cluster_name}-amg-amp"
    Cluster = var.cluster_name
  }
}

locals {
  sandbox_alert_rules = {
    groups = [
      {
        name = "sandbox-platform"
        rules = [
          {
            alert = "SandboxWakeLatencyHigh"
            expr  = "histogram_quantile(0.95, sum by (le) (rate(wake_rpc_duration_seconds_bucket[10m]))) > 10"
            for   = "10m"
            labels = {
              severity = "warning"
              category = "user-experience"
            }
            annotations = {
              summary     = "Sandbox wake p95 latency is above 10 seconds"
              description = "Inspect resume queue wait and Firecracker resume stage histograms."
            }
          },
          {
            alert = "SandboxSnapshotIntegrityFailure"
            expr  = "increase(fc_snapshot_verify_total{result=\"error\"}[5m]) > 0 or increase(fc_snapshot_errors_total[5m]) > 0"
            for   = "0m"
            labels = {
              severity = "critical"
              category = "data-safety"
            }
            annotations = {
              summary     = "A sandbox snapshot failed integrity verification"
              description = "Do not retry restore until the affected snapshot source is inspected."
            }
          },
          {
            alert = "SandboxNodeCapacityLow"
            expr  = "fcnode_free_memory_bytes < 17179869184 or (fcnode_scratch_bytes{kind=\"free\"} / fcnode_scratch_bytes{kind=\"total\"}) < 0.10"
            for   = "10m"
            labels = {
              severity = "warning"
              category = "capacity"
            }
            annotations = {
              summary     = "Sandbox node memory or state disk capacity is low"
              description = "Drain new placements or add sandbox-node capacity."
            }
          },
          {
            alert = "SandboxOrphanGrowth"
            expr  = "increase(reconcile_actions_total{action=\"orphaned\"}[15m]) > 0"
            for   = "0m"
            labels = {
              severity = "warning"
              category = "resource-leak"
            }
            annotations = {
              summary     = "Reconcile is finding orphaned sandboxes"
              description = "Inspect node health and Firecracker runtime drift."
            }
          },
          {
            alert = "SandboxControlPlaneDegraded"
            expr  = "increase(background_loop_runs_total{result=\"error\"}[5m]) > 0 or increase(leader_transitions_total[15m]) > 4 or min(up{namespace=\"sandbox-system\",service=\"sandbox-control-plane\"}) < 1"
            for   = "2m"
            labels = {
              severity = "critical"
              category = "control-plane"
            }
            annotations = {
              summary     = "Sandbox control plane is degraded"
              description = "Check scrape health, leader churn, and background-loop errors."
            }
          },
        ]
      },
    ]
  }

  sandbox_dashboard = {
    uid           = "sandbox-platform"
    title         = "Sandbox Platform"
    tags          = ["sandbox", "firecracker"]
    timezone      = "browser"
    schemaVersion = 39
    version       = 1
    refresh       = "15s"
    time = {
      from = "now-1h"
      to   = "now"
    }
    templating = { list = [] }
    panels = [
      {
        id      = 1
        type    = "stat"
        title   = "Running microVMs"
        gridPos = { x = 0, y = 0, w = 6, h = 4 }
        targets = [{ refId = "A", expr = "sum(fc_vms{state=\"running\"})" }]
      },
      {
        id          = 2
        type        = "stat"
        title       = "Node free memory"
        gridPos     = { x = 6, y = 0, w = 6, h = 4 }
        fieldConfig = { defaults = { unit = "bytes" }, overrides = [] }
        targets     = [{ refId = "A", expr = "min(fcnode_free_memory_bytes)" }]
      },
      {
        id      = 3
        type    = "gauge"
        title   = "State disk used"
        gridPos = { x = 12, y = 0, w = 6, h = 4 }
        fieldConfig = {
          defaults  = { unit = "percentunit", min = 0, max = 1 }
          overrides = []
        }
        targets = [{ refId = "A", expr = "1 - (fcnode_scratch_bytes{kind=\"free\"} / fcnode_scratch_bytes{kind=\"total\"})" }]
      },
      {
        id      = 4
        type    = "stat"
        title   = "Snapshot verification errors"
        gridPos = { x = 18, y = 0, w = 6, h = 4 }
        targets = [{ refId = "A", expr = "sum(increase(fc_snapshot_verify_total{result=\"error\"}[1h]))" }]
      },
      {
        id          = 5
        type        = "timeseries"
        title       = "Lifecycle operation p95"
        gridPos     = { x = 0, y = 4, w = 12, h = 8 }
        fieldConfig = { defaults = { unit = "s" }, overrides = [] }
        targets     = [{ refId = "A", expr = "histogram_quantile(0.95, sum by (operation, le) (rate(fc_operation_duration_seconds_bucket[5m])))", legendFormat = "{{operation}}" }]
      },
      {
        id          = 6
        type        = "timeseries"
        title       = "Resume stages p95"
        gridPos     = { x = 12, y = 4, w = 12, h = 8 }
        fieldConfig = { defaults = { unit = "s" }, overrides = [] }
        targets     = [{ refId = "A", expr = "histogram_quantile(0.95, sum by (stage, le) (rate(fc_resume_stage_duration_seconds_bucket[5m])))", legendFormat = "{{stage}}" }]
      },
      {
        id          = 7
        type        = "timeseries"
        title       = "Control-plane request rate"
        gridPos     = { x = 0, y = 12, w = 12, h = 8 }
        fieldConfig = { defaults = { unit = "reqps" }, overrides = [] }
        targets     = [{ refId = "A", expr = "sum by (route, status_class) (rate(http_requests_total{namespace=\"sandbox-system\",service=\"sandbox-control-plane\"}[5m]))", legendFormat = "{{route}} {{status_class}}" }]
      },
      {
        id      = 8
        type    = "timeseries"
        title   = "Background loops"
        gridPos = { x = 12, y = 12, w = 12, h = 8 }
        targets = [{ refId = "A", expr = "sum by (loop, result) (rate(background_loop_runs_total[5m]))", legendFormat = "{{loop}} {{result}}" }]
      },
    ]
  }
}

resource "helm_release" "observability" {
  count = var.enable_observability_stack ? 1 : 0

  name             = "sandbox-monitoring"
  repository       = "https://prometheus-community.github.io/helm-charts"
  chart            = "kube-prometheus-stack"
  version          = var.observability_chart_version
  namespace        = "monitoring"
  create_namespace = true
  timeout          = 900
  wait             = true

  set_sensitive {
    name  = "grafana.adminPassword"
    value = var.grafana_admin_password
  }

  values = [yamlencode({
    fullnameOverride = "sandbox-monitoring"

    defaultRules = {
      create = true
    }
    kubeControllerManager = { enabled = false }
    kubeEtcd              = { enabled = false }
    kubeScheduler         = { enabled = false }

    prometheusOperator = {
      nodeSelector = { "workload-tier" = "system" }
      resources = {
        requests = { cpu = "100m", memory = "128Mi" }
        limits   = { cpu = "500m", memory = "512Mi" }
      }
    }

    prometheus = {
      serviceAccount = {
        annotations = var.enable_amp_remote_write ? {
          "eks.amazonaws.com/role-arn" = aws_iam_role.prometheus_remote_write[0].arn
        } : {}
      }
      prometheusSpec = {
        nodeSelector       = { "workload-tier" = "system" }
        retention          = "3d"
        scrapeInterval     = "15s"
        evaluationInterval = "15s"
        podMetadata = {
          annotations = var.enable_amp_remote_write ? {
            "sandbox.platform/amp-remote-write-role" = aws_iam_role.prometheus_remote_write[0].arn
          } : {}
        }
        remoteWrite = var.enable_amp_remote_write ? [
          {
            url = "${aws_prometheus_workspace.sandbox[0].prometheus_endpoint}api/v1/remote_write"
            sigv4 = {
              region = var.region
            }
            queueConfig = {
              capacity          = 2500
              maxSamplesPerSend = 1000
              maxShards         = 50
            }
          },
        ] : []
        resources = {
          requests = { cpu = "250m", memory = "512Mi" }
          limits   = { cpu = "1", memory = "2Gi" }
        }
      }
      additionalServiceMonitors = [
        {
          name = "sandbox-control-plane"
          selector = {
            matchLabels = { app = "sandbox-control-plane" }
          }
          namespaceSelector = { matchNames = ["sandbox-system"] }
          endpoints = [
            {
              port     = "metrics"
              path     = "/metrics"
              interval = "15s"
            },
          ]
        },
      ]
      additionalPodMonitors = [
        {
          name = "sandbox-node-agent"
          selector = {
            matchLabels = { app = "node-agent" }
          }
          namespaceSelector = { matchNames = ["sandbox-system"] }
          podMetricsEndpoints = [
            {
              port     = "metrics"
              path     = "/metrics"
              interval = "15s"
              relabelings = [
                {
                  sourceLabels = ["__meta_kubernetes_pod_node_name"]
                  targetLabel  = "kubernetes_node"
                },
              ]
            },
          ]
        },
      ]
    }

    alertmanager = {
      enabled = true
      alertmanagerSpec = {
        nodeSelector = { "workload-tier" = "system" }
        resources = {
          requests = { cpu = "50m", memory = "128Mi" }
          limits   = { cpu = "250m", memory = "256Mi" }
        }
      }
    }

    grafana = {
      enabled                  = true
      defaultDashboardsEnabled = false
      nodeSelector             = { "workload-tier" = "system" }
      resources = {
        requests = { cpu = "100m", memory = "128Mi" }
        limits   = { cpu = "500m", memory = "512Mi" }
      }
      sidecar = {
        dashboards = {
          enabled         = true
          label           = "grafana_dashboard"
          labelValue      = "1"
          searchNamespace = "ALL"
        }
      }
    }

    "kube-state-metrics" = {
      nodeSelector = { "workload-tier" = "system" }
    }
    "prometheus-node-exporter" = {
      tolerations = [
        {
          key      = "dedicated"
          operator = "Equal"
          value    = "sandbox"
          effect   = "NoSchedule"
        },
      ]
    }

    additionalPrometheusRulesMap = {
      sandbox-platform = local.sandbox_alert_rules
    }
  })]

  lifecycle {
    precondition {
      condition     = length(var.grafana_admin_password) >= 16
      error_message = "grafana_admin_password must contain at least 16 characters when observability is enabled."
    }
  }
}

resource "kubernetes_config_map" "sandbox_grafana_dashboard" {
  count = var.enable_observability_stack ? 1 : 0

  metadata {
    name      = "sandbox-platform-dashboard"
    namespace = "monitoring"
    labels = {
      grafana_dashboard = "1"
    }
  }

  data = {
    "sandbox-platform.json" = jsonencode(local.sandbox_dashboard)
  }

  depends_on = [helm_release.observability]
}

output "prometheus_port_forward" {
  value = var.enable_observability_stack ? "kubectl -n monitoring port-forward svc/sandbox-monitoring-prometheus 9090:9090" : "disabled"
}

output "grafana_port_forward" {
  value = var.enable_observability_stack ? "kubectl -n monitoring port-forward svc/sandbox-monitoring-grafana 3000:80" : "disabled"
}

output "alertmanager_port_forward" {
  value = var.enable_observability_stack ? "kubectl -n monitoring port-forward svc/sandbox-monitoring-alertmanager 9093:9093" : "disabled"
}

output "amp_workspace_id" {
  value = var.enable_amp_remote_write ? aws_prometheus_workspace.sandbox[0].id : "disabled"
}

output "amp_query_endpoint" {
  value = var.enable_amp_remote_write ? aws_prometheus_workspace.sandbox[0].prometheus_endpoint : "disabled"
}

output "managed_grafana_endpoint" {
  value = var.enable_amp_remote_write && var.managed_grafana_workspace_id != "" ? data.aws_grafana_workspace.managed[0].endpoint : "disabled"
}

output "managed_grafana_amp_vpc_endpoint_id" {
  value = var.enable_amp_remote_write && var.managed_grafana_vpc_id != "" ? aws_vpc_endpoint.managed_grafana_amp[0].id : "disabled"
}
