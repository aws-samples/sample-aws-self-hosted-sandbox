# JuiceFS 基础设施 —— workspace 持久化（方案 B）
#
# 架构：
#   /workspace  = JuiceFS 客户端在 guest 内运行，数据落 S3，元数据落 ElastiCache Redis
#   快照只含 vm.mem + vm.snapshot（不含 rootfs/workspace 磁盘）
#   跨机 resume 后 JuiceFS 自动重连 S3（无需复制磁盘文件）
#
# suspend 前必须 flush JuiceFS 脏页（writeback 模式），否则可能丢最近几秒写入。
#
# 用法：
#   默认关闭（enable_juicefs=false），需要显式开启：
#   terraform apply -var="enable_juicefs=true" ...

variable "enable_juicefs" {
  type    = bool
  default = false
  description = "启用 JuiceFS workspace（方案 B：workspace 在 S3，快照不含磁盘）"
}

variable "juicefs_redis_node_type" {
  type        = string
  default     = ""   # 留空时按 node_arch 选默认(arm64→cache.t4g.micro / amd64→cache.t3.micro)
  description = "JuiceFS 元数据 Redis(ElastiCache)节点类型。留空则随 node_arch:arm64=cache.t4g.micro(Graviton),amd64=cache.t3.micro(Intel)。"
}

locals {
  # ElastiCache 节点族:arm64=t4g(Graviton),amd64=t3(Intel)。POC 用最小规格 micro。
  juicefs_redis_node_type = var.juicefs_redis_node_type != "" ? var.juicefs_redis_node_type : (
    var.node_arch == "amd64" ? "cache.t3.micro" : "cache.t4g.micro"
  )
}

# ---------- JuiceFS workspace S3 桶 ----------

resource "aws_s3_bucket" "juicefs" {
  count  = var.enable_juicefs ? 1 : 0
  bucket = "${var.cluster_name}-juicefs-${local.account_id}"
  tags   = { Project = "claude-sbx-poc", Purpose = "juicefs-workspace" }
}

resource "aws_s3_bucket_lifecycle_configuration" "juicefs" {
  count  = var.enable_juicefs ? 1 : 0
  bucket = aws_s3_bucket.juicefs[0].id
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"
    filter { prefix = "" }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

# ---------- ElastiCache Redis（JuiceFS 元数据引擎）----------

resource "aws_elasticache_subnet_group" "juicefs" {
  count      = var.enable_juicefs ? 1 : 0
  name       = "${var.cluster_name}-juicefs"
  subnet_ids = data.aws_subnets.private.ids
}

resource "aws_security_group" "juicefs_redis" {
  count       = var.enable_juicefs ? 1 : 0
  name        = "${var.cluster_name}-juicefs-redis"
  description = "JuiceFS Redis metadata engine"
  vpc_id      = data.aws_vpc.cluster.id

  ingress {
    from_port   = 6379
    to_port     = 6379
    protocol    = "tcp"
    # 只允许 .metal 节点子网访问（guest 内 JuiceFS client 通过 TAP 走宿主 IP）
    cidr_blocks = [data.aws_vpc.cluster.cidr_block]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Project = "claude-sbx-poc" }
}

resource "aws_elasticache_replication_group" "juicefs" {
  count = var.enable_juicefs ? 1 : 0

  replication_group_id = "${var.cluster_name}-jfs"
  description          = "JuiceFS metadata engine for sandbox workspaces"
  node_type            = local.juicefs_redis_node_type
  num_cache_clusters   = 1   # POC 单节点；生产改为 2（multi-AZ）
  engine_version       = "7.1"
  port                 = 6379
  subnet_group_name    = aws_elasticache_subnet_group.juicefs[0].name
  security_group_ids   = [aws_security_group.juicefs_redis[0].id]
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true    # 生产默认开启（防同 VPC 旁路窃听 /workspace 元数据）
  # 注：开启 TLS 后 JuiceFS mount 命令需加 --tls 参数，且节点需信任 ElastiCache 证书

  tags = { Project = "claude-sbx-poc" }
}

# ---------- node-agent IAM：加 JuiceFS S3 访问权限 ----------

resource "aws_iam_role_policy" "node_agent_juicefs" {
  count = var.enable_juicefs ? 1 : 0
  name  = "node-agent-juicefs-s3"
  role  = aws_iam_role.node_agent.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                  "s3:ListBucket", "s3:ListMultipartUploadParts",
                  "s3:AbortMultipartUpload"]
      Resource = [
        aws_s3_bucket.juicefs[0].arn,
        "${aws_s3_bucket.juicefs[0].arn}/*",
      ]
    }]
  })
}

# ---------- ConfigMap 补充 JuiceFS 配置 ----------

resource "kubernetes_config_map" "juicefs" {
  count = var.enable_juicefs ? 1 : 0
  metadata {
    name      = "juicefs-config"
    namespace = kubernetes_namespace.sandbox_system.metadata[0].name
  }
  data = {
    JUICEFS_ENABLED    = "true"
    JUICEFS_BUCKET     = aws_s3_bucket.juicefs[0].id
    JUICEFS_REDIS_ADDR = "redis://${aws_elasticache_replication_group.juicefs[0].primary_endpoint_address}:6379/1"
    AWS_REGION         = var.region
  }
}

# ---------- Outputs ----------

output "juicefs_bucket" {
  value = var.enable_juicefs ? aws_s3_bucket.juicefs[0].id : "disabled"
}

output "juicefs_redis_endpoint" {
  value = var.enable_juicefs ? aws_elasticache_replication_group.juicefs[0].primary_endpoint_address : "disabled"
}
