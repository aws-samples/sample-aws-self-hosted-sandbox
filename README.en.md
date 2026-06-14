# AWS Self-Hosted AI Agent Sandbox Platform

> Build your own Fly.io-style Firecracker microVM sandbox on AWS — lower cost, full control, data stays in your account.

[中文 / Chinese](README.md) · **English**

---

### Overview

A production-grade AI Agent sandbox platform built on AWS, replicating Fly.io's Firecracker microVM architecture — with lower cost, full data sovereignty, and native Kubernetes integration.

- **True microVM isolation**: Each sandbox runs in an independent Firecracker/Kata guest kernel — identical behavior to bare metal
- **Pluggable backends**: Same API, switch between Kata-on-EKS (orchestration-first) or bare Firecracker (cost-first)
- **Snapshot-driven cost control**: Idle sandboxes snapshot to S3, resume in ~1.2s
- **Fly Machines-style API**: create/wait/suspend/resume/exec/locate with idempotency, optimistic locking, capability model
- **Zero credentials in sandboxes**: Bedrock credentials live only in LiteLLM Pod's IRSA role

### Use Cases

| Use Case | Description |
|---|---|
| **Claude Code** | fork/exec-heavy, file-watch-intensive, nested processes — microVM guarantees bare-metal fidelity |
| **OpenClaw / Hermes** | Conversational agents needing multi-tenant isolation and autoscaling |
| **OpenAI Codex / Code-gen Agents** | Arbitrary code execution with VM-level security boundary |
| **Long-horizon Agentic Tasks** | Pause/resume workflows, snapshot session state mid-task |
| **SaaS Sandbox Service** | Expose isolated execution to end users, multi-tenant, usage-based billing |
| **CI/CD Sandboxes** | Isolated build/test environments with full OS access |

### Comparison with Alternatives

| Feature | This (AWS Self-Hosted) | E2B | Fly.io Machines | AWS AgentCore |
|---|---|---|---|---|
| **Isolation** | Firecracker/Kata microVM | Firecracker microVM | Firecracker microVM | Container (shared kernel) |
| **Bare-metal fidelity** | ✅ Highest | ✅ High | ✅ High | ❌ Container behavior gaps |
| **Custom images** | ✅ Any ECR image | ✅ | ✅ | ❌ Restricted |
| **Arbitrary ports** | ✅ Wildcard subdomain + NLB | ✅ | ✅ | ❌ |
| **24×7 persistent** | ✅ | ✅ | ✅ | ❌ TTL enforced |
| **Snapshot suspend/resume** | ✅ 1.2s measured | ✅ | ✅ | ❌ |
| **Credential isolation** | ✅ LiteLLM IRSA (verified) | ✅ | ✅ | N/A |
| **Data sovereignty** | ✅ Stays in your AWS account | ❌ 3rd party | ❌ 3rd party | ✅ |
| **K8s ecosystem** | ✅ Native | ❌ | ❌ | ❌ |
| **Est. cost (1000 concurrent)** | **~$170-$450/mo** | ~$800+/mo | ~$600+/mo | Per-call |

> Cost based on c6g.metal with Savings Plan (~-50%) + snapshot idle recovery. Actual depends on active ratio.

### Architecture

```
┌─ EKS cluster ────────────────────────────────────────────────────────┐
│                                                                        │
│  System nodes (Fargate/general)    c6g.metal node group (sandboxes)  │
│  ┌────────────────────────────┐    ┌──────────────────────────────┐  │
│  │ sandbox-control-plane      │    │  node-agent DaemonSet        │  │
│  │ (Deployment, IRSA)         │───►│  Firecracker REST            │  │
│  │  KataDriver (k8s client)   │    │  jailer / tap / snapshots    │  │
│  │  FirecrackerDriver         │    │  S3 upload/download          │  │
│  │  WarmPool                  │    └──────────────────────────────┘  │
│  │  Stateless → DynamoDB      │                                       │
│  └────────────────────────────┘                                       │
│        ↑ ingress-nginx (NLB)                                          │
│        api.sbx.<domain>  ←── production external access              │
│                                                                        │
│  DynamoDB   LiteLLM (Bedrock proxy)   Karpenter (metal autoscaling)  │
└───────────────────────────────────────────────────────────────────────┘
```

### Quick Start (Agent Deployment Guide)

> Copy the following to Claude Code, Cursor, or any code-capable Agent to deploy the platform end-to-end.

```
You are a DevOps engineer deploying an AI Agent sandbox platform on AWS.
Follow these steps exactly, debugging any errors before proceeding.

[Prerequisites]
- AWS CLI configured (IAM permissions: EKS, EC2, IAM, DynamoDB, ECR, S3)
- kubectl, terraform(>=1.5), helm, git installed
- EC2 vCPU service quota for c6g.metal (64 vCPU) — request increase if needed

[Step 0: Clone the repository]
git clone https://github.com/teaguexiao/aws-self-hosted-sandbox.git
cd aws-self-hosted-sandbox
export AWS_REGION=us-east-1

[Step 1: Create DynamoDB state tables]
cd terraform/stage1-dynamodb
terraform init && terraform apply -auto-approve
aws dynamodb list-tables --region us-east-1 | grep claude-sbx

[Step 2: Create EKS cluster + .metal node group]
cd ../phase3
MY_IP=$(curl -s https://checkip.amazonaws.com)
terraform init && terraform apply -auto-approve \
  -var="endpoint_public_access_cidrs=[\"${MY_IP}/32\"]"
# EKS control plane ~10-12 min; with .metal node group cold start, ~15 min total
aws eks update-kubeconfig --name claude-sbx --region us-east-1
kubectl wait node --all --for=condition=Ready --timeout=900s

[Step 3: Create the kata-qemu RuntimeClass (do NOT use the kata-deploy DaemonSet!)]
#
# ⚠️⚠️ Key architecture decision: this project does NOT use the official kata-deploy
#       DaemonSet to install Kata.
#
# Why (a measured, blocking issue): kata-deploy ends its install by running
#   `systemctl restart containerd` on a node that ALREADY runs kubelet + many containers.
#   On c6g.metal + AL2023 this leaves orphaned containerd-shim processes and makes the
#   whole bare-metal node HANG for ~12 minutes before it reboots (containerd restart itself
#   takes 200ms — the slowness is a node-level hang). During that window EC2 reachability
#   checks fail and the managed node group / ASG keeps replacing it as "unhealthy" → a
#   node-replacement loop.
#
# The fix: install Kata at node BOOTSTRAP time (before kubelet registers with EKS), via
#   the Karpenter EC2NodeClass.userData (see Step 7). At bootstrap there are no running
#   containers and EKS can't see the node yet, so the containerd restart is instant and
#   causes zero churn (measured: a fresh .metal node reaches Ready in 30-60s).
#
# So Step 3 only creates the cluster-level RuntimeClass used to schedule sandbox pods:

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
  # Only schedule onto the .metal nodes where Karpenter's UserData pre-installed Kata (Step 7)
  nodeSelector:
    katacontainers.io/kata-runtime: "true"
RUNTIMECLASS

kubectl get runtimeclass kata-qemu

[Step 4: Install ingress-nginx (shared NLB)]
# IMPORTANT: specify namespace to avoid conflicts with Terraform later
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx --create-namespace \
  --set controller.service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-type"=nlb \
  --set controller.ingressClassResource.default=true
# Wait for NLB external address (~1-3 min)
kubectl get svc -n ingress-nginx ingress-nginx-controller --watch

[Step 5: Build and push arm64 images]
# Note: the sandbox image repo claude-sbx is auto-created by phase3 (Step 2); only create these two:
ACCT=$(aws sts get-caller-identity --query Account --output text)
aws ecr create-repository --repository-name sandbox-control-plane --region us-east-1 2>/dev/null || true
aws ecr create-repository --repository-name node-agent --region us-east-1 2>/dev/null || true
# Run on arm64 machine (M-series Mac, Graviton EC2, or the .metal node itself)
# See build_and_push.sh for SSM-based remote build on .metal node
bash scripts/build_and_push.sh

[Step 6: Deploy control plane + LiteLLM + Karpenter IAM]
# sandbox_domain is the subdomain root; control plane will be at api.<sandbox_domain>
# Example: sandbox_domain=sbx.example.com → api.sbx.example.com
#
# IMPORTANT: add create_ingress_nginx=false since Step 4 already installed it
cd terraform/stage2-control-plane && terraform init
ACCT=$(aws sts get-caller-identity --query Account --output text)
S3_BUCKET="my-sandbox-snapshots-${ACCT}"
aws s3 mb s3://${S3_BUCKET} --region us-east-1 2>/dev/null || true
terraform apply -auto-approve \
  -var="sandbox_image=public.ecr.aws/amazonlinux/amazonlinux:2023" \
  -var="control_plane_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/sandbox-control-plane:latest" \
  -var="node_agent_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/node-agent:latest" \
  -var="snapshot_s3_bucket=${S3_BUCKET}" \
  -var="enable_fargate=false" \
  -var="create_ingress_nginx=false" \
  -var="sandbox_domain=sbx.example.com"
# Terraform creates: IRSA roles, Karpenter worker node IAM role, K8s resources,
# and EKS Access Entry for karpenter_node role (EC2_LINUX type) —
# this is what allows Karpenter-provisioned nodes to join the cluster via TLS bootstrap
# Karpenter controller itself is installed manually in Step 7 (OCI Helm auth issues)

[Step 7: Install Karpenter manually]
# Remove Docker credential store first (needed for OCI registry access)
python3 -c "
import json, pathlib
cfg = pathlib.Path.home() / '.docker/config.json'
if cfg.exists():
    d = json.loads(cfg.read_text()); d.pop('credsStore', None)
    cfg.write_text(json.dumps(d)); print('credsStore removed')
"
ACCT=$(aws sts get-caller-identity --query Account --output text)
CLUSTER_ENDPOINT=$(aws eks describe-cluster --name claude-sbx --query 'cluster.endpoint' --output text)
KARPENTER_ROLE_ARN="arn:aws:iam::${ACCT}:role/claude-sbx-karpenter"

helm upgrade --install karpenter \
  oci://public.ecr.aws/karpenter/karpenter --version 1.3.3 \
  --namespace karpenter --create-namespace \
  --set "settings.clusterName=claude-sbx" \
  --set "settings.clusterEndpoint=${CLUSTER_ENDPOINT}" \
  --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=${KARPENTER_ROLE_ARN}" \
  --set "controller.resources.limits.memory=1Gi"
kubectl scale deployment karpenter -n karpenter --replicas=1
kubectl rollout status deployment/karpenter -n karpenter --timeout=120s

# Get node role name (fixed naming pattern, or query AWS)
KARPENTER_NODE_ROLE="claude-sbx-karpenter-node"
# Alternative: KARPENTER_NODE_ROLE=$(aws iam list-roles --query 'Roles[?contains(RoleName,`karpenter-node`)].RoleName' --output text)
#
# Use a QUOTED heredoc (<<'NODEPOOL') into a file so the local shell does not mangle the
# $VARs / backticks inside userData; substitute the role via sed afterwards.
# Kata is pre-installed at bootstrap by EC2NodeClass.userData (see Step 3 rationale).
# Measured: a fresh c6g.metal node from this NodePool reaches Ready in 30-60s, zero churn.
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
      ebs: {volumeSize: 200Gi, volumeType: gp3}
  # ── Plan A: pre-install Kata at bootstrap (before kubelet registers) ──
  userData: |
    #!/bin/bash
    set -euxo pipefail
    KATA_VERSION="3.31.0"; ARCH="arm64"
    cd /tmp
    # NOTE: the release artifact is .tar.zst (NOT .tar.xz); AL2023 ships zstd
    curl -fsSL "https://github.com/kata-containers/kata-containers/releases/download/${KATA_VERSION}/kata-static-${KATA_VERSION}-${ARCH}.tar.zst" -o kata.tar.zst
    tar --use-compress-program=unzstd -xf kata.tar.zst -C /   # paths inside: ./opt/kata/...
    # containerd 2.x (AL2023) uses the v2 path io.containerd.cri.v1.runtime; register kata-qemu only
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
        # Required: the kata-qemu RuntimeClass (Step 3) carries nodeSelector
        # katacontainers.io/kata-runtime=true. With Plan A, Kata is pre-installed via UserData
        # (not by kata-deploy), so this label must be declared here or Karpenter will refuse
        # to provision (NodePool "incompatible" with the RuntimeClass nodeSelector).
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

[Step 8: Configure DNS for production API access]
NLB_HOST=$(kubectl get svc -n ingress-nginx ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
echo "Add DNS record: api.sbx.example.com CNAME $NLB_HOST"
# Or skip DNS and use --resolve flag for testing (see Step 9)

[Step 9: Run end-to-end tests]
# Wait for image pull to complete (ECR first pull ~1-3 min)
kubectl rollout status deployment/sandbox-control-plane -n sandbox-system --timeout=300s
kubectl rollout status deployment/litellm -n litellm --timeout=300s

# Note: stage2 now defaults LiteLLM to 4Gi + 1 replica (2Gi OOMKills; see litellm.tf).
#   If it still OOMs: kubectl set resources deployment/litellm -n litellm --limits=cpu=2,memory=4Gi

kubectl get pods -n sandbox-system   # control-plane 2/2 + node-agent (DaemonSet on kata-metal nodes)
kubectl get pods -n litellm           # litellm 1/1

# ── Recommended: local port-forward mode (no DNS/Ingress; measured ALL TESTS PASSED) ──
bash scripts/e2e_test.sh
# Expected: script ends with ALL TESTS PASSED (some tests skip depending on driver)

# ── Production Ingress (⚠️ does NOT work out of the box) ──
# ingress-nginx here uses the in-tree NLB (target=instance + preserve_client_ip). Combined
# with Karpenter kata-metal nodes (tainted, cross-AZ, no ingress pod) joining the NLB target
# group and cross-zone disabled by default, external HTTP returns empty replies (in-cluster
# ClusterIP access works fine). For real external Ingress, install the AWS Load Balancer
# Controller (target type=ip, pointing straight at pods), or restrict the NLB to system nodes.
# (--resolve command kept for reference):
# NLB_IP=$(dig +short $NLB_HOST | head -1)
# bash scripts/e2e_test.sh --api-url "http://api.sbx.example.com" --resolve "api.sbx.example.com:80:${NLB_IP}"

[Step 10: Use the API]
BASE_URL="http://api.sbx.example.com"   # or http://localhost:18000

# Create sandbox (idempotent)
curl -s $BASE_URL/sandboxes -X POST \
  -H "Content-Type: application/json" \
  -d '{"cpu":2,"mem_mib":4096,"tenant_id":"user-1","idempotency_key":"req-001"}'

# Wait for ready
curl "$BASE_URL/sandboxes/{id}/wait?state=running&timeout=30"

# Execute command
curl -s $BASE_URL/sandboxes/{id}/exec -X POST -d '{"cmd":"echo hello"}'

# Suspend (snapshot + free memory)
curl -s -X POST $BASE_URL/sandboxes/{id}/suspend

# Resume (~1.2s)
curl -s -X POST $BASE_URL/sandboxes/{id}/resume

# Destroy
curl -s -X DELETE $BASE_URL/sandboxes/{id}

[Cleanup]
ACCT=$(aws sts get-caller-identity --query Account --output text)
S3_BUCKET="my-sandbox-snapshots-${ACCT}"
cd terraform/stage2-control-plane && terraform destroy -auto-approve \
  -var="sandbox_image=public.ecr.aws/amazonlinux/amazonlinux:2023" \
  -var="control_plane_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/sandbox-control-plane:latest" \
  -var="node_agent_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/node-agent:latest" \
  -var="snapshot_s3_bucket=${S3_BUCKET}" \
  -var="enable_fargate=false" \
  -var="create_ingress_nginx=false"

# Delete the Karpenter NodePool first (so it reclaims all kata-metal nodes), then uninstall
# helm releases + delete the NLB created by ingress-nginx — otherwise leftover nodes/NLB
# hold public addresses on the subnets and phase3 destroy stalls with DependencyViolation:
kubectl delete nodepool kata-metal 2>/dev/null || true     # triggers Karpenter to reclaim .metal nodes
kubectl delete ec2nodeclass kata-metal 2>/dev/null || true
sleep 60  # wait for Karpenter to terminate the .metal instances
helm uninstall karpenter     -n karpenter     2>/dev/null || true
helm uninstall ingress-nginx -n ingress-nginx 2>/dev/null || true
for arn in $(aws elbv2 describe-load-balancers --region us-east-1 \
    --query 'LoadBalancers[?Type==`network`].LoadBalancerArn' --output text); do
  aws elbv2 delete-load-balancer --region us-east-1 --load-balancer-arn "$arn"
done
sleep 30  # wait for NLB ENIs to release

# Delete orphaned pod ENIs left by Karpenter nodes (VPC CNI creates them; they are NOT
# cleaned up when the node terminates and will stall the phase3 destroy on subnet/SG deletion):
VPC_ID=$(aws ec2 describe-vpcs --region us-east-1 \
  --filters "Name=tag:Name,Values=claude-sbx-vpc" --query 'Vpcs[0].VpcId' --output text)
if [ "$VPC_ID" != "None" ] && [ -n "$VPC_ID" ]; then
  for eni in $(aws ec2 describe-network-interfaces --region us-east-1 \
      --filters "Name=vpc-id,Values=$VPC_ID" "Name=status,Values=available" \
      --query 'NetworkInterfaces[].NetworkInterfaceId' --output text); do
    aws ec2 delete-network-interface --region us-east-1 --network-interface-id "$eni" 2>/dev/null || true
  done
fi

MY_IP=$(curl -s https://checkip.amazonaws.com)
cd ../phase3 && terraform destroy -auto-approve \
  -var="endpoint_public_access_cidrs=[\"${MY_IP}/32\"]"
# If VPC deletion stalls (>5min), the EKS-managed eks-cluster-sg is usually the culprit; delete it:
#   SG=$(aws ec2 describe-security-groups --region us-east-1 \
#     --filters "Name=group-name,Values=eks-cluster-sg-claude-sbx-*" --query 'SecurityGroups[0].GroupId' --output text)
#   [ "$SG" != "None" ] && aws ec2 delete-security-group --region us-east-1 --group-id "$SG"
cd ../stage1-dynamodb && terraform destroy -auto-approve

# Clean up leftovers that destroy won't remove but that block a future re-create:
aws logs delete-log-group --log-group-name /aws/eks/claude-sbx/cluster --region us-east-1 2>/dev/null || true
aws ecr delete-repository --repository-name claude-sbx --force --region us-east-1 2>/dev/null || true
# S3 snapshot bucket (optional): aws s3 rb s3://my-sandbox-snapshots-$(aws sts get-caller-identity --query Account --output text) --force --region us-east-1 2>/dev/null || true
```

### Key Benchmark Numbers

| Metric | Measured | Environment |
|---|---|---|
| microVM cold start | ~0.31s | c6g.metal, Firecracker v1.16 |
| Snapshot resume | **1.2s (cross-host) / 7ms (same host)** | Full snapshot, 4GB memory |
| Idle memory footprint | ~50 MB/VM | 512 MiB allocated |
| Max concurrent VMs (tested) | 60 (not the ceiling) | c6g.metal 128 GiB |
| npm install time | 18s (JuiceFS) / 4s (local ext4) | 7160 files, 8 deps |
| LiteLLM → Bedrock latency | ~1-2s | claude-haiku-4-5 |
| e2e test pass rate | **17/17 (ALL PASS)** | Cluster-deployed, Kata driver |

### Local Smoke Test (No AWS Required)

```bash
pip install "moto[dynamodb]" boto3 kubernetes
python3 sandbox-api/smoke_test.py
# Expected: 21/21 PASS
```

---

*This project is a production-grade reference implementation. Use it as a foundation for building your own agent sandbox platform on AWS.*
