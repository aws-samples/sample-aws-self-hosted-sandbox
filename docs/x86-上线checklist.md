# Intel x86（i7i）上线 Checklist

本清单用于把 Firecracker 沙盒节点从 Graviton `c6g.metal` 切换到 Intel x86 i7i。
x86 默认 `i7i.8xlarge`，也可选择任意其他 `i7i.*` 规格。

## 1. 区域与配额

```bash
export AWS_REGION=us-east-1
export NODE_ARCH=amd64
export SANDBOX_INSTANCE_TYPE=i7i.8xlarge

aws ec2 describe-instance-types \
  --region "$AWS_REGION" \
  --instance-types "$SANDBOX_INSTANCE_TYPE" \
  --query 'InstanceTypes[0].{type:InstanceType,arch:ProcessorInfo.SupportedArchitectures,features:ProcessorInfo.SupportedFeatures}' \
  --output table
```

确认目标区域有该规格、架构包含 `x86_64`，并且支持 nested virtualization。
`i7i.8xlarge` 需要 32 个对应类别的 On-Demand vCPU 配额。

## 2. 构建 amd64 rootfs 与镜像

```bash
ACCT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="my-sandbox-snapshots-${ACCT}"
export PLATFORM=linux/amd64
export ROOTFS_KEY=rootfs/amd64/min-rootfs.tar.gz

bash scripts/build-min-rootfs.sh "$BUCKET"
bash scripts/build_and_push.sh --region "$AWS_REGION" --platform "$PLATFORM"
```

不要复用 arm64 rootfs；不同架构使用独立 S3 key。

## 3. 部署 phase3

```bash
cd terraform/phase3
MY_IP=$(curl -s https://checkip.amazonaws.com)

terraform init -upgrade
terraform apply -auto-approve \
  -var="node_arch=${NODE_ARCH}" \
  -var="sandbox_instance_type=${SANDBOX_INSTANCE_TYPE}" \
  -var="rootfs_s3_uri=s3://${BUCKET}/${ROOTFS_KEY}" \
  -var="rootfs_images=" \
  -var="endpoint_public_access_cidrs=[\"${MY_IP}/32\"]"
```

Terraform 会在 EKS 节点组的 Launch Template 中设置
`cpu_options.nested_virtualization=enabled`。

## 4. 验证宿主 KVM

```bash
aws eks update-kubeconfig --name claude-sbx --region "$AWS_REGION"
kubectl wait node --all --for=condition=Ready --timeout=1200s
kubectl get nodes -L kubernetes.io/arch,node.kubernetes.io/instance-type

INSTANCE_ID=$(kubectl get nodes -o jsonpath='{.items[0].spec.providerID}' | awk -F/ '{print $NF}')
aws ssm send-command \
  --region "$AWS_REGION" \
  --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["uname -m","ls -l /dev/kvm","sudo /usr/local/bin/firecracker --version"]'
```

期望节点为 `amd64 + i7i.*`，且 `/dev/kvm` 存在。

## 5. 部署控制面并跑 E2E

按 `docs/deploy.md` Step 6 部署控制面，所有 stage2 apply 都传
`-var="node_arch=amd64"`，随后执行：

```bash
bash scripts/e2e_test.sh --driver firecracker --api-url http://localhost:18000
```

通过标准：create、wait、exec、suspend、resume、destroy 全部成功，guest 内
`uname -m` 返回 `x86_64`。

## 6. 清理

按 `docs/deploy.md` 的清理顺序执行 stage2 → phase3 → stage1，并在 phase3 destroy
中传回相同的 `node_arch`、`sandbox_instance_type` 和 `rootfs_s3_uri`。
