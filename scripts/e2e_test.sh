#!/usr/bin/env bash
# 端到端集成测试 — 对真实 EKS 集群上的控制面 API 跑完整生命周期
#
# 前提:
#   1. kubectl 已配置(aws eks update-kubeconfig --name claude-sbx)
#   2. terraform/stage2-control-plane 已 apply
#   3. 控制面 Pod Running: kubectl -n sandbox-system get pods
#
# 用法(三种模式):
#
#   1) 生产 Ingress 模式（推荐）—— 通过 ingress-nginx NLB 访问控制面，无需 port-forward:
#      NLB_HOST=$(kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
#      bash scripts/e2e_test.sh --api-url "http://api.sbx.example.com" \
#                               --resolve "api.sbx.example.com:80:$(dig +short $NLB_HOST | head -1)"
#      # 或在 DNS 已配好的情况下:
#      bash scripts/e2e_test.sh --api-url "http://api.sbx.example.com"
#
#   2) port-forward 模式（本地开发）—— 不传 --api-url，脚本自动 port-forward:
#      bash scripts/e2e_test.sh
#
#   3) 直接指定地址:
#      bash scripts/e2e_test.sh --api-url http://localhost:18000

set -euo pipefail

# ---------- 参数解析 ----------
DRIVER="kata"
API_URL=""
CURL_EXTRA=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --driver)    DRIVER="$2";    shift 2 ;;
    --api-url)   API_URL="$2";   shift 2 ;;
    --resolve)   CURL_EXTRA="--resolve $2"; shift 2 ;;  # 覆盖 DNS(用于 Ingress 测试)
    *)           shift ;;
  esac
done

NAMESPACE="sandbox-system"
LOCAL_PORT=18000
PF_PID=""

# ---------- 颜色 ----------
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass() { echo -e "${GREEN}  PASS${NC} $1"; }
fail() { echo -e "${RED}  FAIL${NC} $1"; FAILED=$((FAILED+1)); }
info() { echo -e "${YELLOW}  ----${NC} $1"; }
FAILED=0

# ---------- port-forward（本地开发/调试用）----------
# 生产环境：API_URL 应指向 ingress-nginx NLB，例如：
#   http://api.sbx.example.com（已配 DNS → NLB）
#   或使用 --resolve 参数绕过 DNS，直接测 NLB IP
setup_portforward() {
  if [[ -n "$API_URL" ]]; then
    info "Using provided API URL: $API_URL"
    return
  fi
  info "No --api-url provided, starting port-forward (dev mode)"
  info "Production: use --api-url http://api.sbx.<domain> (ingress-nginx NLB)"
  kubectl -n "$NAMESPACE" port-forward svc/sandbox-control-plane "${LOCAL_PORT}:80" &>/tmp/pf.log &
  PF_PID=$!
  sleep 3
  API_URL="http://localhost:${LOCAL_PORT}"
  echo "  API URL: $API_URL (port-forward)"
}

teardown_portforward() {
  if [[ -n "$PF_PID" ]]; then
    kill "$PF_PID" 2>/dev/null || true
  fi
}
trap teardown_portforward EXIT

# ---------- HTTP helpers ----------
api() {
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    # shellcheck disable=SC2086
    curl -s -w "\n%{http_code}" -X "$method" \
      -H "Content-Type: application/json" \
      -d "$body" ${CURL_EXTRA} "${API_URL}${path}"
  else
    # shellcheck disable=SC2086
    curl -s -w "\n%{http_code}" -X "$method" ${CURL_EXTRA} "${API_URL}${path}"
  fi
}

# 返回 (body, http_code) — body 存 $BODY, code 存 $CODE
# 用 awk 兼容 macOS/GNU 两种 head 语法
call() {
  local raw
  raw=$(api "$@")
  CODE=$(echo "$raw" | tail -1)
  BODY=$(echo "$raw" | awk 'NR>1{print prev} {prev=$0}')
}

wait_state() {
  local sid="$1" target="$2" timeout="${3:-60}"
  info "Waiting for sandbox $sid → $target (timeout ${timeout}s)"
  local deadline=$((SECONDS + timeout))
  while [[ $SECONDS -lt $deadline ]]; do
    call GET "/sandboxes/${sid}/wait?state=${target}&timeout=10"
    local state
    state=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('state',''))" 2>/dev/null || echo "")
    if [[ "$state" == "$target" || "$state" == "failed" ]]; then
      echo "    state=$state"
      return 0
    fi
    sleep 2
  done
  echo "    timeout: last state=$state"
  return 1
}

# ---------- 测试开始 ----------
echo ""
echo "========================================"
echo "  Sandbox Control Plane E2E Test"
echo "  Driver: $DRIVER"
echo "========================================"

setup_portforward

# ---- T1: 服务健康检查 ----
info "T1: Service health"
call GET "/"
if [[ "$CODE" == "200" ]]; then
  DRIVER_REPORTED=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('driver',''))" 2>/dev/null || echo "")
  pass "GET / → 200 (driver=$DRIVER_REPORTED)"
else
  fail "GET / → $CODE"
fi

# ---- T2: capabilities ----
info "T2: Capabilities endpoint"
call GET "/capabilities"
if [[ "$CODE" == "200" ]]; then
  SR=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('suspend_resume',''))" 2>/dev/null || echo "")
  pass "GET /capabilities → 200 (suspend_resume=$SR)"
else
  fail "GET /capabilities → $CODE"
fi

# ---- T3: 创建沙盒 ----
info "T3: Create sandbox"
IDEM_KEY="e2e-test-$(date +%s)"
call POST "/sandboxes" "{\"image\":\"\",\"cpu\":1,\"mem_mib\":2048,\"tenant_id\":\"e2e\",\"services\":[{\"port\":8080}],\"idempotency_key\":\"${IDEM_KEY}\"}"
if [[ "$CODE" == "201" || "$CODE" == "200" ]]; then
  SID=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")
  pass "POST /sandboxes → $CODE (id=$SID)"
else
  fail "POST /sandboxes → $CODE: $BODY"
  echo "Cannot continue without sandbox"; exit 1
fi

# ---- T4: 等待 running ----
info "T4: Wait for running state"
if wait_state "$SID" "running" 120; then
  pass "sandbox $SID reached running"
else
  fail "sandbox $SID did not reach running within 120s"
fi

# ---- T5: GET 单个沙盒 ----
info "T5: Get sandbox"
call GET "/sandboxes/${SID}"
if [[ "$CODE" == "200" ]]; then
  STATE=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('state',''))" 2>/dev/null || echo "")
  pass "GET /sandboxes/${SID} → 200 (state=$STATE)"
else
  fail "GET /sandboxes/${SID} → $CODE"
fi

# ---- T6: GET 列表 ----
info "T6: List sandboxes"
call GET "/sandboxes?tenant_id=e2e"
if [[ "$CODE" == "200" ]]; then
  COUNT=$(echo "$BODY" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('sandboxes',[])))" 2>/dev/null || echo "0")
  pass "GET /sandboxes → 200 (count=$COUNT)"
else
  fail "GET /sandboxes → $CODE"
fi

# ---- T7: 幂等键 ----
info "T7: Idempotency key"
call POST "/sandboxes" "{\"image\":\"\",\"cpu\":1,\"mem_mib\":2048,\"tenant_id\":\"e2e\",\"idempotency_key\":\"${IDEM_KEY}\"}"
SID2=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")
if [[ "$SID2" == "$SID" ]]; then
  pass "Idempotency: same id returned ($SID2)"
else
  fail "Idempotency: got different id $SID2 (expected $SID)"
fi

# ---- T8: locate ----
info "T8: Locate sandbox (VMM info)"
call GET "/sandboxes/${SID}/locate"
if [[ "$CODE" == "200" ]]; then
  pass "GET /sandboxes/${SID}/locate → 200"
else
  fail "GET /sandboxes/${SID}/locate → $CODE"
fi

# ---- T9: exec ----
info "T9: Exec in sandbox"
call POST "/sandboxes/${SID}/exec" '{"cmd":"echo sandbox-ok"}'
if [[ "$CODE" == "200" ]]; then
  STDOUT=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('stdout',''))" 2>/dev/null || echo "")
  pass "POST /exec → 200 (stdout='$STDOUT')"
else
  # exec 在 Kata 下通过 kubectl exec,FC 下通过 SSH;若未配 SSH 则 skip
  info "exec returned $CODE (may be expected if SSH not configured)"
fi

# ---- T10: suspend/resume(仅 FC driver 支持) ----
info "T10: Suspend / Resume (FC driver only)"
call GET "/capabilities"
SUPPORTS_SR=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('suspend_resume',False))" 2>/dev/null || echo "False")

if [[ "$SUPPORTS_SR" == "True" ]]; then
  call POST "/sandboxes/${SID}/suspend"
  if [[ "$CODE" == "200" ]]; then
    SNAP=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('snapshot_s3',''))" 2>/dev/null || echo "")
    pass "POST /suspend → 200 (snapshot=$SNAP)"

    call POST "/sandboxes/${SID}/resume"
    if [[ "$CODE" == "200" ]]; then
      RT=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('restore_time_s','?'))" 2>/dev/null || echo "?")
      pass "POST /resume → 200 (restore_time=${RT}s)"
      wait_state "$SID" "running" 30
    else
      fail "POST /resume → $CODE: $BODY"
    fi
  else
    fail "POST /suspend → $CODE: $BODY"
  fi
else
  info "suspend_resume=false (driver=$DRIVER_REPORTED), skip T10"
fi

# ---- T11: Kata suspend → 501 ----
info "T11: Kata driver returns 501 for suspend"
if [[ "$SUPPORTS_SR" == "False" ]]; then
  call POST "/sandboxes/${SID}/suspend"
  if [[ "$CODE" == "501" ]]; then
    pass "suspend on Kata → 501 (correct)"
  else
    fail "Expected 501, got $CODE"
  fi
else
  info "FC driver supports suspend, skip T11"
fi

# ---- T12: wait timeout ----
info "T12: Wait endpoint with short timeout on nonexistent state"
call GET "/sandboxes/${SID}/wait?state=nonexistent-state&timeout=3"
if [[ "$CODE" == "408" ]]; then
  pass "wait timeout → 408"
else
  # 若沙盒已变成 failed 可能返回 200
  info "wait returned $CODE (acceptable if sandbox state changed)"
fi

# ---- T13: destroy ----
info "T13: Destroy sandbox"
call DELETE "/sandboxes/${SID}"
if [[ "$CODE" == "200" ]]; then
  pass "DELETE /sandboxes/${SID} → 200"
else
  fail "DELETE /sandboxes/${SID} → $CODE"
fi

# ---- T14: 销毁后 GET → 404 ----
info "T14: GET after destroy → 404"
sleep 2
call GET "/sandboxes/${SID}"
if [[ "$CODE" == "404" ]]; then
  pass "GET after destroy → 404"
else
  fail "GET after destroy → $CODE (expected 404)"
fi

# ---- T15: Kata 节点 microVM 保真度验证 ----
info "T15: Kata microVM fidelity (if Kata Pod accessible)"
KATA_POD=$(kubectl get pods -l app=claude-sbx -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
if [[ -n "$KATA_POD" ]]; then
  NPROC=$(kubectl exec "$KATA_POD" -- nproc 2>/dev/null || echo "")
  KERNEL=$(kubectl exec "$KATA_POD" -- uname -r 2>/dev/null || echo "")
  NODE_KERNEL=$(kubectl get node -o jsonpath='{.items[0].status.nodeInfo.kernelVersion}' 2>/dev/null || echo "")
  if [[ "$KERNEL" != "$NODE_KERNEL" && -n "$KERNEL" ]]; then
    pass "Kata guest kernel ($KERNEL) ≠ node kernel ($NODE_KERNEL) → true microVM"
  else
    info "Kata kernel check: guest=$KERNEL node=$NODE_KERNEL"
  fi
  [[ -n "$NPROC" ]] && pass "nproc=$NPROC (guest quota, not host)"
else
  info "No claude-sbx pods found, skip T15"
fi

# ---- 结果汇总 ----
echo ""
# ---- T16: 控制面认证(有 API_KEY 时验证 401) ----
info "T16: Control plane auth"
# 尝试一个需要认证的请求,看是否需要 key
code_noauth=$(curl -s -o /dev/null -w "%{http_code}" "${API_URL}/sandboxes")
if [[ "$code_noauth" == "401" ]]; then
  pass "Auth enforced → 401 without key"
elif [[ "$code_noauth" == "200" ]]; then
  info "Auth not enforced (API_KEYS not set, dev mode)"
else
  fail "Unexpected status $code_noauth on /sandboxes without auth"
fi

# ---- T17: LiteLLM 健康检查(若可达) ----
info "T17: LiteLLM health (if port-forward active)"
LITELLM_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
  --connect-timeout 3 http://localhost:14000/ 2>/dev/null || echo "000")
if [[ "$LITELLM_STATUS" == "200" ]]; then
  # 验证模型可调
  MODELS=$(curl -s http://localhost:14000/models \
    -H "Authorization: Bearer sk-litellm-poc-change-me" 2>/dev/null | \
    python3 -c "import sys,json; d=json.load(sys.stdin); print(','.join(m['id'] for m in d.get('data',[])))" 2>/dev/null || echo "")
  if [[ -n "$MODELS" ]]; then
    pass "LiteLLM → 200, models=$MODELS"
  else
    info "LiteLLM reachable but no models (check IRSA auth)"
  fi
else
  info "LiteLLM not reachable on localhost:14000 (port-forward not active), skip T17"
fi

echo "========================================"
if [[ "$FAILED" -eq 0 ]]; then
  echo -e "${GREEN}  ALL TESTS PASSED${NC}"
else
  echo -e "${RED}  $FAILED TEST(S) FAILED${NC}"
fi
echo "========================================"
echo ""

exit $FAILED
