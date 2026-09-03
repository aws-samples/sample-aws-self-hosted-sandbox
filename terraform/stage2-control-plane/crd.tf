# ---------- Route A: FirecrackerSandbox CRD + Operator ----------

variable "crd_control_enabled" {
  type        = bool
  default     = true
  description = "Use FirecrackerSandbox CRDs as lifecycle source of truth. false rolls the API back to the legacy direct lifecycle path."
}

variable "crd_operator_replicas" {
  type        = number
  default     = 2
  description = "Firecracker operator replicas. Per-sandbox DynamoDB leases fence lifecycle side effects; one elected leader runs maintenance loops."
}

variable "crd_operator_workers" {
  type        = number
  default     = 8
  description = "Concurrent CRD reconciliation workers per operator replica."
}

resource "kubernetes_manifest" "firecracker_sandbox_crd" {
  count = var.crd_control_enabled ? 1 : 0

  manifest = {
    apiVersion = "apiextensions.k8s.io/v1"
    kind       = "CustomResourceDefinition"
    metadata = {
      name = "firecrackersandboxes.sandbox.memorion.ai"
      labels = {
        "app.kubernetes.io/managed-by" = "terraform"
        "app.kubernetes.io/part-of"    = "sandbox-platform"
      }
    }
    spec = {
      group = "sandbox.memorion.ai"
      scope = "Namespaced"
      names = {
        plural     = "firecrackersandboxes"
        singular   = "firecrackersandbox"
        kind       = "FirecrackerSandbox"
        shortNames = ["fcsbx"]
        categories = ["all"]
      }
      versions = [{
        name    = "v1alpha1"
        served  = true
        storage = true
        schema = {
          openAPIV3Schema = {
            type = "object"
            properties = {
              apiVersion = { type = "string" }
              kind       = { type = "string" }
              metadata   = { type = "object" }
              spec = {
                type = "object"
                required = [
                  "desiredState",
                  "tenantId",
                  "cpu",
                  "memoryMiB",
                ]
                properties = {
                  desiredState = {
                    type = "string"
                    enum = ["Running", "Suspended"]
                  }
                  suspendReason = {
                    type = "string"
                    enum = ["", "manual", "idle"]
                  }
                  operationId = {
                    type      = "string"
                    maxLength = 128
                  }
                  tenantId = {
                    type      = "string"
                    minLength = 1
                    maxLength = 256
                  }
                  image = {
                    type      = "string"
                    maxLength = 2048
                  }
                  cpu = {
                    type    = "integer"
                    minimum = 1
                    maximum = 64
                  }
                  memoryMiB = {
                    type    = "integer"
                    minimum = 128
                    maximum = 524288
                  }
                  pool = {
                    type = "string"
                    enum = ["", "protected", "spot"]
                  }
                  env = {
                    type                 = "object"
                    additionalProperties = { type = "string" }
                  }
                  services = {
                    type = "array"
                    items = {
                      type     = "object"
                      required = ["port"]
                      properties = {
                        port = {
                          type    = "integer"
                          minimum = 1
                          maximum = 65535
                        }
                        protocol = {
                          type = "string"
                          enum = ["tcp", "udp"]
                        }
                        autostop  = { type = "boolean" }
                        autostart = { type = "boolean" }
                      }
                    }
                  }
                  meta = {
                    type                                   = "object"
                    "x-kubernetes-preserve-unknown-fields" = true
                  }
                }
              }
              status = {
                type                                   = "object"
                "x-kubernetes-preserve-unknown-fields" = true
              }
            }
          }
        }
        subresources = {
          status = {}
        }
        additionalPrinterColumns = [
          {
            name     = "Phase"
            type     = "string"
            jsonPath = ".status.phase"
          },
          {
            name     = "Desired"
            type     = "string"
            jsonPath = ".spec.desiredState"
          },
          {
            name     = "Node"
            type     = "string"
            jsonPath = ".status.node"
          },
          {
            name     = "Age"
            type     = "date"
            jsonPath = ".metadata.creationTimestamp"
          },
        ]
      }]
    }
  }
}

resource "kubernetes_deployment" "firecracker_operator" {
  count            = var.crd_control_enabled ? 1 : 0
  wait_for_rollout = false

  metadata {
    name      = "firecracker-operator"
    namespace = kubernetes_namespace.sandbox_system.metadata[0].name
    labels = {
      app                         = "firecracker-operator"
      "app.kubernetes.io/part-of" = "sandbox-platform"
    }
  }

  spec {
    replicas = var.crd_operator_replicas
    selector {
      match_labels = { app = "firecracker-operator" }
    }
    template {
      metadata {
        labels = {
          app                         = "firecracker-operator"
          "app.kubernetes.io/part-of" = "sandbox-platform"
        }
        annotations = {
          "sandbox.platform/config-sha256"          = sha256(jsonencode(kubernetes_config_map.control_plane.data))
          "sandbox.platform/node-agent-auth-sha256" = sha256(local.node_agent_auth_secret)
        }
      }
      spec {
        service_account_name = kubernetes_service_account.firecracker_operator.metadata[0].name
        node_selector        = var.enable_fargate ? {} : { "workload-tier" = "system" }

        affinity {
          pod_anti_affinity {
            preferred_during_scheduling_ignored_during_execution {
              weight = 100
              pod_affinity_term {
                topology_key = "kubernetes.io/hostname"
                label_selector {
                  match_labels = { app = "firecracker-operator" }
                }
              }
            }
          }
        }

        container {
          name    = "operator"
          image   = var.control_plane_image
          command = ["python3", "-m", "sandbox_api.operator"]
          env_from {
            config_map_ref {
              name = kubernetes_config_map.control_plane.metadata[0].name
            }
          }
          env_from {
            secret_ref {
              name = kubernetes_secret.node_agent_auth.metadata[0].name
            }
          }
          resources {
            requests = { cpu = "250m", memory = "512Mi" }
            limits   = { cpu = "2", memory = "2Gi" }
          }
        }
      }
    }
  }

  depends_on = [
    kubernetes_manifest.firecracker_sandbox_crd,
    kubernetes_cluster_role_binding.firecracker_operator,
  ]
}

resource "kubernetes_pod_disruption_budget_v1" "firecracker_operator" {
  count = var.crd_control_enabled ? 1 : 0

  metadata {
    name      = "firecracker-operator"
    namespace = kubernetes_namespace.sandbox_system.metadata[0].name
  }
  spec {
    min_available = "1"
    selector {
      match_labels = { app = "firecracker-operator" }
    }
  }
}
