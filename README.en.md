# AWS Self-Hosted AI Agent Sandbox Platform

> Build your own Fly.io-style Firecracker microVM sandbox on AWS — lower cost, full control, data stays in your account.

[中文 / Chinese](README.md) · **English**

---

### Overview

A production-grade AI Agent sandbox platform built on AWS, replicating Fly.io's Firecracker microVM architecture — with lower cost, full data sovereignty, and native Kubernetes integration.

- **True microVM isolation**: Each sandbox runs in an independent Firecracker guest kernel — identical behavior to bare metal
- **Bare Firecracker backend**: node-agent directly manages microVMs (jailer/tap/snapshot), cost-first; snapshots land on persistent state EBS (**not S3**), cross-node recovery relies on the EBS volume surviving + detach/attach (see "Snapshot persistence & cross-node recovery" below)
- **Snapshot-driven cost control**: Idle sandboxes snapshot to persistent EBS, resume in ~1.2s (same-node)
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

### Portal (Demo Dashboard)

A lightweight E2B / Fly.io-style console ([`portal/`](portal/)) for demoing and observing the platform:
a global overview of all sandboxes, node capacity, warm-pool level and an event timeline, plus an API
Playground to run create / suspend / resume / exec / destroy and see each call's response and latency live.
**Runs locally** (`npm run dev` + `kubectl port-forward`); see [portal/README.md](portal/README.md).

| Dashboard Overview | Sandbox Detail + Metrics |
|---|---|
| ![Portal Dashboard](docs/portal/portal-dashboard.png) | ![Sandbox Detail](docs/portal/portal-detail.png) |

> Screenshots from a live deployment (EKS + c6g.metal): summary cards, sandbox table with status badges,
> node capacity, event timeline; the detail page shows the full record plus snapshot/resume performance
> metrics (e.g. a diff snapshot writing only 5.35 MB, resume in 408 ms).

### Comparison with Alternatives

| Feature | This (AWS Self-Hosted) | E2B | Fly.io Machines | AWS AgentCore |
|---|---|---|---|---|
| **Isolation** | Firecracker microVM | Firecracker microVM | Firecracker microVM | Container (shared kernel) |
| **Bare-metal fidelity** | ✅ Highest | ✅ High | ✅ High | ❌ Container behavior gaps |
| **Custom images** | ✅ Any ECR image | ✅ | ✅ | ❌ Restricted |
| **Arbitrary ports** | ✅ Wildcard subdomain + NLB | ✅ | ✅ | ❌ |
| **24×7 persistent** | ✅ | ✅ | ✅ | ❌ TTL enforced |
| **Snapshot suspend/resume** | ✅ 1.2s measured | ✅ | ✅ | ❌ |
| **Credential isolation** | ✅ LiteLLM IRSA (verified) | ✅ | ✅ | N/A |
| **Data sovereignty** | ✅ Stays in your AWS account | ❌ 3rd party | ❌ 3rd party | ✅ |
| **K8s ecosystem** | ✅ Native | ❌ | ❌ | ❌ |
| **Min. monthly cost (1 machine)** | **~$632/mo** (spot + snapshot recovery) | Managed pricing | Managed pricing | Per-call |

### Architecture

```
┌─ EKS cluster ───────────────────────────────────────────────────────────┐
│                                                                           │
│  Managed node group (system)      c6g.metal nodes (sandboxes)           │
│  ┌────────────────────────────┐   ┌──────────────────────────────────┐  │
│  │ sandbox-control-plane      │   │  Firecracker microVM             │  │
│  │ (Deployment, IRSA)         │──►│  node-agent DaemonSet            │  │
│  │  FirecrackerDriver         │   │  jailer / tap / snapshot / S3    │  │
│  │  WarmPool                  │   └──────────────────────────────────┘  │
│  │  Stateless → DynamoDB      │                                          │
│  └────────────────────────────┘                                          │
│        ↑ ingress-nginx (NLB)                                             │
│        api.sbx.<domain>  ←── production (POC: use port-forward)         │
│                                                                           │
│  DynamoDB   LiteLLM (Bedrock proxy)                                       │
└──────────────────────────────────────────────────────────────────────────┘
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
# EKS control plane ~10-12 min; total with .metal node group cold start ~15 min
aws eks update-kubeconfig --name claude-sbx --region us-east-1
kubectl wait node --all --for=condition=Ready --timeout=900s

# (No RuntimeClass / ingress-nginx / Karpenter needed — bare Firecracker uses the phase3
#  managed .metal node group directly, and POC accesses the control plane via port-forward.)

[Step 5: Build and push arm64 images]
# Note: the sandbox image repo claude-sbx is auto-created by phase3 (Step 2); only create these two:
ACCT=$(aws sts get-caller-identity --query Account --output text)
aws ecr create-repository --repository-name sandbox-control-plane --region us-east-1 2>/dev/null || true
aws ecr create-repository --repository-name node-agent --region us-east-1 2>/dev/null || true
# Run on arm64 machine (M-series Mac, Graviton EC2, or the .metal node itself)
# See build_and_push.sh for SSM-based remote build on .metal node
bash scripts/build_and_push.sh

[Step 6: Deploy control plane + node-agent + LiteLLM]
# FC_NODES: internal IPs of the STABLE metal nodes (control plane reaches node-agent over HTTP)
# sandbox_domain is the subdomain root; control plane will be at api.<sandbox_domain>
cd terraform/stage2-control-plane && terraform init
ACCT=$(aws sts get-caller-identity --query Account --output text)
S3_BUCKET="my-sandbox-snapshots-${ACCT}"
aws s3 mb s3://${S3_BUCKET} --region us-east-1 2>/dev/null || true
FC_NODES=$(kubectl get nodes -o jsonpath='{range .items[*]}{.status.addresses[?(@.type=="InternalIP")].address}{","}{end}' | sed 's/,$//')
API_KEY=$(openssl rand -hex 32); LITELLM_KEY=$(openssl rand -hex 32)
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
# Terraform creates: IRSA roles, K8s resources (sandbox-system namespace +
# control-plane Deployment + node-agent DaemonSet), api-keys Secret + ConfigMap.

[Step 8: Configure DNS for production API access]
NLB_HOST=$(kubectl get svc -n ingress-nginx ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
echo "Add DNS record: api.sbx.example.com CNAME $NLB_HOST"
# Or skip DNS and use --resolve flag for testing (see Step 9)

[Step 9: Run end-to-end tests]
# Wait for image pull to complete (ECR first pull ~1-3 min)
kubectl rollout status deployment/sandbox-control-plane -n sandbox-system --timeout=300s
kubectl rollout status deployment/litellm -n litellm --timeout=300s

# Tip: LiteLLM defaults to 4Gi memory + 1 replica (configured in litellm.tf to prevent OOMKill).
# If it still OOMKills: kubectl set resources deployment/litellm -n litellm --limits=cpu=2,memory=4Gi
# Tip: single-node cluster — if the 2nd LiteLLM replica stays Pending (anti-affinity):
#   kubectl scale deployment/litellm -n litellm --replicas=1
# Tip: if terraform reports "Unexpected Identity Change" on a deployment resource:
#   terraform state rm kubernetes_deployment.litellm kubernetes_deployment.control_plane
#   then re-run terraform apply

kubectl get pods -n sandbox-system   # control-plane 2/2 + node-agent (DaemonSet, one per .metal node)
kubectl get pods -n litellm           # litellm 1/1

# ── Recommended: local port-forward mode (no DNS/Ingress; measured ALL TESTS PASSED) ──
bash scripts/e2e_test.sh
# Expected: script ends with ALL TESTS PASSED (some tests skip depending on driver)

# ── Production Ingress (optional) ──
# POC accesses the control plane via kubectl port-forward (no ingress-nginx installed).
# For real external access, install ingress-nginx / AWS Load Balancer Controller and
# expose the control-plane Service; then use --resolve for local testing against the NLB:
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
  -var="fc_nodes=placeholder" \
  -var="sandbox_image=public.ecr.aws/amazonlinux/amazonlinux:2023" \
  -var="control_plane_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/sandbox-control-plane:latest" \
  -var="node_agent_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/node-agent:latest" \
  -var="snapshot_s3_bucket=${S3_BUCKET}" \
  -var="enable_fargate=false" \
  -var="create_ingress_nginx=false" \
  -var="api_keys=placeholder" \
  -var="litellm_master_key=placeholder"

# Delete orphaned pod ENIs left by terminated nodes (VPC CNI creates them; they are NOT
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

### Operations Prompt

```
You are the ops engineer for this AWS sandbox platform. Platform overview:
- EKS cluster claude-sbx, c6g.metal nodes, bare Firecracker microVM + node-agent DaemonSet
- Control plane: sandbox-system namespace, Deployment 2 replicas
  External access: http://api.sbx.<domain> (ingress-nginx NLB; POC use port-forward)
- State storage: DynamoDB (claude-sbx-sandboxes / events / tap-idx / nodes / locks)
- Credential isolation: LiteLLM (litellm namespace) holds Bedrock IRSA; sandboxes have no credentials
- Snapshots: persistent state EBS (base + Diff incremental memory snapshots), spot evacuation + cross-node recovery

Common ops tasks:
1. List sandboxes:    curl http://api.sbx.<domain>/sandboxes?tenant_id=<id>
   Local:            kubectl port-forward -n sandbox-system svc/sandbox-control-plane 18000:80 &
2. Restart control plane: kubectl rollout restart deployment/sandbox-control-plane -n sandbox-system
3. View nodes:            kubectl get nodes -o wide
4. View LiteLLM logs:     kubectl logs -n litellm deployment/litellm --tail=50
5. DynamoDB item count:   aws dynamodb scan --table-name claude-sbx-sandboxes --select COUNT
6. Update images:         bash scripts/build_and_push.sh
                          kubectl rollout restart deployment/sandbox-control-plane -n sandbox-system
7. Scale node capacity:   adjust the phase3 metal node group desired/min/max and terraform apply
8. Cost optimization — bulk-suspend idle sandboxes:
   for id in $(curl -s http://api.sbx.<domain>/sandboxes?tenant_id=all | python3 -c "import sys,json; [print(s['id']) for s in json.load(sys.stdin)['sandboxes'] if s['state']=='running']"); do
     curl -s -X POST http://api.sbx.<domain>/sandboxes/$id/suspend
   done

Monitoring:
- node-agent memory: kubectl exec -n sandbox-system daemonset/node-agent -- python3 -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8002/health').read().decode())"
- DynamoDB write latency: AWS Console → DynamoDB → Metrics → SuccessfulRequestLatency
- Node utilization: kubectl top nodes
- LiteLLM request volume: kubectl logs -n litellm deployment/litellm | grep "INFO:"
```

---

### Cost Breakdown (Minimum Setup — 1 × c6g.metal, us-east-1)

| Resource | Unit Price | Monthly (730h) |
|---|---|---|
| c6g.metal (64 vCPU / 128 GiB) **spot** (platform target mode) | ~$0.67/hr (us-east-1a live, ~29% of on-demand) | **~$486** |
| c6g.metal on-demand (baseline for comparison) | $2.304/hr | ~$1,682 |
| EKS control plane | $0.10/hr | ~$73 |
| DynamoDB (PAY_PER_REQUEST) | per write | <$1 |
| Persistent state EBS (gp3 400GB / 4000 IOPS / 1000MB/s, one per node, holds memory snapshots) | $32 storage + $5 IOPS + $35 throughput | ~$72/node |
| **Total (on-demand)** | | **~$1,828/mo** |
| **Total (on-demand + 1-yr Savings Plan ~42% off, compute only)** | | **~$1,122/mo** |
| **Total (spot + snapshot recovery, platform target mode)** | | **~$632/mo** |

> **Spot is the platform's core cost model**: c6g.metal spot ≈ 29% of on-demand (measured us-east-1 AZs $0.65–$0.74/hr, queried 2026-07);
> when spot is reclaimed, snapshot evacuation + cross-node recovery preserves memory state (see the 50-sandbox test below). **Spot prices fluctuate** — use live quotes.
> Without spot, on-demand can use a 1-yr Savings Plan for ~42% off. Use [AWS Pricing Calculator](https://calculator.aws) for exact figures.

**Per-sandbox amortized cost (single c6g.metal, 128 GiB):**

| Mode | Memory per sandbox | Sandboxes | Amortized cost |
|---|---|---|---|
| 24×7 active workload | 1.5 GiB | ~75 | **~$23/sandbox·mo** |
| **Snapshot idle recovery** | ~50 MB (idle footprint) | **400+** | **~$4/sandbox·mo** |
| Savings Plan + snapshot recovery | — | same | **~$2–3/sandbox·mo** |

> **vCPU / Memory Overcommit further reduces per-sandbox cost:** Firecracker microVMs support CPU oversubscription — idle sandboxes consume nearly zero CPU, and active sandboxes are burst-oriented. Measured idle footprint is only ~50 MB per VM (far below the allocated 1.5 GiB), which means you can provision more sandboxes than raw memory math suggests and fill the machine based on actual working-set, not allocation. Combined with snapshot-based idle recovery, the effective sandbox density — and thus per-sandbox cost — can be significantly lower than the table above. The right overcommit ratio depends on your workload profile and should be validated through load testing.

### Key Benchmark Numbers

| Metric | Measured | Environment |
|---|---|---|
| microVM cold start | ~0.31s | c6g.metal, Firecracker v1.16 |
| Snapshot resume | **~0.13s (same-host Full load)** | warm pool resumes on origin node; cross-host via persistent EBS migration (see 50-sandbox test below) |
| Snapshot storage | persistent state EBS (base + Diff, not S3) | spot volume survives reclaim, migrates to another node |
| Idle memory footprint | ~50 MB/VM | 512 MiB allocated |
| Max concurrent VMs (tested) | 60 (not the ceiling) | c6g.metal 128 GiB |
| npm install time | 18s (JuiceFS) / 4s (local ext4) | 7160 files, 8 deps |
| LiteLLM → Bedrock latency | ~1-2s | claude-haiku-4-5 |
| Smoke tests | **26/26 PASS** | moto mock, `sandbox-api/smoke_test.py` |

#### Snapshot persistence & cross-node recovery (current state — please read)

To avoid misunderstanding vs. the implementation, the current boundaries:

- **Snapshots only land on the node's local persistent state EBS (`/var/lib/sbx/{id}/snap`), never S3.**
  Neither suspend nor spot evacuation uploads to S3; the `snapshot_s3` field is always empty. Cross-node
  recovery relies on that `DeleteOnTermination=false` state volume surviving and being attached to a new
  node — **not on downloading snapshots from S3**.
- **The S3 fallback path in code is currently dormant**: resume/`op_resume` still keeps a "pull from
  `s3_prefix` if no local snapshot" branch, but since nothing ever writes snapshots to S3 (`upload_s3` is
  never set true, `snapshot_s3` is always empty), that branch is never triggered. It's a reserved hook for a
  future optional S3 archive — **it does not mean an S3 copy exists today**.
- **Cross-node recovery is not yet fully automated**: node-agent's spot-reclaim auto-evacuation defaults to
  **DRY-RUN** (records a plan only, takes no snapshot); set `RECLAIM_AUTO_EVACUATE=1` to actually snapshot to
  EBS. The "on node death, auto-detach volume → attach to new node → batch resume" step (Block 2 cross-node
  orchestration) is **not implemented yet**. In the 50-sandbox test, the volume detach/attach and batch
  resume were **triggered manually / semi-automatically** to validate the capability — not an automatic
  production flow.

> In one line: **same-node suspend/resume is fully automatic and never touches S3; the primitives for
> cross-node recovery (EBS volume survival + exact memory resume) are proven, but the "auto-detect spot
> reclaim → auto-migrate volume → auto-resume" orchestration loop is not finished.**

### Local Smoke Test (No AWS Required)

```bash
pip install "moto[dynamodb]" boto3 kubernetes
python3 sandbox-api/smoke_test.py
# Expected: 29/29 PASS
```

---

### Contributing (Git Hooks, team-shared)

After cloning, **run once** to enable the pre-commit AI code review + doc auto-sync:

```bash
./scripts/install-hooks.sh    # sets git config core.hooksPath .githooks
```

- Hook sources live in `.githooks/` (version-controlled), so they **update automatically on `git pull` — no reinstall needed**.
- Git won't change local config automatically for security reasons, so each member sets `core.hooksPath` once (persists afterwards).
- Skip temporarily: `SKIP_CODE_REVIEW=1` / `SKIP_DOC_UPDATE=1 git commit`; skip all: `git commit --no-verify`.
- Details: [.githooks/README.md](.githooks/README.md).

---

*This project is a production-grade reference implementation. Use it as a foundation for building your own agent sandbox platform on AWS.*
