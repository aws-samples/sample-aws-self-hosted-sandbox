# Intel x86（r8i）上线 Checklist

本清单用于把 Firecracker 沙盒节点从 Graviton `c6g.metal` 切换到 Intel x86。
x86 默认 `r8i.8xlarge`，也支持任意其他 `r8i.*` 或 `i7i.*` 规格（建议不低于 8xlarge）。
system 节点不随之切换：控制面仍运行在独立的 On-Demand Graviton 节点组。

> **为什么默认 `r8i` 而不是 `i7i`**：i7i 的本地 NVMe 是 instance store，spot 回收或
> 停机即销毁，存不了方案C 需要幸存的快照；r8i 无本地盘，状态全落
> `delete_on_termination=false` 的持久 EBS，且同配置每月比 i7i 便宜约 $582（按需）/
> $333（1 年 SP）。`i7i.*` 仅保留用于复现既有 i7i 真机报告。

## 1. 区域与配额

```bash
export AWS_REGION=us-east-1
export NODE_ARCH=amd64
export SANDBOX_INSTANCE_TYPE=r8i.8xlarge
export SANDBOX_AZ_INDEX=0
export SYSTEM_INSTANCE_TYPE=m7g.large
export SYSTEM_NODE_COUNT=2

aws ec2 describe-instance-types \
  --region "$AWS_REGION" \
  --instance-types "$SANDBOX_INSTANCE_TYPE" \
  --query 'InstanceTypes[0].{type:InstanceType,arch:ProcessorInfo.SupportedArchitectures,features:ProcessorInfo.SupportedFeatures}' \
  --output table
```

确认目标区域有该规格、架构包含 `x86_64`，并且支持 nested virtualization。
`r8i.8xlarge` 需要 32 个 Standard 实例类别的 On-Demand vCPU 配额（改用 Spot 时
Spot 配额是独立的一套，也要够）。

## 2. 构建 amd64 rootfs 与镜像

```bash
ACCT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="my-sandbox-snapshots-${ACCT}"
export PLATFORM=linux/amd64
export ROOTFS_KEY=rootfs/amd64/min-rootfs.tar.gz

bash scripts/build-min-rootfs.sh "$BUCKET"
bash scripts/build_and_push.sh --region "$AWS_REGION" \
  --control-plane-platform linux/arm64 \
  --node-agent-platform "$PLATFORM"
```

不要复用 arm64 rootfs；不同架构使用独立 S3 key。node-agent 镜像不包含 SSH
私钥，不要再创建或复制 `node-agent/sbx_exec_key`。

> 🔴 如果还要构建 `claude-code` / `openclaw` 预打包模板，**必须在 amd64 机器上跑**。
> Apple Silicon 上 `--platform linux/amd64` 会在 `npm install -g` 中途 qemu 段错误
> （2026-08-13 实测，exit 139）。`min` / `web` 不装 npm 包，跨架构可以。
> 变通做法（一次性 amd64 EC2）见
> `docs/OD-Spot双池-节点预热池-预打包运行环境-真机测试报告-2026-08-13.md` §9。

## 3. 部署 phase3

```bash
cd terraform/phase3
MY_IP=$(curl -s https://checkip.amazonaws.com)

terraform init -upgrade
terraform apply -auto-approve \
  -var="node_arch=${NODE_ARCH}" \
  -var="sandbox_instance_type=${SANDBOX_INSTANCE_TYPE}" \
  -var="sandbox_az_index=${SANDBOX_AZ_INDEX}" \
  -var="system_instance_type=${SYSTEM_INSTANCE_TYPE}" \
  -var="system_node_count=${SYSTEM_NODE_COUNT}" \
  -var="rootfs_s3_uri=s3://${BUCKET}/${ROOTFS_KEY}" \
  -var="rootfs_images=" \
  -var="endpoint_public_access_cidrs=[\"${MY_IP}/32\"]"
```

首次上线先只开 On-Demand 数据面池（默认 `sandbox_od_node_count=1`，
`sandbox_spot_node_count=0`）。Spot 池和节点预热池都是后续单独验证的项：

```bash
# 验完 x86 基线后再加(可分两次 apply)
#   -var="sandbox_spot_node_count=2" -var='sandbox_spot_instance_types=["r8i.8xlarge","r8i.12xlarge"]'
#   -var="sandbox_warm_pool_size=1"
# ⚠️ 开 Spot 前先把 stage2 的 reclaim_auto_evacuate 置 true(默认 DRY-RUN 不真疏散)
```

> **2026-08-13 真机结论，决定这两项现在该不该开**：
> - **Spot 池：先别开**。节点组能建出来，但控制面 `_pick_node` 不看 `capacity-type`，
>   实测 5/5 个沙盒全落 OD 节点 → 开了也不会被用到，只是白付钱。
> - **节点预热池：可以开，但只在 `rootfs_images=` 为空时开**。加速实测有效
>   （terminate → Ready **44s**，冷启动基线 5m03s），但 ASG 不等 cloud-init 就停机，
>   预热出来的节点上命名 rootfs 模板会全部缺失，而 node-agent 对缺失模板**静默回退 min**
>   → `image=claude-code` 会返回 `running` 但 guest 里没有 CLI。

Terraform 会在两个数据面节点组的 Launch Template 中都设置
`cpu_options.nested_virtualization=enabled`，同时创建至少 2 台 On-Demand
Graviton system 节点。若 ASG activity 返回 `InsufficientInstanceCapacity`，
依次尝试 `SANDBOX_AZ_INDEX=0/1/2`，并在 apply/destroy 中保持最终值一致。

## 4. 验证宿主 KVM

```bash
aws eks update-kubeconfig --name claude-sbx --region "$AWS_REGION"
kubectl wait node --all --for=condition=Ready --timeout=1200s
kubectl get nodes -L role,workload-tier,sandbox,kubernetes.io/arch,node.kubernetes.io/instance-type

INSTANCE_ID=$(kubectl get nodes -l sandbox=true \
  -o jsonpath='{.items[0].spec.providerID}' | awk -F/ '{print $NF}')
aws ssm send-command \
  --region "$AWS_REGION" \
  --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["uname -m","ls -l /dev/kvm","sudo /usr/local/bin/firecracker --version"]'
```

期望 system 节点为 `arm64 + m7g.*`，sandbox 节点为 `amd64 + r8i.*`（或 `i7i.*`），且数据节点
`/dev/kvm` 存在。CoreDNS、控制面和 LiteLLM 应在 system 节点，node-agent 应只在
sandbox 节点。

## 5. 部署控制面并跑 E2E

按 `docs/deploy.md` Step 6 部署控制面，所有 stage2 apply 都传
`-var="node_arch=amd64"`，随后执行：

```bash
bash scripts/e2e_test.sh --driver firecracker \
  --api-url http://localhost:18000 \
  --api-key "$API_KEY"
```

通过标准：create、wait、exec、suspend、resume、destroy 全部成功，guest 内
`uname -m` 返回 `x86_64`。resume 后必须再次 exec，并验证 suspend 前写入的数据；
仅检查 API 状态为 `running` 不足以证明 vsock 与 guest 已恢复。

## 6. 清理

按 `docs/deploy.md` 的清理顺序执行 stage2 → phase3 → stage1，并在 phase3 destroy
中传回相同的 `node_arch`、`sandbox_instance_type` 和 `rootfs_s3_uri`。

⚠️ destroy **不会**删数据面的持久状态卷（`delete_on_termination=false`）。开过预热池
或经历过节点替换时会有更多残留（实测一次替换就留下一块 `available` 的 400G 孤儿卷）。
这些卷上只有 `Name` / `eks:cluster-name` / `eks:nodegroup-name` tag，共享账号里
删前必须逐块核对 `eks:cluster-name=claude-sbx`：

```bash
aws ec2 describe-volumes --region "$AWS_REGION" \
  --filters Name=status,Values=available \
  --query 'Volumes[].{id:VolumeId,size:Size,tags:Tags}' --output json
```
