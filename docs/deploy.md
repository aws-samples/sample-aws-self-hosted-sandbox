# 完整部署手册

> 本文件包含从零部署 AWS 沙盒平台的完整步骤，供 Agent 或工程师按序执行。
> 快速入口见根目录 [README.md](../README.md)。

---

## 前提条件

- AWS CLI 已配置（需要权限：EKS / EC2 / IAM / DynamoDB / ECR / S3）
- 已安装：kubectl, terraform (≥1.5), helm, git
- EC2 vCPU 服务配额：c6g.metal = 64 vCPU，默认配额通常不够，需提前申请
- 生产部署必须设置 `API_KEYS`（见 Step 6 注意事项）

---

## ⚠️ 注意事项

1. **认证**：控制面默认关闭对外 Ingress（`expose_control_plane=false`），本地通过 port-forward 访问。生产开启 Ingress 时必须同时配置 `api_keys` variable 和 TLS，否则控制面启动后会拒绝所有受保护请求（503）。
2. **不要用 kata-deploy DaemonSet**：本方案通过 Karpenter EC2NodeClass.userData 在节点 bootstrap 阶段预装 Kata（见 Step 7）。kata-deploy DaemonSet 会在节点已运行时重启 containerd，导致 c6g.metal 节点 hang 约 12 分钟，触发 ASG 节点替换死循环。
3. **LiteLLM 必须传 master key**：`litellm_master_key` 无默认值，terraform apply 时必须传入（如 `openssl rand -hex 32`）。
4. **arm64 镜像**：控制面和 node-agent 镜像必须在 arm64 机器上构建（M 系列 Mac、Graviton EC2 或 .metal 节点）。
5. **费用提醒**：c6g.metal 按小时计费（约 $2.3/hr），EKS 控制面 $0.1/hr，用完务必执行清理步骤。

---

## Step 0: 克隆代码库

```bash
git clone https://github.com/teaguexiao/aws-self-hosted-sandbox.git
cd aws-self-hosted-sandbox
export AWS_REGION=us-east-1
```

---

## Step 1: 创建 DynamoDB 状态表

```bash
cd terraform/stage1-dynamodb
terraform init && terraform apply -auto-approve
# 验证
aws dynamodb list-tables --region us-east-1 | grep claude-sbx
```

---

## Step 2: 创建 EKS 集群 + .metal 节点组

```bash
cd ../phase3
MY_IP=$(curl -s https://checkip.amazonaws.com)
terraform init && terraform apply -auto-approve \
  -var="endpoint_public_access_cidrs=[\"${MY_IP}/32\"]"
# EKS 控制面约 10-12 分钟，加 .metal 节点组冷启动整体约 15 分钟
aws eks update-kubeconfig --name claude-sbx --region us-east-1
kubectl wait node --all --for=condition=Ready --timeout=900s
```

---

## Step 3: 创建 kata-qemu RuntimeClass

```bash
kubectl apply -f - <<'RUNTIMECLASS'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: kata-qemu
handler: kata-qemu
overhead:
  podFixed:
    cpu: 250m
    memory: 160Mi
scheduling:
  # 只调度到 Step 7 由 Karpenter UserData 预装好 Kata 的 .metal 节点
  nodeSelector:
    katacontainers.io/kata-runtime: "true"
RUNTIMECLASS

kubectl get runtimeclass kata-qemu
```

---

## Step 4: 安装 ingress-nginx（共享 NLB）

```bash
# 必须指定 namespace，否则与 Step 6 的 Terraform 冲突
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx --create-namespace \
  --set controller.service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-type"=nlb \
  --set controller.ingressClassResource.default=true
# 等待 NLB 分配外部地址（1-3 分钟）
kubectl get svc -n ingress-nginx ingress-nginx-controller --watch
```

---

## Step 5: 创建 ECR 仓库并构建 arm64 镜像

```bash
# claude-sbx 仓库已由 Step 2 的 Terraform 自动创建，只需建以下两个：
ACCT=$(aws sts get-caller-identity --query Account --output text)
aws ecr create-repository --repository-name sandbox-control-plane --region us-east-1 2>/dev/null || true
aws ecr create-repository --repository-name node-agent --region us-east-1 2>/dev/null || true

# 方式 A：本地 arm64 机器（M 系列 Mac 或 Graviton EC2）
bash scripts/build_and_push.sh

# 方式 B：在 .metal 节点上原生构建（x86 机器无 buildx 时推荐，见脚本注释）
```

---

## Step 6: 部署控制面 + LiteLLM + Karpenter IAM

```bash
cd terraform/stage2-control-plane
terraform init

ACCT=$(aws sts get-caller-identity --query Account --output text)
S3_BUCKET="my-sandbox-snapshots-${ACCT}"
aws s3 mb s3://${S3_BUCKET} --region us-east-1 2>/dev/null || true

# 生成随机 API key（生产必填，不能留空）
API_KEY=$(openssl rand -hex 32)
LITELLM_KEY=$(openssl rand -hex 32)
echo "API_KEY: $API_KEY  （保存好，后续 curl 鉴权用）"

# Step 4 已安装 ingress-nginx，加 create_ingress_nginx=false 避免冲突
terraform apply -auto-approve \
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
# - IRSA 角色（控制面/node-agent/LiteLLM/Karpenter）
# - Karpenter Worker Node IAM Role + EKS Access Entry（节点 TLS bootstrap 必需）
# - K8s 资源（sandbox-system / litellm namespace）
# - api-keys Secret（API_KEY 注入控制面）
```

> **常见问题：** Terraform `Unexpected Identity Change` 错误 → 清理 state 重试：
> ```bash
> terraform state rm kubernetes_deployment.litellm kubernetes_deployment.control_plane
> terraform apply ...
> ```

---

## Step 7: 手动安装 Karpenter + 部署 NodePool

```bash
# 某些环境需先移除 Docker credential store（Helm OCI 需要）
python3 -c "
import json, pathlib
cfg = pathlib.Path.home() / '.docker/config.json'
if cfg.exists():
    d = json.loads(cfg.read_text())
    d.pop('credsStore', None)
    cfg.write_text(json.dumps(d))
    print('credsStore removed')
"

ACCT=$(aws sts get-caller-identity --query Account --output text)
CLUSTER_ENDPOINT=$(aws eks describe-cluster --name claude-sbx --query 'cluster.endpoint' --output text)
KARPENTER_ROLE_ARN="arn:aws:iam::${ACCT}:role/claude-sbx-karpenter"
KARPENTER_NODE_ROLE="claude-sbx-karpenter-node"
# 或查询：aws iam list-roles --query 'Roles[?contains(RoleName,`karpenter-node`)].RoleName' --output text

helm upgrade --install karpenter \
  oci://public.ecr.aws/karpenter/karpenter --version 1.3.3 \
  --namespace karpenter --create-namespace \
  --set "settings.clusterName=claude-sbx" \
  --set "settings.clusterEndpoint=${CLUSTER_ENDPOINT}" \
  --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=${KARPENTER_ROLE_ARN}" \
  --set "controller.resources.limits.memory=1Gi"

# 单节点集群缩为 1 副本（多副本 anti-affinity 会阻塞）
kubectl scale deployment karpenter -n karpenter --replicas=1
kubectl rollout status deployment/karpenter -n karpenter --timeout=120s

# 部署 NodePool + EC2NodeClass
# Kata 由 userData 在 bootstrap 阶段预装（kubelet 注册前），节点 30-60s Ready，零抖动
cat > /tmp/kata-metal.yaml <<'NODEPOOL'
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: kata-metal
spec:
  amiSelectorTerms:
    - alias: al2023@latest
  role: __KARPENTER_NODE_ROLE__
  subnetSelectorTerms:
    - tags:
        kubernetes.io/role/elb: "1"
  securityGroupSelectorTerms:
    - tags:
        kubernetes.io/cluster/claude-sbx: owned
  blockDeviceMappings:
    - deviceName: /dev/xvda
      ebs:
        volumeSize: 200Gi
        volumeType: gp3
  userData: |
    #!/bin/bash
    set -euxo pipefail
    KATA_VERSION="3.31.0"; ARCH="arm64"
    cd /tmp
    curl -fsSL "https://github.com/kata-containers/kata-containers/releases/download/${KATA_VERSION}/kata-static-${KATA_VERSION}-${ARCH}.tar.zst" -o kata.tar.zst
    tar --use-compress-program=unzstd -xf kata.tar.zst -C /
    mkdir -p /opt/kata/containerd/config.d
    cat > /opt/kata/containerd/config.d/kata-deploy.toml <<'TOML'
    [plugins."io.containerd.cri.v1.runtime".containerd.runtimes.kata-qemu]
    runtime_type = "io.containerd.kata-qemu.v2"
    runtime_path = "/opt/kata/bin/containerd-shim-kata-v2"
    privileged_without_host_devices = true
    pod_annotations = ["io.katacontainers.*"]

    [plugins."io.containerd.cri.v1.runtime".containerd.runtimes.kata-qemu.options]
    ConfigPath = "/opt/kata/share/defaults/kata-containers/configuration-qemu.toml"
    TOML
    if ! grep -q "kata-deploy.toml" /etc/containerd/config.toml 2>/dev/null; then
      if grep -q "^imports" /etc/containerd/config.toml 2>/dev/null; then
        sed -i 's#^imports = \[#imports = ["/opt/kata/containerd/config.d/kata-deploy.toml", #' /etc/containerd/config.toml
      else
        sed -i '1i imports = ["/opt/kata/containerd/config.d/kata-deploy.toml"]' /etc/containerd/config.toml
      fi
    fi
    systemctl restart containerd && systemctl enable containerd
---
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: kata-metal
spec:
  template:
    metadata:
      labels:
        sandbox: "true"
        katacontainers.io/kata-runtime: "true"
    spec:
      requirements:
        - {key: node.kubernetes.io/instance-type, operator: In, values: ["c6g.metal"]}
        - {key: kubernetes.io/arch, operator: In, values: ["arm64"]}
        - {key: karpenter.sh/capacity-type, operator: In, values: ["on-demand"]}
      taints:
        - {key: kata-dedicated, value: "true", effect: NoSchedule}
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: kata-metal
  disruption:
    consolidationPolicy: WhenEmpty
    consolidateAfter: 30m
NODEPOOL

sed -i.bak "s#__KARPENTER_NODE_ROLE__#${KARPENTER_NODE_ROLE}#" /tmp/kata-metal.yaml
kubectl apply -f /tmp/kata-metal.yaml
kubectl get nodepools && kubectl get ec2nodeclasses
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

## Step 9: 验证部署

```bash
# 等待镜像拉取（ECR 首次约 1-3 分钟）
kubectl rollout status deployment/sandbox-control-plane -n sandbox-system --timeout=300s
kubectl rollout status deployment/litellm -n litellm --timeout=300s

kubectl get pods -n sandbox-system   # 控制面 2/2 + node-agent DaemonSet
kubectl get pods -n litellm           # LiteLLM 1/1
kubectl get nodepools                 # kata-metal READY=True

# 运行端到端测试（port-forward 模式，实测 ALL TESTS PASSED）
bash scripts/e2e_test.sh
```

> **LiteLLM 常见问题：**
> - OOMKilled → `kubectl set resources deployment/litellm -n litellm --limits=cpu=2,memory=4Gi`
> - 单节点 Pending（anti-affinity）→ `kubectl scale deployment/litellm -n litellm --replicas=1`

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

```bash
ACCT=$(aws sts get-caller-identity --query Account --output text)
S3_BUCKET="my-sandbox-snapshots-${ACCT}"

# 1. 删 stage2（K8s 资源/LiteLLM/Karpenter IAM）
cd terraform/stage2-control-plane && terraform destroy -auto-approve \
  -var="sandbox_image=public.ecr.aws/amazonlinux/amazonlinux:2023" \
  -var="control_plane_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/sandbox-control-plane:latest" \
  -var="node_agent_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/node-agent:latest" \
  -var="snapshot_s3_bucket=${S3_BUCKET}" \
  -var="enable_fargate=false" \
  -var="create_ingress_nginx=false" \
  -var="api_keys=placeholder" \
  -var="litellm_master_key=placeholder"

# 2. 回收 Karpenter 节点 + 删 NLB（否则 VPC destroy 会因依赖卡住）
kubectl delete nodepool kata-metal 2>/dev/null || true
kubectl delete ec2nodeclass kata-metal 2>/dev/null || true
sleep 60
helm uninstall karpenter -n karpenter 2>/dev/null || true
helm uninstall ingress-nginx -n ingress-nginx 2>/dev/null || true
for arn in $(aws elbv2 describe-load-balancers --region us-east-1 \
    --query 'LoadBalancers[?Type==`network`].LoadBalancerArn' --output text); do
  aws elbv2 delete-load-balancer --region us-east-1 --load-balancer-arn "$arn"
done
sleep 30

# 3. 删孤儿 pod ENI（Karpenter 节点终止后不自动清理，会让 VPC destroy 卡 7+ 分钟）
VPC_ID=$(aws ec2 describe-vpcs --region us-east-1 \
  --filters "Name=tag:Name,Values=claude-sbx-vpc" --query 'Vpcs[0].VpcId' --output text)
if [ "$VPC_ID" != "None" ] && [ -n "$VPC_ID" ]; then
  for eni in $(aws ec2 describe-network-interfaces --region us-east-1 \
      --filters "Name=vpc-id,Values=$VPC_ID" "Name=status,Values=available" \
      --query 'NetworkInterfaces[].NetworkInterfaceId' --output text); do
    aws ec2 delete-network-interface --region us-east-1 --network-interface-id "$eni" 2>/dev/null || true
  done
fi

# 4. 删 EKS 集群（约 15-20 分钟）
MY_IP=$(curl -s https://checkip.amazonaws.com)
cd ../phase3 && terraform destroy -auto-approve \
  -var="endpoint_public_access_cidrs=[\"${MY_IP}/32\"]"
# VPC 删除卡住 >5min → 删 eks-cluster-sg：
#   SG=$(aws ec2 describe-security-groups --region us-east-1 \
#     --filters "Name=group-name,Values=eks-cluster-sg-claude-sbx-*" \
#     --query 'SecurityGroups[0].GroupId' --output text)
#   [ "$SG" != "None" ] && aws ec2 delete-security-group --region us-east-1 --group-id "$SG"

# 5. 删 DynamoDB
cd ../stage1-dynamodb && terraform destroy -auto-approve

# 6. 清理残留（不清理会阻塞下次重建）
aws logs delete-log-group --log-group-name /aws/eks/claude-sbx/cluster --region us-east-1 2>/dev/null || true
aws ecr delete-repository --repository-name claude-sbx --force --region us-east-1 2>/dev/null || true
# aws s3 rb s3://${S3_BUCKET} --force --region us-east-1 2>/dev/null || true
```
