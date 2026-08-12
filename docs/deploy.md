# 完整部署手册

> 本文件包含从零部署 AWS 沙盒平台的完整步骤，供 Agent 或工程师按序执行。
> 快速入口见根目录 [README.md](../README.md)。

---

## 后端：裸 Firecracker microVM

本平台后端为**裸 Firecracker microVM + node-agent DaemonSet**：支持 suspend/resume 亚秒恢复 +
持久 EBS 跨机快照（状态卷落持久 EBS + Diff 增量内存快照），成本优先。控制面通过 HTTP 直管每台沙盒节点上的
node-agent，不依赖 K8s 编排沙盒本身。

部署者必须选择节点架构：

| 选择 | `node_arch` | 默认实例 | KVM 来源 |
|---|---|---|---|
| Graviton | `arm64` | `c6g.metal` | 裸金属 |
| Intel x86 | `amd64` | `i7i.8xlarge` | Nitro nested virtualization |

x86 支持所有 `i7i.*` 规格，可用 `sandbox_instance_type` 覆盖；默认选 `i7i.8xlarge`。

无论选择哪种数据节点，Terraform 都会另外创建至少 2 台 On-Demand Graviton
system 节点，承载控制面、LiteLLM、Ingress 与 CoreDNS。sandbox 节点带
`dedicated=sandbox:NoSchedule` taint，只承载 node-agent 与 Firecracker microVM。
`node_arch` 描述的是数据节点，不是整个 EKS 集群。

> （历史上曾有可插拔的 Kata-on-EKS 后端，因无法快照/恢复、与本平台 spot 疏散核心诉求不符，已移除。
> 本手册即 Firecracker 单一主线，无需再选 driver。）

---

## 前提条件

- AWS CLI 已配置（需要权限：EKS / EC2 / IAM / DynamoDB / ECR / S3；启用托管可观测性时还需要 APS/AMP、Amazon Managed Grafana、CloudWatch Logs、X-Ray 和 VPC Endpoint 权限）
- 已安装：kubectl, terraform (≥1.5), helm, git, docker
- EC2 vCPU 服务配额：Graviton `c6g.metal` 需要 64 vCPU；x86 默认 `i7i.8xlarge` 需要 32 vCPU
- x86 部署区域必须提供 i7i 且支持 nested virtualization；部署前按 Step 0.5 查询
- 生产部署必须设置 `API_KEYS`（见 Step 6 注意事项）

---

## ⚠️ 注意事项（含实测踩坑，务必先读）

1. **认证 = 硬门槛**：控制面若 `API_KEYS` 未设、又没设 `ALLOW_UNAUTHENTICATED=1`，则**所有写操作（create/exec/suspend…）直接返回 503 `control plane not configured`**。生产必须传 `-var="api_keys=..."`；本地测试可给控制面 deployment 加 env `ALLOW_UNAUTHENTICATED=1`（见 Step 9 排障）。
2. **DynamoDB 表必须先建**（Step 1）。漏了这步，控制面 create 会报 `ResourceNotFoundException`（boto3 找不到表），且报错发生在业务逻辑里、不易一眼看出。
3. **`fc_nodes` 是 fallback，节点发现优先走 DynamoDB 心跳表**：P0 加固后 node-agent 每 30s 写 `claude-sbx-nodes` 表，控制面 `_pick_node` 优先从心跳表选活节点（按 `last_seen` 超时剔除死节点），`fc_nodes` 仅在心跳表为空时兜底。**首次部署 fc_nodes 仍建议只填稳定节点**（心跳还没写起来时靠它），但节点增减后无需再改 `fc_nodes` + 重启控制面——心跳表会自动反映。查活节点：`aws dynamodb scan --table-name claude-sbx-nodes --query 'Items[].{node:node_id.S,last_seen:last_seen.S}'`。
4. **rootfs 必须是含 vsock agent 的 min-rootfs**：exec 走 vsock 通道，需要 `scripts/build-min-rootfs.sh` 产出的 rootfs（内含 `/sbin/vsock-exec-agent.py`，sbxinit 后台启动）。**别用 phase3 `rootfs_s3_uri` 的默认 juicefs 版**——apply phase3 时必须显式传 `-var="rootfs_s3_uri=s3://<bucket>/rootfs/min-rootfs.tar.gz"`（见 Step 1.5 + Step 2）。
5. **节点反复 NotReady / ASG 替换循环**：`c6g.metal` 过 EC2 status check 需 5-10 分钟，而 EKS 托管节点组 ASG 默认 grace period 过短。`terraform/phase3/main.tf` 已用 `null_resource.sandbox_asg_grace_period` 固化为 900s。若仍反复替换，检查 ASG activity、EC2 status check 和 grace period。
6. **异构镜像必须分别构建**：控制面固定用 `linux/arm64`；node-agent 与 rootfs 跟随 sandbox 数据节点，Graviton 用 `linux/arm64`，i7i 用 `linux/amd64`。不要用一个共享 `--platform` 把控制面也构建成 amd64。
7. **system 节点必须保持 On-Demand**：未来可为 sandbox 数据面增加 Spot 池，但控制面、LiteLLM、Ingress 与 CoreDNS 不应跟随 Spot 中断。
8. **LiteLLM 必须传 master key**：`litellm_master_key` 无默认值，terraform apply 时必须传入（如 `openssl rand -hex 32`）。
9. **SSM 排障用 `AWS-RunShellScript`**：本账号 `AWS-RunShellCommand`（旧名）不可用，`aws ssm send-command` 要用 document 名 `AWS-RunShellScript`。
10. **费用提醒**：沙盒节点和 EKS 控制面持续计费，用完务必执行【清理】步骤。清理时 stage2 destroy 若卡在删 `sandbox-system` namespace，可强制删除残留 node-agent pod 后继续。
11. **Helm/Kubernetes 认证依赖 AWS CLI**：stage2 provider 使用 `aws eks get-token` 动态刷新凭据，避免 15 分钟 EKS token 在长时间 Helm upgrade 中过期。执行 Terraform 的环境必须能从 `PATH` 调用 `aws`，且当前身份可访问目标集群。
12. **可观测性分三层**：`enable_observability_stack=true` 安装集群内监控；`enable_amp_remote_write=true` 创建 AMP；`enable_p2_observability=true` 增加 CloudWatch Logs、ADOT/X-Ray 和 AMG datasource/Dashboard 自动配置。AMG workspace 仍需预先存在。

---

## Step 0: 克隆代码库

```bash
git clone https://github.com/teaguexiao/aws-self-hosted-sandbox.git
cd aws-self-hosted-sandbox
export AWS_REGION=us-east-1
```

---

## Step 0.5: 选择 Graviton 或 x86

```bash
# 二选一。本文后续命令复用这两个变量。
export NODE_ARCH=arm64
export SANDBOX_INSTANCE_TYPE=c6g.metal
export SANDBOX_AZ_INDEX=0
export SYSTEM_INSTANCE_TYPE=m7g.large
export SYSTEM_NODE_COUNT=2

# Intel x86（默认 i7i.8xlarge；其他 i7i 规格也支持）
# export NODE_ARCH=amd64
# export SANDBOX_INSTANCE_TYPE=i7i.8xlarge
# export SANDBOX_AZ_INDEX=0   # 0/1/2 = us-east-1a/1b/1c

if [ "$NODE_ARCH" = "amd64" ]; then
  aws ec2 describe-instance-types --region "$AWS_REGION" \
    --instance-types "$SANDBOX_INSTANCE_TYPE" \
    --query 'InstanceTypes[0].{type:InstanceType,arch:ProcessorInfo.SupportedArchitectures,features:ProcessorInfo.SupportedFeatures}' \
    --output table
fi
```

选择 `amd64` 时，Terraform 会在 Launch Template 中显式设置
`nested_virtualization=enabled`。若查询结果或目标区域不支持该能力，不要继续 apply。

---

## Step 1: 创建 DynamoDB 状态表（必做，勿跳）

```bash
cd terraform/stage1-dynamodb
terraform init && terraform apply -auto-approve
# 验证：应看到 claude-sbx-sandboxes / -sandbox-events / -tap-idx
aws dynamodb list-tables --region us-east-1 | grep claude-sbx
```

> ⚠️ 漏掉这步 → 控制面 create 报 `ResourceNotFoundException`（见注意事项 2）。

---

## Step 1.5: 构建并上传含 vsock agent 的 min-rootfs（FC 专用，勿跳）

FC 的 exec 走 vsock 通道，rootfs 内必须有 `/sbin/vsock-exec-agent.py`（由 sbxinit 后台启动）。
用 `build-min-rootfs.sh` 构建。目标平台必须与 `NODE_ARCH` 一致：

```bash
cd ../..   # 回到仓库根
ACCT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="my-sandbox-snapshots-${ACCT}"
aws s3 mb "s3://${BUCKET}" --region us-east-1 2>/dev/null || true

# 按架构隔离 S3 key，避免 arm64 / amd64 rootfs 相互覆盖
if [ "$NODE_ARCH" = "amd64" ]; then
  export PLATFORM=linux/amd64 ROOTFS_KEY=rootfs/amd64/min-rootfs.tar.gz
else
  export PLATFORM=linux/arm64 ROOTFS_KEY=rootfs/arm64/min-rootfs.tar.gz
fi

bash scripts/build-min-rootfs.sh "${BUCKET}"

# 验证 vsock agent 确实进了 rootfs（可选）
aws s3 cp "s3://${BUCKET}/${ROOTFS_KEY}" /tmp/r.tgz --region us-east-1
tar tzf /tmp/r.tgz | grep -E 'sbin/(vsock-exec-agent.py|sbxinit)$'
```

> Mac 上 docker 未起：Graviton 可用 `colima start --cpu 4 --memory 8 --arch aarch64`；
> i7i 构建用 Docker 的 `linux/amd64` 平台。

---

## Step 1.6: 构建自定义镜像 / rootfs 模板（可选，vibe coding / web demo 用）

让 `image` 字段生效——沙盒按 image 选不同 rootfs 模板。`build-rootfs-image.sh <name> <bucket>`
产出 `rootfs-{name}.tar.gz`,节点会造成 `/opt/sbx/rootfs-{name}.ext4`,create `image={name}` 时 CoW 它。
内置 **`web`** 预设:自带 demo 首页 + 开机自起 :80 → 端口暴露打开即见站点。

```bash
# 构建 web 模板(与 min 同基底,叠加 demo 站点 + 开机自起 :80)
ROOTFS_PREFIX="rootfs/${NODE_ARCH}" PLATFORM="${PLATFORM}" \
  bash scripts/build-rootfs-image.sh web "${BUCKET}"
# → 上传到 s3://<bucket>/rootfs/rootfs-web.tar.gz
```

> 节点在 Step 2 apply 时按 `rootfs_images`(默认含 `web`)拉取这些模板造 ext4。
> **本步须在 Step 2 之前完成**(节点 userData 启动时拉);漏了则 create `image=web` 会回退默认 min(不报错)。
> 未列出的 image → 同样回退 min。控制面 `SANDBOX_IMAGES`(默认 `min,web`)决定 Portal 下拉列表。

---

## Step 2: 创建 EKS 集群 + Firecracker 沙盒节点组

```bash
cd terraform/phase3
MY_IP=$(curl -s https://checkip.amazonaws.com)   # 若出口 IP 不固定（NAT 池），用覆盖网段如 x.y.z.0/24
ACCT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="my-sandbox-snapshots-${ACCT}"

terraform init && terraform apply -auto-approve \
  -var="node_arch=${NODE_ARCH}" \
  -var="sandbox_instance_type=${SANDBOX_INSTANCE_TYPE}" \
  -var="sandbox_az_index=${SANDBOX_AZ_INDEX}" \
  -var="system_instance_type=${SYSTEM_INSTANCE_TYPE}" \
  -var="system_node_count=${SYSTEM_NODE_COUNT}" \
  -var="rootfs_s3_uri=s3://${BUCKET}/${ROOTFS_KEY}" \
  -var="rootfs_images=web" \
  -var="endpoint_public_access_cidrs=[\"${MY_IP}/32\"]"
# rootfs_images(默认 web):节点额外拉 rootfs-{name}.tar.gz 造 /opt/sbx/rootfs-{name}.ext4 模板;
#   须在 Step 1.6 先构建上传对应模板。不需要自定义镜像可传 -var="rootfs_images="。
# 默认 1 台沙盒节点；跨机快照演示加 -var="sandbox_node_count=2"
aws eks update-kubeconfig --name claude-sbx --region us-east-1
kubectl wait node --all --for=condition=Ready --timeout=900s
```

若 ASG activity 报 `InsufficientInstanceCapacity`，说明所选规格在当前 AZ 暂时无
On-Demand 容量。保持单 AZ/EBS 约束，设置 `SANDBOX_AZ_INDEX=1`（region-b）或 `2`
（region-c）后重新 apply。可先用下面命令查看 AWS 的具体建议：

```bash
aws autoscaling describe-scaling-activities --region "$AWS_REGION" \
  --auto-scaling-group-name "<asg-name>" --max-items 5 \
  --query 'Activities[].StatusMessage' --output table
```

> ⚠️ `rootfs_s3_uri` 不传 → 用默认 juicefs 版 rootfs（无 vsock agent）→ exec 掉到 SSH 兜底并因 sbxinit 硬编码 IP 失败（见注意事项 4）。
> ⚠️ 节点可能冷启动抖动（NotReady）；记下**稳定 Ready** 的节点内网 IP，Step 6 的 `fc_nodes` 只填稳定节点。
>
> 直接进入 Step 5（构建镜像）→ Step 6（部署控制面）。POC 用 kubectl port-forward 访问控制面，
> 无需 ingress-nginx；沙盒节点即 phase3 的托管节点组，无需 Karpenter。

---

## Step 5: 创建 ECR 仓库并构建对应架构镜像

```bash
# claude-sbx 仓库已由 Step 2 的 Terraform 自动创建，只需建以下两个：
ACCT=$(aws sts get-caller-identity --query Account --output text)
aws ecr create-repository --repository-name sandbox-control-plane --region us-east-1 2>/dev/null || true
aws ecr create-repository --repository-name node-agent --region us-east-1 2>/dev/null || true

# 控制面运行在 Graviton system 节点；node-agent 跟随 sandbox 数据节点。
bash scripts/build_and_push.sh \
  --control-plane-platform linux/arm64 \
  --node-agent-platform "${PLATFORM}"

# 也可在目标架构 EC2 上原生构建
```

node-agent 镜像不包含 SSH 私钥，exec 默认使用 vsock。若确需 SSH fallback，应在
运行时通过 Kubernetes Secret 只读挂载 `/root/.ssh/id_ed25519`，不要写入镜像层。

---

## Step 6: 部署控制面 + node-agent（Firecracker 模式）

```bash
cd terraform/stage2-control-plane
terraform init

ACCT=$(aws sts get-caller-identity --query Account --output text)
S3_BUCKET="my-sandbox-snapshots-${ACCT}"
aws s3 mb s3://${S3_BUCKET} --region us-east-1 2>/dev/null || true

# 生成随机 API key（生产必填，不能留空，否则写操作全 503）
API_KEY=$(openssl rand -hex 32)
LITELLM_KEY=$(openssl rand -hex 32)
echo "API_KEY: $API_KEY  （保存好，后续 curl 鉴权用）"

# FC 模式关键：只从【稳定 Ready】的 sandbox 数据节点取内网 IP
FC_NODES=$(kubectl get nodes -l sandbox=true \
  -o jsonpath='{range .items[*]}{.status.addresses[?(@.type=="InternalIP")].address}{","}{end}' \
  | sed 's/,$//')
echo "FC_NODES=$FC_NODES  （若含 NotReady 节点，手动改成只留稳定的）"

terraform apply -auto-approve \
  -var="node_arch=${NODE_ARCH}" \
  -var="fc_nodes=${FC_NODES}" \
  -var="sandbox_image=public.ecr.aws/amazonlinux/amazonlinux:2023" \
  -var="control_plane_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/sandbox-control-plane:latest" \
  -var="node_agent_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/node-agent:latest" \
  -var="snapshot_s3_bucket=${S3_BUCKET}" \
  -var="enable_fargate=false" \
  -var="create_ingress_nginx=false" \
  -var="sandbox_domain=sbx.example.com" \
  -var="api_keys=${API_KEY}" \
  -var="litellm_master_key=${LITELLM_KEY}"

# Terraform 自动完成：
# - IRSA 角色（控制面 / node-agent / LiteLLM）
# - K8s 资源（sandbox-system namespace + 控制面 Deployment + node-agent DaemonSet）
# - api-keys Secret + ConfigMap（FC_NODES 经 env_from 注入控制面）
```

> ⚠️ **FC_NODES 只填稳定的 sandbox 节点**：不能使用无 label 的 `kubectl get nodes`，
> 否则会把 system 节点误当 Firecracker 宿主。控制面 `_pick_node` 串行探每个节点
> `/health`，遇不可达节点会阻塞。改完可热更新：
> `kubectl set env deployment/sandbox-control-plane -n sandbox-system FC_NODES=<稳定数据节点IP>`。
>
> **常见问题：** Terraform `Unexpected Identity Change` 错误 → 清理 state 重试：
> ```bash
> terraform state rm kubernetes_deployment.control_plane
> terraform apply ...
> ```

跳到 Step 9 验证。

---

## Step 6.2: 部署可观测性（P1，推荐）

控制面与 node-agent 均暴露低基数 `/metrics`，并提供 `/livez`、`/readyz`。指标不包含
`sandbox_id`，避免实例规模直接放大 Prometheus series。快照生成 SHA-256 manifest，
恢复前校验；校验失败会拒绝恢复并增加错误指标。

### 模式 A：集群内 Prometheus + Alertmanager + Grafana

先设置 Terraform 环境变量，再重新执行 Step 6 的完整 `terraform apply`，保持原参数值不变：

```bash
export TF_VAR_enable_observability_stack=true
export TF_VAR_grafana_admin_password="$(openssl rand -base64 32 | tr -d '\n')"

# 重新执行 Step 6 的 terraform apply；无需再追加 -var。
```

`grafana_admin_password` 至少 16 字符，通过 Helm `set_sensitive` 传入。不要打印密码或提交
tfvars/state；后续 apply 应复用原值，可从 `monitoring/sandbox-monitoring-grafana` Secret
读取到当前 shell，而不是重新生成。

```bash
export TF_VAR_grafana_admin_password="$(
  kubectl get secret sandbox-monitoring-grafana -n monitoring \
    -o jsonpath='{.data.admin-password}' | base64 --decode
)"
```

Terraform 安装并配置：

- `kube-prometheus-stack`（chart 固定版本见 `observability_chart_version`）
- control-plane ServiceMonitor 与 node-agent PodMonitor
- 5 类告警：唤醒体验、快照完整性、节点容量、孤儿增长、控制面退化
- `Sandbox Platform` Dashboard：8 个低基数面板
- Prometheus 3 天本地 retention；组件固定到 system 节点，node-exporter 容忍 sandbox taint

访问方式：

```bash
terraform output prometheus_port_forward
terraform output grafana_port_forward
terraform output alertmanager_port_forward

kubectl -n monitoring port-forward svc/sandbox-monitoring-prometheus 9090:9090
# 新终端验证 targets 与 remote-write 前的本地采集
curl -fsS http://127.0.0.1:9090/api/v1/targets | \
  jq '{active:(.data.activeTargets|length),up:([.data.activeTargets[]|select(.health=="up")]|length)}'
```

### 模式 B：增加 Amazon Managed Service for Prometheus（AMP）

AMP 依赖模式 A，不能单独启用：

```bash
export TF_VAR_enable_amp_remote_write=true

# 现在逐字重新执行 Step 6 的完整 terraform apply，然后查看输出：

terraform output amp_workspace_id
terraform output amp_query_endpoint
```

Terraform 会创建 AMP workspace、Prometheus IRSA role 和最小
`aps:RemoteWrite` policy，并给 Prometheus 配置 SigV4 remote-write。Prometheus Pod
template annotation 会在角色或配置变化时触发受控滚动。

```bash
# 本地 Prometheus 中确认 remote-write 无失败；先执行上面的 port-forward
curl -fsSG http://127.0.0.1:9090/api/v1/query \
  --data-urlencode 'query=sum(prometheus_remote_storage_samples_failed_total)' | jq
curl -fsSG http://127.0.0.1:9090/api/v1/query \
  --data-urlencode 'query=sum(prometheus_remote_storage_samples_total)' | jq

# IRSA 必须同时存在于 Prometheus 容器
POD=$(kubectl get pod -n monitoring -l app.kubernetes.io/name=prometheus \
  -o jsonpath='{.items[0].metadata.name}')
kubectl get pod "$POD" -n monitoring -o json | jq \
  '[.spec.containers[].env[]?|select(.name=="AWS_ROLE_ARN" or .name=="AWS_WEB_IDENTITY_TOKEN_FILE")|.name]'
```

### 模式 C：让现有 Amazon Managed Grafana（AMG）查询 AMP

先创建或选择一个 `ACTIVE` AMG workspace，并配置 VPC 连接。将 workspace ID、VPC、
AMG 使用的子网和安全组传给同一次 apply：

```bash
export TF_VAR_managed_grafana_workspace_id="g-xxxxxxxxxx"
export TF_VAR_managed_grafana_vpc_id="vpc-id"
export TF_VAR_managed_grafana_subnet_ids='["subnet-a","subnet-b"]'
export TF_VAR_managed_grafana_security_group_id="amg-workspace-sg"

# 现在逐字重新执行 Step 6 的完整 terraform apply，然后查看输出：

terraform output managed_grafana_endpoint
terraform output managed_grafana_amp_vpc_endpoint_id
```

Terraform 会把 AMP 查询权限附加到 AMG workspace role，并在 AMG VPC 创建
`aps-workspaces` Interface Endpoint。Endpoint 专用安全组只允许 AMG workspace SG
的 TCP/443。若不传 `managed_grafana_vpc_id`，不会创建 PrivateLink。

最后在 AMG 中创建 Prometheus datasource（可用 AWS Console 的 workspace
`Data sources` 页面）：

| 设置 | 值 |
|---|---|
| Name / UID | `Sandbox AMP` / `sandbox-amp` |
| URL | `terraform output -raw amp_query_endpoint` |
| SigV4 auth | Enabled |
| SigV4 region | 与 `var.region` 相同 |
| Authentication provider | Workspace IAM role / default AWS SDK credentials |
| Default datasource | Enabled（Dashboard 查询未硬编码 datasource UID） |

保存后 `Save & test` 必须返回 `Successfully queried the Prometheus API`。导出 Terraform
生成的 Dashboard JSON，再从 AMG 的 `Dashboards → New → Import` 导入：

```bash
kubectl get configmap sandbox-platform-dashboard -n monitoring \
  -o jsonpath='{.data.sandbox-platform\.json}' > /tmp/sandbox-platform.json
```

导入后至少验证：

```promql
count(up{namespace="sandbox-system",service="sandbox-control-plane"})
count(up{namespace="sandbox-system",job="monitoring/sandbox-node-agent"})
```

AMG API 自动化测试如果临时创建 service account/token，必须在测试结束后立即删除；
不要把 token 写入日志、文档或 shell history。2026-08-12 的真实 AWS 验证证据见
[P1 可观测性真机测试报告](P1可观测性-真机测试报告-2026-08-12.md)。

### 模式 D：集中日志、跨组件 tracing 与 AMG 自动配置

模式 D 要求模式 A、B 已启用，并要求模式 C 的 AMG workspace 参数完整：

```bash
export TF_VAR_enable_p2_observability=true

# 逐字重新执行 Step 6 的完整 terraform apply。
terraform output cloudwatch_platform_log_group
terraform output adot_otlp_http_endpoint
```

Terraform 将部署：

- `aws-for-fluent-bit` DaemonSet，仅把 `sandbox-system` 与 `monitoring` 日志写入
  `/aws/eks/<cluster>/sandbox-platform`，默认保留 30 天；
- 两副本 ADOT Collector，通过 IRSA 写入 AWS X-Ray；
- control-plane/node-agent 的 W3C `traceparent` 传播和 `trace_id`/`span_id` JSON 日志；
- `configure-managed-grafana.sh`：创建 15 分钟 AMG service-account token，幂等 upsert
  `sandbox-amp` datasource 和 `sandbox-platform` Dashboard，健康检查后立即删除凭据。

按 correlation ID 验证 CloudWatch Logs：

```bash
aws logs start-query \
  --log-group-name "/aws/eks/${CLUSTER}/sandbox-platform" \
  --start-time "$(( $(date +%s) - 900 ))" --end-time "$(date +%s)" \
  --query-string 'fields @logStream, request_id, trace_id, event | filter request_id="<REQUEST_ID>"'
```

从日志取得 32 位 `trace_id` 后转换为 X-Ray ID：

```bash
TRACE_HEX="<32-hex-trace-id>"
aws xray batch-get-traces \
  --trace-ids "1-${TRACE_HEX:0:8}-${TRACE_HEX:8}" --region "$AWS_REGION"
```

应同时看到 control-plane server segment、node-agent client/server segment。完整证据见
[P2 可观测性真机测试报告](P2可观测性-真机测试报告-2026-08-12.md)。

---

## Step 6.5: 启用沙盒端口暴露（可选，vibe coding / web 预览需要）

让沙盒内的 web 服务（如 :80 / :3000）能从集群外访问。链路：
`用户 → NLB → ingress-nginx → 控制面 /s/{id}/{port}/ → node-agent /proxy → guest`。
用**路径**（非子域名）路由，天然支持**多个沙盒暴露同一内部端口**（如两个沙盒都开 80）。

> 不需要端口暴露（只用 exec/API）可**跳过本步**，Step 6 已足够。

```bash
cd terraform/stage2-control-plane
ACCT=$(aws sts get-caller-identity --query Account --output text)
S3_BUCKET="my-sandbox-snapshots-${ACCT}"
FC_NODES="<Step 6 用的稳定节点 IP>"
API_KEY="<Step 6 生成的 API_KEY>"
LITELLM_KEY="<Step 6 生成的 LITELLM_KEY>"

# 1) 重新 apply，打开 create_ingress_nginx（拉起共享 NLB）。其余 var 与 Step 6 保持一致。
terraform apply -auto-approve \
  -var="node_arch=${NODE_ARCH}" \
  -var="fc_nodes=${FC_NODES}" \
  -var="sandbox_image=public.ecr.aws/amazonlinux/amazonlinux:2023" \
  -var="control_plane_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/sandbox-control-plane:latest" \
  -var="node_agent_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/node-agent:latest" \
  -var="snapshot_s3_bucket=${S3_BUCKET}" \
  -var="enable_fargate=false" \
  -var="create_ingress_nginx=true" \
  -var="sandbox_domain=sbx.example.com" \
  -var="api_keys=${API_KEY}" \
  -var="litellm_master_key=${LITELLM_KEY}"

# 2) 等 NLB 就绪，取它的自带域名（约 2-3 分钟才会 provision 出 hostname）
NLB_HOST=$(kubectl get svc -n ingress-nginx ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
echo "NLB_HOST=$NLB_HOST"   # 形如 xxxx.elb.us-east-1.amazonaws.com

# 3) 把 NLB 域名回填给控制面（Portal 用它拼可点击 URL）。二选一：
#    a) 快速热更新（不改 terraform state）：
kubectl set env deployment/sandbox-control-plane -n sandbox-system NLB_HOSTNAME="$NLB_HOST"
#    b) 或重新 apply 固化：上面命令再加 -var="nlb_hostname=${NLB_HOST}"

# 4) 加一条 Ingress，把 /s 路径路由到控制面（sandbox-proxy）。NLB 自带域名无证书，走 HTTP。
kubectl apply -f - <<EOF
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: sandbox-proxy
  namespace: sandbox-system
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
spec:
  ingressClassName: nginx
  rules:
  - http:
      paths:
      - path: /s
        pathType: Prefix
        backend: { service: { name: sandbox-control-plane, port: { number: 80 } } }
EOF
```

> ⚠️ **安全与开关**（控制面 env，均可 `kubectl set env deployment/sandbox-control-plane -n sandbox-system ...` 热更新）：
> - **`ALLOW_ALL_PORTS`**（默认 `1`）：任意端口都可暴露,用户 guest 内起在任何端口都能经 `/s/{id}/{port}/` 访问,无需 create 时声明。设 `0` 退回"仅 `services` 声明端口"白名单模式(多租户生产更安全)。
> - **`EXPOSE_TOKEN`**（默认空=公开）：设置后访问 `/s/` 必须带 token（`?token=xxx` / Cookie `sbx_token` / Header `X-Sbx-Token`）。生产多租户建议开启。
> - **WebSocket** 已支持透传（Vite HMR / SSE / Web Terminal 均可）。
> - **交互式终端 / Demo Web**：Portal 详情页"打开终端""启动 Demo Web"按钮,一键在 guest 内起服务(无需重建 rootfs)。
> - **文件上传/下载**：`PUT/GET /sandboxes/{id}/files?path=`(base64 over exec,`MAX_FILE_BYTES` 默认 10MB);Portal 详情页有"文件传输"卡片。
> - **自动休眠 / 唤醒(auto-sleep / auto-wake)**：空闲沙盒自动打快照进 `slept` 状态释放 RAM,请求打到网关 `/s/` 透明 resume 唤醒(对齐 fly.io)。**opt-in,默认关**:仅对创建时声明了 `services[].autostop/autostart`(或 `meta.auto_sleep/auto_wake`)的沙盒生效。控制面 env(可 `kubectl set env` 热更新):
>   - `AUTO_SLEEP_ENABLED`(默认 `1`):总开关(仅作用于 opt-in 沙盒;`0` 整体关闭扫描)。
>   - `AUTO_SLEEP_IDLE_S`(默认 `300`):空闲多久(秒)后自动休眠。**实际入睡 = 该值 + 最多一个扫描周期**。演示可临时设小(如 `30`)。
>   - `AUTO_SLEEP_SCAN_S`(默认 `30`):后台扫描间隔;leader 门控,多副本只有 leader 扫描。
>   - `AUTO_WAKE_TIMEOUT_S`(默认 `30`):网关触发 resume 后等其回 running 的超时。
>   - `ACTIVITY_TOUCH_MIN_S`(默认 `15`):活跃时间写节流下限,防热路径写放大。
>   - **自动休眠(`slept`)与手动挂起(`suspended`)严格区分**:只有 `slept` 会被网关请求唤醒;手动 `POST /suspend` 的 `suspended` 网关不唤醒(维持 409)。Portal 徽章 `slept`=靛蓝、`suspended`=灰。
>   - Portal API Playground 创建表单有"自动休眠 / 唤醒"复选框,勾选即带上 opt-in 字段,便于测试。验证脚本:`scripts/autosleep_e2e.sh`(A0~A5)。
> - 生产进一步建议：自定义域名 + TLS（当前 NLB 自带域名走 HTTP）。

**验证任意端口 + WebSocket 终端 + 文件传输**：
```bash
# 终端:经 Portal "打开终端" 一键更方便;或浏览器访问 http://<nlb 或 localhost:18000>/s/<id>/7681/。
# 文件:上传后下载校验往返
B64=$(printf 'hello file' | base64)
curl -s -X PUT "$BASE/sandboxes/$SID/files?path=/root/t.txt" -H "Authorization: Bearer $API_KEY" \
  -d "{\"content_b64\":\"$B64\"}"                                    # → {"bytes":10,...}
curl -s "$BASE/sandboxes/$SID/files?path=/root/t.txt" -H "Authorization: Bearer $API_KEY" \
  | python3 -c "import sys,json,base64;print(base64.b64decode(json.load(sys.stdin)['content_b64']))"  # → b'hello file'
```

---


## Step 8: 配置 DNS（可选，POC 跳过）

```bash
NLB_HOST=$(kubectl get svc -n ingress-nginx ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
echo "NLB: $NLB_HOST"
# 在 Route53 添加：api.sbx.example.com CNAME $NLB_HOST
# POC 跳过 DNS，用 port-forward 即可
```

---

## Step 9: 验证部署（Firecracker）

> 已验证配置：On-Demand system=`2 × m7g.large`，sandbox=`i7i.8xlarge`、
> `node_arch=amd64`、`sandbox_az_index=2`。`us-east-1a/1b` 容量不足后，
> `us-east-1c` 创建成功。完整调度、宿主、guest、生命周期、Bedrock 和故障转移证据见
> [控制面与数据面分离 i7i 真机测试报告](控制面数据面分离-i7i真机测试报告-2026-08-11.md)。

```bash
kubectl rollout status deployment/sandbox-control-plane -n sandbox-system --timeout=300s
kubectl get pods -n sandbox-system -o wide   # 控制面 2/2 + node-agent DaemonSet（每台 sandbox=true 节点一个）

# port-forward 访问控制面
kubectl port-forward -n sandbox-system svc/sandbox-control-plane 18000:80 &
BASE=http://localhost:18000
API_KEY="<Step 6 生成的 API_KEY>"

# 健康 / 能力（driver 应为 firecracker，suspend_resume=true）
curl -s $BASE/ ; echo
curl -s $BASE/capabilities ; echo   # {"driver":"firecracker","suspend_resume":true,...}

# 端到端测试（FC 模式）
bash scripts/e2e_test.sh --driver firecracker --api-url "$BASE" --api-key "$API_KEY"
# 通过标准包含 resume 后再次 exec，并读取 suspend 前写入的 marker；只看到
# state=running 不足以证明 guest/vsock 已恢复。

# 手动验证 vsock exec 在 microVM 内执行（复现实测报告 §八）
SID=$(curl -s -X POST $BASE/sandboxes -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"cpu":1,"mem_mib":512,"tenant_id":"t","idempotency_key":"k1"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -s -X POST $BASE/sandboxes/$SID/exec -H "Authorization: Bearer $API_KEY" \
  -d '{"cmd":"echo sandbox-ok && uname -r && nproc"}' ; echo
# 期望 rc=0, stdout="sandbox-ok\n5.10.223\n1"
#   5.10.223 = guest kernel（≠ 宿主 6.1.x）→ 确在 microVM 内；nproc=1 = guest 配额
# 走的是 vsock 通道的证据：node-agent 上 /var/lib/sbx/<id>/v.sock 存在（PUT /vsock 生效）
```

**验证端口暴露**（若做了 Step 6.5）—— 含"两个沙盒同开一个端口"：

```bash
# 建两个都暴露 80 的沙盒
SA=$(curl -s -X POST $BASE/sandboxes -H "Authorization: Bearer $API_KEY" \
  -d '{"cpu":1,"mem_mib":512,"tenant_id":"a","services":[{"port":80}]}' | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
SB=$(curl -s -X POST $BASE/sandboxes -H "Authorization: Bearer $API_KEY" \
  -d '{"cpu":1,"mem_mib":512,"tenant_id":"b","services":[{"port":80}]}' | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

# 各自在 guest 里起一个内容不同的 web(注意用绝对路径 /usr/local/bin/python3)
PY=/usr/local/bin/python3
for pair in "$SA:AAA" "$SB:BBB"; do sid=${pair%:*}; msg=${pair#*:}; \
  curl -s -X POST $BASE/sandboxes/$sid/exec -H "Authorization: Bearer $API_KEY" \
    -d "{\"cmd\":\"mkdir -p /web && echo $msg>/web/index.html && cd /web && (setsid $PY -m http.server 80 >/tmp/w.log 2>&1 &); sleep 1; echo ok\"}" >/dev/null; done

# 经反代访问 —— 本地(port-forward)用 localhost:18000;若配了 NLB 用 http://$NLB_HOST
curl -s $BASE/s/$SA/80/    # → AAA
curl -s $BASE/s/$SB/80/    # → BBB   ← 两个都开 80、可能同一 metal，靠 sid 区分不串
curl -s $BASE/s/$SA/3000/  # → 403 port not exposed(仅 services 声明的端口可暴露)
```

**验证自动休眠 / 唤醒**（auto-sleep / auto-wake）—— 一键 e2e(A0~A5,含自动/手动区分):

```bash
# 建议先把 idle 阈值临时调小便于观察(演示用;生产保持 300+)
kubectl set env deployment/sandbox-control-plane -n sandbox-system \
  AUTO_SLEEP_IDLE_S=30 AUTO_SLEEP_SCAN_S=15
kubectl rollout status deployment/sandbox-control-plane -n sandbox-system

# 自动 port-forward + 全流程验证(ALLOW_UNAUTHENTICATED 时可省 --api-key)
bash scripts/autosleep_e2e.sh --idle 30 --api-key "$API_KEY"
# 预期:A2 自动进 slept(≠ suspended)、A3 网关 /s/ 首请求透明唤醒回 running、
#       A4 保活不误睡、A5 手动 suspended 网关不唤醒(409)
```

> 手工快速验证:创建带 opt-in 的沙盒 → 静置 → 观察状态变 `slept`(非 `suspended`)→ `curl $BASE/s/<id>/80/` 透明唤醒。
> ```bash
> SID=$(curl -s -X POST $BASE/sandboxes -H "Authorization: Bearer $API_KEY" \
>   -d '{"image":"web","cpu":2,"mem_mib":2048,"services":[{"port":80,"autostop":true,"autostart":true}],"meta":{"auto_sleep":true,"auto_wake":true}}' \
>   | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
> # 等 idle+扫描周期后:
> curl -s $BASE/sandboxes/$SID | python3 -c "import sys,json;print(json.load(sys.stdin)['state'])"  # → slept
> curl -s $BASE/s/$SID/80/ >/dev/null; curl -s $BASE/sandboxes/$SID | python3 -c "import sys,json;print(json.load(sys.stdin)['state'])"  # → running(被唤醒)
> ```

**验证 P0 高可用编排能力**（reconcile / 心跳 / leader / S3 强一致）：

```bash
# P0-A 节点心跳：node-agent 每 30s 写 nodes 表（起 pod 后等 ~35s）
aws dynamodb scan --table-name claude-sbx-nodes --region us-east-1 \
  --query 'Items[].{node:node_id.S,free_mem:free_mem_mib.N,last_seen:last_seen.S}'
# 期望：每台 node-agent 节点一条，last_seen 随周期刷新
#   ⚠️ 若为空：node-agent 心跳失败。查 node-agent 日志 stderr 有无 [heartbeat] failed；
#      确认 stage2 已给 node-agent IAM 加 dynamodb:PutItem on nodes 表 + env DYNAMODB_NODES_TABLE

# P0-B leader 选举：控制面 2 副本，locks 表只有一个 reconciler 锁、单一 owner
aws dynamodb get-item --table-name claude-sbx-locks --key '{"lock_id":{"S":"reconciler"}}' \
  --region us-east-1 --query 'Item.{owner:owner.S,rvn:rvn.N}'
# 期望：owner 为某副本，rvn 持续自增（每 ~10s +1）= leader 在续租
# 故障转移：kubectl delete pod <leader pod> → 等 ~40s → owner 转移到另一副本

# P0-D S3 强一致：suspend 返回 suspended ⟺ S3 确有快照
curl -s -X POST $BASE/sandboxes/$SID/suspend -H "Authorization: Bearer $API_KEY" | \
  python3 -c "import sys,json;print(json.load(sys.stdin).get('state'))"   # → suspended
aws s3 ls "s3://<snapshot-bucket>/sbx/$SID/" --region us-east-1
# 期望：vm.mem + vm.snapshot（Full 全量快照模式下还会有 rootfs.ext4）都在 → 不变式成立

# P0-E reconcile 漂移：制造 running 但节点无 VM 的漂移记录，等一轮对账（~20-40s）
NODE_IP=$(aws dynamodb scan --table-name claude-sbx-nodes --region us-east-1 --query 'Items[0].ip.S' --output text)
aws dynamodb put-item --table-name claude-sbx-sandboxes --region us-east-1 --item \
  "{\"id\":{\"S\":\"drift-test\"},\"tenant_id\":{\"S\":\"t\"},\"state\":{\"S\":\"running\"},\"driver\":{\"S\":\"firecracker\"},\"node\":{\"S\":\"$NODE_IP\"},\"tap_idx\":{\"N\":\"99\"},\"updated_at\":{\"S\":\"2020-01-01T00:00:00+00:00\"}}"
sleep 40
aws dynamodb get-item --table-name claude-sbx-sandboxes --key '{"id":{"S":"drift-test"}}' \
  --region us-east-1 --query 'Item.{state:state.S,reason:reconcile_reason.S}'
# 期望：state=orphaned, reason=runtime_unknown（reconcile 检出漂移并自动标记）
aws dynamodb delete-item --table-name claude-sbx-sandboxes --key '{"id":{"S":"drift-test"}}' --region us-east-1
```

> 完整 P0 真机测试报告见 **[docs/P0编排加固-真机测试报告-2026-07-07.md](P0编排加固-真机测试报告-2026-07-07.md)**。

**验证 P1/P2 可观测性**（若执行了 Step 6.2）：

```bash
kubectl get pods -n monitoring
kubectl get prometheus sandbox-monitoring-prometheus -n monitoring -o json | \
  jq '{availableReplicas:.status.availableReplicas,podAnnotations:.spec.podMetadata.annotations}'
kubectl get prometheusrules -n monitoring

# 期望：所有 monitoring Pod Ready；Prometheus availableReplicas=1；
# 启用 AMP 时 podAnnotations 含 sandbox.platform/amp-remote-write-role。
```

完整验收还应覆盖：29/29 targets（数量随集群规模变化）、remote-write failed=0、
AMP 查询到 2 个控制面和每个 node-agent、AMG datasource health=`OK`、Dashboard
存在。启用 P2 时还需验证 correlation ID 跨两组件、X-Ray 父子链路和临时 AMG
service account 残留为 0，最后 `terraform plan -detailed-exitcode` 返回 0。详见
[P2 可观测性真机测试报告](P2可观测性-真机测试报告-2026-08-12.md)。

> **排障：**
> - **create/exec 全 503 `control plane not configured`** → 没配 API_KEYS。测试环境快速放行：`kubectl set env deployment/sandbox-control-plane -n sandbox-system ALLOW_UNAUTHENTICATED=1`（生产严禁）。
> - **create 卡住无响应（~90-120s）** → `_pick_node` 探到不可达节点的 `/health` 阻塞。P0 后正常情况节点来自心跳表（死节点按 last_seen 自动剔除），但若心跳表为空回退到 `FC_NODES` 且里面有抖动节点会阻塞。先查心跳表 `aws dynamodb scan --table-name claude-sbx-nodes`；若心跳未起，临时改 `kubectl set env deployment/sandbox-control-plane -n sandbox-system FC_NODES=<稳定IP>`。
> - **nodes 表为空 / 节点发现不到** → node-agent 心跳失败。查 `kubectl logs -n sandbox-system <node-agent-pod>` 有无 `[heartbeat] failed`；确认 node-agent IAM 有 `dynamodb:PutItem` on nodes 表、env 有 `DYNAMODB_NODES_TABLE`。
> - **create 报 `ResourceNotFoundException`** → 漏了 Step 1（DynamoDB 表）。
> - **控制面 Pending / 节点 NotReady 抖动** → cordon 抖动节点，把控制面固定到稳定节点：`kubectl cordon <抖动节点>; kubectl delete pod -n sandbox-system -l app=sandbox-control-plane`。
> - **节点上 FC 资产核查**（SSM 用 `AWS-RunShellScript`，或经 node-agent 容器）：
>   ```bash
>   NA=$(kubectl get pod -n sandbox-system -l app=node-agent -o name | head -1)
>   kubectl exec -n sandbox-system $NA -- ls -l /usr/local/bin/firecracker /opt/sbx/vmlinux /opt/sbx/rootfs.ext4 /dev/kvm
>   ```
> - **LiteLLM**（若部署）：OOMKilled → 调大 limits；单节点 Pending → `--replicas=1`。

---

## Step 10: 使用 API

```bash
# port-forward 本地访问
kubectl port-forward -n sandbox-system svc/sandbox-control-plane 18000:80 &
BASE_URL="http://localhost:18000"
API_KEY="<Step 6 生成的 API_KEY>"

# 创建沙盒
curl -s $BASE_URL/sandboxes \
  -X POST -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${API_KEY}" \
  -d '{"cpu":2,"mem_mib":4096,"tenant_id":"user-1","services":[{"port":8080}]}'

# 等待就绪
curl -H "Authorization: Bearer ${API_KEY}" \
  "$BASE_URL/sandboxes/{id}/wait?state=running"

# 执行命令
curl -s $BASE_URL/sandboxes/{id}/exec \
  -X POST -H "Authorization: Bearer ${API_KEY}" \
  -d '{"cmd":"claude --version"}'

# 挂起（快照到 S3 + 释放内存）
curl -s -X POST -H "Authorization: Bearer ${API_KEY}" \
  $BASE_URL/sandboxes/{id}/suspend

# 恢复（~1.2s）
curl -s -X POST -H "Authorization: Bearer ${API_KEY}" \
  $BASE_URL/sandboxes/{id}/resume

# 销毁
curl -s -X DELETE -H "Authorization: Bearer ${API_KEY}" \
  $BASE_URL/sandboxes/{id}
```

---

## 清理（避免费用）

> ⏱ 顺序：stage2 → phase3（删除 EKS 与沙盒节点，停止主要计算费用）→ stage1。

```bash
ACCT=$(aws sts get-caller-identity --query Account --output text)
S3_BUCKET="my-sandbox-snapshots-${ACCT}"

# 0. 若做过 Step 6.5（端口暴露）：先删 sandbox-proxy Ingress + ingress-nginx（NLB），
#    否则残留 NLB 占 ENI 会让后面 VPC destroy 卡住。
kubectl delete ingress sandbox-proxy -n sandbox-system --ignore-not-found
helm uninstall ingress-nginx -n ingress-nginx 2>/dev/null || true
#    （下面 stage2 destroy 传 create_ingress_nginx=false，terraform 里本就无此 NLB 记录，故手动删）

# 1. 删 stage2（var 必须与 apply 时一致，含 fc_nodes 和所有可观测性开关）。
#    最稳妥做法是 destroy 复用 apply 时的同一份 tfvars。
cd terraform/stage2-control-plane && terraform destroy -auto-approve \
  -var="node_arch=${NODE_ARCH}" \
  -var="fc_nodes=placeholder" \
  -var="sandbox_image=public.ecr.aws/amazonlinux/amazonlinux:2023" \
  -var="control_plane_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/sandbox-control-plane:latest" \
  -var="node_agent_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/node-agent:latest" \
  -var="snapshot_s3_bucket=${S3_BUCKET}" \
  -var="enable_fargate=false" \
  -var="create_ingress_nginx=false" \
  -var="api_keys=placeholder" \
  -var="litellm_master_key=placeholder"

# 若启用了模式 A/B/C，上面的 destroy 还必须带同一组：
#   -var="enable_observability_stack=true"
#   -var="enable_amp_remote_write=true"
#   -var="enable_p2_observability=true"
#   -var="grafana_admin_password=<至少16字符的占位值>"
#   -var="enable_amp_remote_write=true"
#   -var="managed_grafana_workspace_id=<原 workspace id>"
#   -var="managed_grafana_vpc_id=<原 vpc id>"
#   -var='managed_grafana_subnet_ids=["<原subnet-a>","<原subnet-b>"]'
#   -var="managed_grafana_security_group_id=<原 AMG SG>"
# 这会删除 Terraform 创建的 AMP、查询 IAM policy 和 Interface Endpoint；
# 不会删除外部已有的 AMG workspace。销毁前先导出需保留的 Dashboard。

# ⚠️ 若卡在删 sandbox-system namespace（node-agent pod 在 NotReady 节点上无法优雅终止）：
#   kubectl delete pods -n sandbox-system --all --force --grace-period=0
# 强删后 destroy 会在 1-2 分钟内继续完成。

# 2. 删孤儿 pod ENI（节点终止后不自动清理，会让 VPC destroy 卡 7+ 分钟）
VPC_ID=$(aws ec2 describe-vpcs --region us-east-1 \
  --filters "Name=tag:Name,Values=claude-sbx-vpc" --query 'Vpcs[0].VpcId' --output text)
if [ "$VPC_ID" != "None" ] && [ -n "$VPC_ID" ]; then
  for eni in $(aws ec2 describe-network-interfaces --region us-east-1 \
      --filters "Name=vpc-id,Values=$VPC_ID" "Name=status,Values=available" \
      --query 'NetworkInterfaces[].NetworkInterfaceId' --output text); do
    aws ec2 delete-network-interface --region us-east-1 --network-interface-id "$eni" 2>/dev/null || true
  done
fi

# 4. 删 EKS 集群 + 沙盒节点（var 要与 apply 时一致）
MY_IP=$(curl -s https://checkip.amazonaws.com)
ACCT=$(aws sts get-caller-identity --query Account --output text)
cd ../phase3 && terraform destroy -auto-approve \
  -var="node_arch=${NODE_ARCH}" \
  -var="sandbox_instance_type=${SANDBOX_INSTANCE_TYPE}" \
  -var="sandbox_az_index=${SANDBOX_AZ_INDEX}" \
  -var="system_instance_type=${SYSTEM_INSTANCE_TYPE}" \
  -var="system_node_count=${SYSTEM_NODE_COUNT}" \
  -var="rootfs_s3_uri=s3://my-sandbox-snapshots-${ACCT}/${ROOTFS_KEY}" \
  -var="endpoint_public_access_cidrs=[\"${MY_IP}/32\"]"
# VPC 删除卡住 >5min → 删 eks-cluster-sg：
#   SG=$(aws ec2 describe-security-groups --region us-east-1 \
#     --filters "Name=group-name,Values=eks-cluster-sg-claude-sbx-*" \
#     --query 'SecurityGroups[0].GroupId' --output text)
#   [ "$SG" != "None" ] && aws ec2 delete-security-group --region us-east-1 --group-id "$SG"
#
# 若私有子网仍被 GuardDutyManaged endpoint 占用：
#   aws ec2 describe-vpc-endpoints --region "$AWS_REGION" \
#     --filters "Name=vpc-id,Values=${VPC_ID}" \
#     --query 'VpcEndpoints[?Tags[?Key==`GuardDutyManaged`]].VpcEndpointId' --output text
# 确认 endpoint 属于本次待删除 VPC 后，删除它并重新执行 phase3 destroy：
#   aws ec2 delete-vpc-endpoints --region "$AWS_REGION" --vpc-endpoint-ids <vpce-id>

# 5. 显式删除 delete_on_termination=false 遗留的 sandbox 状态 EBS。
#    只删已 available、且带本集群和 sandbox 节点组标签的卷。
for vol in $(aws ec2 describe-volumes --region "$AWS_REGION" \
    --filters "Name=status,Values=available" \
      "Name=tag:eks:cluster-name,Values=claude-sbx" \
      "Name=tag:Name,Values=sandbox_*" \
    --query 'Volumes[].VolumeId' --output text); do
  aws ec2 delete-volume --region "$AWS_REGION" --volume-id "$vol"
done

# 6. 删 DynamoDB（建议彻底删，不要保留）
#    stage1 共 5 张表：sandboxes / events / tap-idx / nodes / locks
#    ⚠️ 彻底删而非保留 —— 保留会遗留上一轮脏数据：旧沙盒记录(下次重建后节点 IP 全变、
#       reconcile 起来会把它们全标 orphaned)、旧 node 心跳、locks 锁、tap_idx counter
#       接着上次的值继续涨。重建仅需 ~10s（PAY_PER_REQUEST 空表零费用），无保留的理由。
cd ../stage1-dynamodb && terraform destroy -auto-approve

# 7. 清理残留（不清理会阻塞下次重建）
aws logs delete-log-group --log-group-name /aws/eks/claude-sbx/cluster --region us-east-1 2>/dev/null || true
aws ecr delete-repository --repository-name claude-sbx --force --region us-east-1 2>/dev/null || true
aws ecr delete-repository --repository-name sandbox-control-plane --force --region us-east-1 2>/dev/null || true
aws ecr delete-repository --repository-name node-agent --force --region us-east-1 2>/dev/null || true
# 只删除本轮上传的架构 rootfs；不要无条件清空可能含历史快照的共享 bucket。
aws s3 rm "s3://${S3_BUCKET}/${ROOTFS_KEY}" --region us-east-1 2>/dev/null || true
# aws s3 rb s3://${S3_BUCKET} --force --region us-east-1 2>/dev/null || true
```

---
