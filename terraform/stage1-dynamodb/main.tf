# Stage 1 — DynamoDB 表:沙盒状态 + 事件历史 + tap_idx 分配器
#
# 用法:
#   cd terraform/stage1-dynamodb
#   terraform init && terraform apply
#
# 销毁:
#   terraform destroy

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" { region = var.region }

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "prefix" {
  type    = string
  default = "claude-sbx"
}

# ---------- 主状态表 ----------
resource "aws_dynamodb_table" "sandboxes" {
  name         = "${var.prefix}-sandboxes"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
    type = "S"
  }
  attribute {
    name = "tenant_id"
    type = "S"
  }
  attribute {
    name = "updated_at"
    type = "S"
  }
  attribute {
    name = "idempotency_key"
    type = "S"
  }
  attribute {
    name = "pool_state"
    type = "S"
  }
  attribute {
    name = "driver"
    type = "S"
  }

  # 按租户列出沙盒(最新优先)
  global_secondary_index {
    name            = "tenant_id-updated_at-index"
    hash_key        = "tenant_id"
    range_key       = "updated_at"
    projection_type = "ALL"
  }

  # 幂等键查找
  global_secondary_index {
    name            = "idempotency_key-index"
    hash_key        = "idempotency_key"
    projection_type = "ALL"
  }

  # 暖池查询(pool_state=warm, driver=firecracker/kata)
  global_secondary_index {
    name            = "pool_state-driver-index"
    hash_key        = "pool_state"
    range_key       = "driver"
    projection_type = "ALL"
  }

  point_in_time_recovery { enabled = true }

  tags = { Project = "claude-sbx-poc" }
}

# ---------- 事件历史表(TTL 30 天自动清理) ----------
resource "aws_dynamodb_table" "sandbox_events" {
  name         = "${var.prefix}-sandbox-events"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"
  range_key    = "ts"

  attribute {
    name = "id"
    type = "S"
  }
  attribute {
    name = "ts"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = { Project = "claude-sbx-poc" }
}

# ---------- tap_idx 分配器表 ----------
resource "aws_dynamodb_table" "tap_idx" {
  name         = "${var.prefix}-tap-idx"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "node"

  attribute {
    name = "node"
    type = "S"
  }

  tags = { Project = "claude-sbx-poc" }
}

# ---------- 初始化 tap_idx counter ----------
resource "aws_dynamodb_table_item" "tap_idx_init" {
  table_name = aws_dynamodb_table.tap_idx.name
  hash_key   = "node"

  item = jsonencode({
    node     = { S = "global" }
    next_idx = { N = "0" }
  })

  lifecycle { ignore_changes = [item] } # 只初始化一次,不覆盖运行时自增的值
}

# ---------- 节点心跳注册表(P0-3:替换 FC_NODES 环境变量硬编码) ----------
# node-agent 每 ~30s upsert 一条,写 last_seen(ISO8601)。
# 控制面读时按 last_seen 超时过滤活节点 —— 不用 DynamoDB TTL 自动删
# (TTL 删除延迟可达 48h,不可靠;死节点必须即时从调度池剔除)。
resource "aws_dynamodb_table" "nodes" {
  name         = "${var.prefix}-nodes"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "node_id"

  attribute {
    name = "node_id"
    type = "S"
  }

  tags = { Project = "claude-sbx-poc" }
}

# ---------- 分布式锁表(P1-4:reconcile/暖池 loop 的 leader 选举) ----------
# 单条 item(lock_id="reconciler")存 owner/expires/rvn。
# 独立小表,不污染 sandboxes 表的 GSI。
resource "aws_dynamodb_table" "locks" {
  name         = "${var.prefix}-locks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "lock_id"

  attribute {
    name = "lock_id"
    type = "S"
  }

  tags = { Project = "claude-sbx-poc" }
}

# ---------- 输出 ----------
output "sandboxes_table" { value = aws_dynamodb_table.sandboxes.name }
output "events_table" { value = aws_dynamodb_table.sandbox_events.name }
output "tap_idx_table" { value = aws_dynamodb_table.tap_idx.name }
output "nodes_table" { value = aws_dynamodb_table.nodes.name }
output "locks_table" { value = aws_dynamodb_table.locks.name }
