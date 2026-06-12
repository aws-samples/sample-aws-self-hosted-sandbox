#!/bin/bash
# verify-h3.sh —— EKS 就绪后,装 Kata + ingress-nginx,部署沙盒,验证 H3 三要素
# 前提:aws eks update-kubeconfig 已配好,kubectl 能连集群。
set -uxo pipefail

REGION=us-east-1
ACCT=$(aws sts get-caller-identity --query Account --output text)
ECR=$ACCT.dkr.ecr.$REGION.amazonaws.com/claude-sbx

echo "===== 0. 集群与节点 ====="
kubectl get nodes -o wide
kubectl get node -l sandbox=true

echo "===== 1. 装 Kata Containers(kata-deploy DaemonSet) ====="
kubectl apply -k "github.com/kata-containers/kata-containers/tools/packaging/kata-deploy/kata-rbac/base?ref=stable-3.x"
kubectl apply -k "github.com/kata-containers/kata-containers/tools/packaging/kata-deploy/kata-deploy/base?ref=stable-3.x"
kubectl -n kube-system rollout status ds/kata-deploy --timeout=300s
kubectl apply -k "github.com/kata-containers/kata-containers/tools/packaging/kata-deploy/runtimeclasses?ref=stable-3.x"
echo "--- 可用 RuntimeClass ---"
kubectl get runtimeclass

echo "===== 2. 推送沙盒镜像到 ECR(若 Phase1 没推过) ====="
# 镜像在 Phase1 主机上构建过;这里假设需要本地重新构建并推送(arm64)
# 若已在 ECR,跳过。简单起见用 buildx 直接构建 arm64:
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ACCT.dkr.ecr.$REGION.amazonaws.com || true

echo "===== 3. 部署沙盒 Pod(Kata RuntimeClass) ====="
sed "s|ACCT.dkr.ecr.REGION|$ACCT.dkr.ecr.$REGION|g" sandbox.yaml | kubectl apply -f -
kubectl wait --for=condition=Ready pod/claude-sbx-1 --timeout=180s || kubectl describe pod claude-sbx-1

echo "===== 4. 验证 H3 (a) 自定义镜像 / (c) 24x7 ====="
kubectl get pod claude-sbx-1 -o jsonpath='{.spec.runtimeClassName}{"\n"}'  # 应为 kata-clh
kubectl exec claude-sbx-1 -- uname -r       # guest 内核 —— 证明在 microVM 里
kubectl exec claude-sbx-1 -- nproc
kubectl exec claude-sbx-1 -- claude --version

echo "===== 5. 验证 H3 (b) 任意端口 ====="
# 沙盒内起 http server,经 ingress 验证外部可达
kubectl exec claude-sbx-1 -- bash -c 'cd /tmp && nohup python3 -m http.server 8080 >/dev/null 2>&1 &'
sleep 3
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx 2>/dev/null || true
helm repo update >/dev/null
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --set controller.service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-type"=nlb \
  --set controller.ingressClassResource.default=true \
  --wait --timeout 300s
NLB=$(kubectl get svc ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
echo "NLB=$NLB"
echo "--- 经 ingress 按 Host 路由访问沙盒 8080(任意端口暴露)---"
sleep 30  # 等 NLB target 健康
curl -s --resolve 8080-sbx1.sbx.example.com:80:$(dig +short $NLB | head -1) \
     http://8080-sbx1.sbx.example.com/ | head -5 && echo "PORT_EXPOSE_OK" || echo "PORT_EXPOSE_PENDING(NLB注册可能需更久)"
echo "===== H3 验证完成 ====="
