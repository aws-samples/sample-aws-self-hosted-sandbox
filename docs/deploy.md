# 完整部署手册

> 本文件包含从零部署 AWS 沙盒平台的完整步骤，供 Agent 或工程师按序执行。
> 快速入口见根目录 [README.md](../README.md)。

---

## 后端：裸 Firecracker microVM

本平台后端为**裸 Firecracker microVM + node-agent DaemonSet**：支持 suspend/resume 亚秒恢复 +
持久 EBS 跨机快照（状态卷落持久 EBS + Diff 增量内存快照），成本优先。控制面通过 HTTP 直管每台 .metal 节点上的
node-agent，不依赖 K8s 编排沙盒本身。

> （历史上曾有可插拔的 Kata-on-EKS 后端，因无法快照/恢复、与本平台 spot 疏散核心诉求不符，已移除。
> 本手册即 Firecracker 单一主线，无需再选 driver。）

---

## 前提条件

- AWS CLI 已配置（需要权限：EKS / EC2 / IAM / DynamoDB / ECR / S3）
- 已安装：kubectl, terraform (≥1.5), helm, git, docker（构建 arm64 镜像/rootfs 用）
- EC2 vCPU 服务配额：c6g.metal = 64 vCPU，默认配额通常不够，需提前申请
- 生产部署必须设置 `API_KEYS`（见 Step 6 注意事项）

---

## ⚠️ 注意事项（含实测踩坑，务必先读）

1. **认证 = 硬门槛**：控制面若 `API_KEYS` 未设、又没设 `ALLOW_UNAUTHENTICATED=1`，则**所有写操作（create/exec/suspend…）直接返回 503 `control plane not configured`**。生产必须传 `-var="api_keys=..."`；本地测试可给控制面 deployment 加 env `ALLOW_UNAUTHENTICATED=1`（见 Step 9 排障）。
2. **DynamoDB 表必须先建**（Step 1）。漏了这步，控制面 create 会报 `ResourceNotFoundException`（boto3 找不到表），且报错发生在业务逻辑里、不易一眼看出。
3. **`fc_nodes` 是 fallback，节点发现优先走 DynamoDB 心跳表**：P0 加固后 node-agent 每 30s 写 `claude-sbx-nodes` 表，控制面 `_pick_node` 优先从心跳表选活节点（按 `last_seen` 超时剔除死节点），`fc_nodes` 仅在心跳表为空时兜底。**首次部署 fc_nodes 仍建议只填稳定节点**（心跳还没写起来时靠它），但节点增减后无需再改 `fc_nodes` + 重启控制面——心跳表会自动反映。查活节点：`aws dynamodb scan --table-name claude-sbx-nodes --query 'Items[].{node:node_id.S,last_seen:last_seen.S}'`。
4. **rootfs 必须是含 vsock agent 的 min-rootfs**：exec 走 vsock 通道，需要 `scripts/build-min-rootfs.sh` 产出的 rootfs（内含 `/sbin/vsock-exec-agent.py`，sbxinit 后台启动）。**别用 phase3 `rootfs_s3_uri` 的默认 juicefs 版**——apply phase3 时必须显式传 `-var="rootfs_s3_uri=s3://<bucket>/rootfs/min-rootfs.tar.gz"`（见 Step 1.5 + Step 2）。
5. **.metal 节点反复 NotReady / ASG 替换循环 —— 真因是 ASG grace period 太短，已固化修复**：c6g.metal 过 EC2 status check 需 5-10 分钟，而 EKS 托管节点组建的 ASG 默认 health check grace period 仅 **15 秒** → 节点刚起就被判 unhealthy 替换 → 无限替换循环，永远收敛不到全 Ready（2026-07-07 实测定位，纠正了旧认知"暂态自愈"）。**`terraform/phase3/main.tf` 已用 `null_resource.metal_asg_grace_period` 固化 grace period=900s，apply 时自动 patch，正常情况无需干预**。若仍见反复替换：`aws autoscaling describe-scaling-activities --auto-scaling-group-name <asg>` 看 cause 是否 "EC2 instance status checks failure"，`aws ec2 describe-instance-status --instance-ids <iid>` 看 status check 是否卡在 initializing/impaired；确认 grace period 已生效：`aws autoscaling describe-auto-scaling-groups ... --query '...HealthCheckGracePeriod'` 应为 900。**给足 15-20 分钟等 metal 过 status check + kubelet 注册**，别在头几分钟手动删节点。
6. **arm64 镜像 + rootfs**：控制面/node-agent 镜像**和** min-rootfs 都必须在 arm64 机器上构建（M 系列 Mac、Graviton EC2 或 .metal 节点）。Mac 上若用 colima：`colima start --arch aarch64`。
8. **LiteLLM 必须传 master key**：`litellm_master_key` 无默认值，terraform apply 时必须传入（如 `openssl rand -hex 32`）。
9. **SSM 排障用 `AWS-RunShellScript`**：本账号 `AWS-RunShellCommand`（旧名）不可用，`aws ssm send-command` 要用 document 名 `AWS-RunShellScript`。
10. **费用提醒**：c6g.metal 按小时计费（约 $2.3/hr/台，FC 默认起 2 台 = ~$4.6/hr），EKS 控制面 $0.1/hr，用完务必执行【清理】步骤。清理时 stage2 destroy 若卡在删 `sandbox-system` namespace，多半是 node-agent pod 在 NotReady 节点上无法优雅终止 → `kubectl delete pods -n sandbox-system --all --force --grace-period=0` 解除。

---

## Step 0: 克隆代码库

```bash
git clone https://github.com/teaguexiao/aws-self-hosted-sandbox.git
cd aws-self-hosted-sandbox
export AWS_REGION=us-east-1
```

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
用 `build-min-rootfs.sh` 构建（**arm64 机器**上跑）：

```bash
cd ../..   # 回到仓库根
ACCT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="my-sandbox-snapshots-${ACCT}"
aws s3 mb "s3://${BUCKET}" --region us-east-1 2>/dev/null || true

# SSH 兜底通道用的密钥（vsock 主通道不依赖它，但脚本要求存在；已被 .gitignore 忽略）
mkdir -p .sbxkeys
[ -f .sbxkeys/sbx_exec ] || ssh-keygen -t ed25519 -N "" -f .sbxkeys/sbx_exec -C sbx-exec
cp .sbxkeys/sbx_exec node-agent/sbx_exec_key   # node-agent 镜像构建需要（Dockerfile COPY）

# 构建 + 上传 → s3://<bucket>/rootfs/min-rootfs.tar.gz
bash scripts/build-min-rootfs.sh "${BUCKET}"

# 验证 vsock agent 确实进了 rootfs（可选）
aws s3 cp "s3://${BUCKET}/rootfs/min-rootfs.tar.gz" /tmp/r.tgz --region us-east-1
tar tzf /tmp/r.tgz | grep -E 'sbin/(vsock-exec-agent.py|sbxinit)$'
```

> Mac 上 docker 未起：`colima start --cpu 4 --memory 8 --arch aarch64`。

---

## Step 1.6: 构建自定义镜像 / rootfs 模板（可选，vibe coding / web demo 用）

让 `image` 字段生效——沙盒按 image 选不同 rootfs 模板。`build-rootfs-image.sh <name> <bucket>`
产出 `rootfs-{name}.tar.gz`,节点会造成 `/opt/sbx/rootfs-{name}.ext4`,create `image={name}` 时 CoW 它。
内置 **`web`** 预设:自带 demo 首页 + 开机自起 :80 → 端口暴露打开即见站点。

```bash
# 构建 web 模板(与 min 同基底,叠加 demo 站点 + 开机自起 :80)
bash scripts/build-rootfs-image.sh web "${BUCKET}"
# → 上传到 s3://<bucket>/rootfs/rootfs-web.tar.gz
```

> 节点在 Step 2 apply 时按 `rootfs_images`(默认含 `web`)拉取这些模板造 ext4。
> **本步须在 Step 2 之前完成**(节点 userData 启动时拉);漏了则 create `image=web` 会回退默认 min(不报错)。
> 未列出的 image → 同样回退 min。控制面 `SANDBOX_IMAGES`(默认 `min,web`)决定 Portal 下拉列表。

---

## Step 2: 创建 EKS 集群 + .metal 节点组（传 rootfs_s3_uri）

```bash
cd terraform/phase3
MY_IP=$(curl -s https://checkip.amazonaws.com)   # 若出口 IP 不固定（NAT 池），用覆盖网段如 x.y.z.0/24
ACCT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="my-sandbox-snapshots-${ACCT}"

terraform init && terraform apply -auto-approve \
  -var="node_arch=arm64" \
  -var="rootfs_s3_uri=s3://${BUCKET}/rootfs/min-rootfs.tar.gz" \
  -var="rootfs_images=web" \
  -var="endpoint_public_access_cidrs=[\"${MY_IP}/32\"]"
# rootfs_images(默认 web):节点额外拉 rootfs-{name}.tar.gz 造 /opt/sbx/rootfs-{name}.ext4 模板;
#   须在 Step 1.6 先构建上传对应模板。不需要自定义镜像可传 -var="rootfs_images="。
# EKS 控制面约 10-12 分钟，加 .metal 节点组冷启动整体约 15 分钟
# 默认起 2 台 c6g.metal（跨机快照演示需 2 台；只测 exec 可改 min/max/desired=1）
aws eks update-kubeconfig --name claude-sbx --region us-east-1
kubectl wait node --all --for=condition=Ready --timeout=900s
```

> ⚠️ `rootfs_s3_uri` 不传 → 用默认 juicefs 版 rootfs（无 vsock agent）→ exec 掉到 SSH 兜底并因 sbxinit 硬编码 IP 失败（见注意事项 4）。
> ⚠️ .metal 节点可能冷启动抖动（NotReady）；记下**稳定 Ready** 的节点内网 IP，Step 6 的 `fc_nodes` 只填稳定节点（见注意事项 3/5）。
>
> 直接进入 Step 5（构建镜像）→ Step 6（部署控制面）。POC 用 kubectl port-forward 访问控制面，
> 无需 ingress-nginx；沙盒节点即 phase3 的 .metal 托管节点组，无需 Karpenter。

---

## Step 5: 创建 ECR 仓库并构建 arm64 镜像

```bash
# claude-sbx 仓库已由 Step 2 的 Terraform 自动创建，只需建以下两个：
ACCT=$(aws sts get-caller-identity --query Account --output text)
aws ecr create-repository --repository-name sandbox-control-plane --region us-east-1 2>/dev/null || true
aws ecr create-repository --repository-name node-agent --region us-east-1 2>/dev/null || true

# 方式 A：本地 arm64 机器（M 系列 Mac 或 Graviton EC2）
# 前置：Step 1.5 已生成 node-agent/sbx_exec_key（Dockerfile COPY 需要），否则镜像构建失败
bash scripts/build_and_push.sh

# 方式 B：在 .metal 节点上原生构建（x86 机器无 buildx 时推荐，见脚本注释）
```

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

# FC 模式关键：拿【稳定 Ready】的 .metal 节点内网 IP 拼 fc_nodes（只填稳定节点！见注意事项 3）
FC_NODES=$(kubectl get nodes -o jsonpath='{range .items[*]}{.status.addresses[?(@.type=="InternalIP")].address}{","}{end}' | sed 's/,$//')
echo "FC_NODES=$FC_NODES  （若含 NotReady 节点，手动改成只留稳定的）"

terraform apply -auto-approve \
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

> ⚠️ **FC_NODES 只填稳定节点**：控制面 `_pick_node` 串行探每个节点 `/health`，遇不可达节点阻塞最长 120s（表现为 create "卡住无响应"）。若节点抖动，先 `kubectl get nodes` 确认，只把稳定的 IP 传给 `fc_nodes`。改完可热更新：`kubectl set env deployment/sandbox-control-plane -n sandbox-system FC_NODES=<稳定IP>`。
>
> **常见问题：** Terraform `Unexpected Identity Change` 错误 → 清理 state 重试：
> ```bash
> terraform state rm kubernetes_deployment.control_plane
> terraform apply ...
> ```

跳到 Step 9 验证。

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
bash scripts/e2e_test.sh --driver firecracker --api-url $BASE

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

> ⏱ 顺序：stage2 → phase3（删 EKS+metal，真正停止 metal 计费的一步，约 15-20 分钟）→ stage1。
> phase3 destroy 里 node group 删除本身就要 3-6 分钟，metal 实例到那时才终止，属正常。

```bash
ACCT=$(aws sts get-caller-identity --query Account --output text)
S3_BUCKET="my-sandbox-snapshots-${ACCT}"

# 0. 若做过 Step 6.5（端口暴露）：先删 sandbox-proxy Ingress + ingress-nginx（NLB），
#    否则残留 NLB 占 ENI 会让后面 VPC destroy 卡住。
kubectl delete ingress sandbox-proxy -n sandbox-system --ignore-not-found
helm uninstall ingress-nginx -n ingress-nginx 2>/dev/null || true
#    （下面 stage2 destroy 传 create_ingress_nginx=false，terraform 里本就无此 NLB 记录，故手动删）

# 1. 删 stage2（var 要与 apply 时一致，含 fc_nodes）
cd terraform/stage2-control-plane && terraform destroy -auto-approve \
  -var="fc_nodes=placeholder" \
  -var="sandbox_image=public.ecr.aws/amazonlinux/amazonlinux:2023" \
  -var="control_plane_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/sandbox-control-plane:latest" \
  -var="node_agent_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/node-agent:latest" \
  -var="snapshot_s3_bucket=${S3_BUCKET}" \
  -var="enable_fargate=false" \
  -var="create_ingress_nginx=false" \
  -var="api_keys=placeholder" \
  -var="litellm_master_key=placeholder"

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

# 4. 删 EKS 集群 + metal 节点（约 15-20 分钟；var 要与 apply 时一致）
#    node group 删除本身 3-6 分钟，metal 实例在此期间终止（计费到实例 terminated 为止）
MY_IP=$(curl -s https://checkip.amazonaws.com)
ACCT=$(aws sts get-caller-identity --query Account --output text)
cd ../phase3 && terraform destroy -auto-approve \
  -var="node_arch=arm64" \
  -var="rootfs_s3_uri=s3://my-sandbox-snapshots-${ACCT}/rootfs/min-rootfs.tar.gz" \
  -var="endpoint_public_access_cidrs=[\"${MY_IP}/32\"]"
# VPC 删除卡住 >5min → 删 eks-cluster-sg：
#   SG=$(aws ec2 describe-security-groups --region us-east-1 \
#     --filters "Name=group-name,Values=eks-cluster-sg-claude-sbx-*" \
#     --query 'SecurityGroups[0].GroupId' --output text)
#   [ "$SG" != "None" ] && aws ec2 delete-security-group --region us-east-1 --group-id "$SG"

# 5. 删 DynamoDB（建议彻底删，不要保留）
#    stage1 共 5 张表：sandboxes / events / tap-idx / nodes / locks
#    ⚠️ 彻底删而非保留 —— 保留会遗留上一轮脏数据：旧沙盒记录(下次重建后节点 IP 全变、
#       reconcile 起来会把它们全标 orphaned)、旧 node 心跳、locks 锁、tap_idx counter
#       接着上次的值继续涨。重建仅需 ~10s（PAY_PER_REQUEST 空表零费用），无保留的理由。
cd ../stage1-dynamodb && terraform destroy -auto-approve

# 6. 清理残留（不清理会阻塞下次重建）
aws logs delete-log-group --log-group-name /aws/eks/claude-sbx/cluster --region us-east-1 2>/dev/null || true
aws ecr delete-repository --repository-name claude-sbx --force --region us-east-1 2>/dev/null || true
# aws s3 rb s3://${S3_BUCKET} --force --region us-east-1 2>/dev/null || true
```

---
