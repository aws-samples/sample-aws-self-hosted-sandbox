# LiteLLM 统一 AI 网关 —— 凭据隔离落地(R8)
#
# 沙盒内 Claude Code 不再直连 Bedrock;统一走集群内 LiteLLM:
#   Sandbox → http://litellm.litellm:4000 → Bedrock
# 凭据(Bedrock 调用权限)只在 LiteLLM Pod 的 IRSA 角色里,沙盒永远看不到。
#
# 依赖:stage2-control-plane/main.tf 已 apply(EKS 集群/OIDC 已知)

# ---------- LiteLLM IRSA 角色 ----------

resource "aws_iam_role" "litellm" {
  name = "${var.cluster_name}-litellm"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_issuer}:sub" = "system:serviceaccount:litellm:litellm"
          "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "litellm_bedrock" {
  name = "litellm-bedrock"
  role = aws_iam_role.litellm.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
      ]
      Resource = [
        "arn:aws:bedrock:*::foundation-model/anthropic.*",
        "arn:aws:bedrock:*:*:inference-profile/us.anthropic.*",
      ]
    }]
  })
}

# 节点角色 Bedrock 权限撤销:在 terraform/phase3/main.tf 里把
# aws_iam_role_policy.node_bedrock 删掉并 apply 即可完成迁移。
# 此处不重复声明,避免跨 state 引用复杂度。

# ---------- Kubernetes: litellm namespace ----------

resource "kubernetes_namespace" "litellm" {
  metadata {
    name   = "litellm"
    labels = { "app.kubernetes.io/managed-by" = "terraform" }
  }
}

resource "kubernetes_service_account" "litellm" {
  metadata {
    name      = "litellm"
    namespace = kubernetes_namespace.litellm.metadata[0].name
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.litellm.arn
    }
  }
}

# ---------- LiteLLM 配置(ConfigMap) ----------

resource "kubernetes_config_map" "litellm" {
  metadata {
    name      = "litellm-config"
    namespace = kubernetes_namespace.litellm.metadata[0].name
  }
  data = {
    "config.yaml" = <<-YAML
      model_list:
        - model_name: claude-opus-4-8
          litellm_params:
            model: bedrock/us.anthropic.claude-opus-4-8
            aws_region_name: ${var.region}
        - model_name: claude-sonnet-4-6
          litellm_params:
            model: bedrock/us.anthropic.claude-sonnet-4-6
            aws_region_name: ${var.region}
        - model_name: claude-haiku-4-5
          litellm_params:
            model: bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0
            aws_region_name: ${var.region}
      litellm_settings:
        master_key: os.environ/LITELLM_MASTER_KEY
        drop_params: true
    YAML
  }
}

# ---------- LiteLLM Master Key Secret ----------

resource "kubernetes_secret" "litellm_key" {
  metadata {
    name      = "litellm-secrets"
    namespace = kubernetes_namespace.litellm.metadata[0].name
  }
  # 生产应从 Secrets Manager 取;POC 用随机占位
  data = {
    LITELLM_MASTER_KEY = var.litellm_master_key
  }
}

variable "litellm_master_key" {
  type        = string
  description = "LiteLLM master key（生产必传随机密钥，例如: openssl rand -hex 32）"
  sensitive   = true
  # 无默认值——强制调用方显式传入，防止弱密钥上线
}

# ---------- LiteLLM Deployment ----------

resource "kubernetes_deployment" "litellm" {
  # 镜像较大（~750MB），首次拉取时间长；wait_for_rollout=false 避免 apply 超时
  # 用 kubectl rollout status -n litellm deployment/litellm 手动确认就绪
  wait_for_rollout = false

  metadata {
    name      = "litellm"
    namespace = kubernetes_namespace.litellm.metadata[0].name
    labels    = { app = "litellm" }
  }
  spec {
    # 实测 (2026-06-14)：LiteLLM 镜像在 2Gi limit 下【必 OOMKilled】（连续重启）。
    # 默认设为 4Gi；单节点集群两副本受 anti-affinity 影响第二副本会 Pending，故默认 1 副本。
    replicas = 1
    selector { match_labels = { app = "litellm" } }
    template {
      metadata { labels = { app = "litellm" } }
      spec {
        service_account_name = kubernetes_service_account.litellm.metadata[0].name
        container {
          name  = "litellm"
          image = "ghcr.io/berriai/litellm:main-stable"
          args  = ["--config", "/app/config.yaml", "--port", "4000", "--num_workers", "2"]
          port { container_port = 4000 }
          env {
            name = "LITELLM_MASTER_KEY"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.litellm_key.metadata[0].name
                key  = "LITELLM_MASTER_KEY"
              }
            }
          }
          volume_mount {
            name       = "config"
            mount_path = "/app/config.yaml"
            sub_path   = "config.yaml"
          }
          resources {
            requests = { cpu = "250m", memory = "1Gi" }
            limits   = { cpu = "2",    memory = "4Gi" }  # 2Gi 会 OOMKilled，实测需 4Gi
          }
          readiness_probe {
            http_get {
              path = "/health/readiness"
              port = 4000
            }
            initial_delay_seconds = 10
            period_seconds        = 10
          }
        }
        volume {
          name = "config"
          config_map {
            name = kubernetes_config_map.litellm.metadata[0].name
          }
        }
      }
    }
  }
}

# ---------- LiteLLM Service(集群内) ----------

resource "kubernetes_service" "litellm" {
  metadata {
    name      = "litellm"
    namespace = kubernetes_namespace.litellm.metadata[0].name
  }
  spec {
    selector = { app = "litellm" }
    port {
      port        = 4000
      target_port = 4000
      protocol    = "TCP"
    }
    type = "ClusterIP"
  }
}

output "litellm_url" {
  value = "http://litellm.litellm.svc.cluster.local:4000"
}

output "litellm_role_arn" {
  value = aws_iam_role.litellm.arn
}
