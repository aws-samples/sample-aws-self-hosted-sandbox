#!/usr/bin/env bash
# Route A acceptance test: exercise the unchanged REST API while proving each
# lifecycle request is reconciled through FirecrackerSandbox CRs.
set -euo pipefail

API_URL=""
API_KEY=""
NAMESPACE="sandbox-system"
LOCAL_PORT=18001
PF_PID=""
SID=""
FAILED=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-url) API_URL="$2"; shift 2 ;;
    --api-key) API_KEY="$2"; shift 2 ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    *) shift ;;
  esac
done

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass() { echo -e "${GREEN}  PASS${NC} $1"; }
fail() { echo -e "${RED}  FAIL${NC} $1"; FAILED=$((FAILED + 1)); }
info() { echo -e "${YELLOW}  ----${NC} $1"; }

cleanup() {
  if [[ -n "$SID" && -n "$API_URL" ]]; then
    local cleanup_auth=()
    [[ -n "$API_KEY" ]] && cleanup_auth=(
      -H "Authorization: Bearer ${API_KEY}"
    )
    curl -fsS -X DELETE "${cleanup_auth[@]}" \
      "${API_URL}/sandboxes/${SID}" >/dev/null 2>&1 || true
  fi
  [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null || true
}
trap cleanup EXIT

if [[ -z "$API_URL" ]]; then
  kubectl -n "$NAMESPACE" port-forward \
    svc/sandbox-control-plane "${LOCAL_PORT}:80" >/tmp/crd-e2e-pf.log 2>&1 &
  PF_PID=$!
  sleep 3
  API_URL="http://localhost:${LOCAL_PORT}"
fi

AUTH=()
[[ -n "$API_KEY" ]] && AUTH=(-H "Authorization: Bearer ${API_KEY}")

call() {
  local method="$1" path="$2" body="${3:-}" raw
  if [[ -n "$body" ]]; then
    raw=$(curl -sS -w "\n%{http_code}" -X "$method" \
      "${AUTH[@]}" -H "Content-Type: application/json" \
      -d "$body" "${API_URL}${path}")
  else
    raw=$(curl -sS -w "\n%{http_code}" -X "$method" \
      "${AUTH[@]}" "${API_URL}${path}")
  fi
  CODE=$(echo "$raw" | tail -1)
  BODY=$(echo "$raw" | awk 'NR>1{print prev} {prev=$0}')
}

json_get() {
  local field="$1"
  echo "$BODY" | python3 -c \
    "import json,sys; print(json.load(sys.stdin).get('$field',''))" \
    2>/dev/null || true
}

cr_field() {
  kubectl -n "$NAMESPACE" get firecrackersandbox "$SID" \
    -o "jsonpath={$1}" 2>/dev/null || true
}

assert_cr() {
  local desired="$1" phase="$2" reason="${3:-}" got_desired got_phase got_reason
  got_desired=$(cr_field '.spec.desiredState')
  got_phase=$(cr_field '.status.phase')
  got_reason=$(cr_field '.spec.suspendReason')
  if [[ "$got_desired" == "$desired" && "$got_phase" == "$phase" &&
        ( -z "$reason" || "$got_reason" == "$reason" ) ]]; then
    pass "CR desired=$got_desired status.phase=$got_phase reason=${got_reason:-none}"
  else
    fail "CR expected desired=$desired phase=$phase reason=$reason; got desired=$got_desired phase=$got_phase reason=$got_reason"
  fi
}

echo "========================================"
echo "  Route A CRD E2E"
echo "========================================"

info "CRD, operator, API and unchanged node-agent topology"
kubectl get crd firecrackersandboxes.sandbox.memorion.ai >/dev/null
kubectl -n "$NAMESPACE" rollout status deploy/firecracker-operator --timeout=180s
kubectl -n "$NAMESPACE" rollout status deploy/sandbox-control-plane --timeout=180s
if kubectl -n "$NAMESPACE" get ds node-agent >/dev/null 2>&1; then
  DESIRED=$(kubectl -n "$NAMESPACE" get ds node-agent -o jsonpath='{.status.desiredNumberScheduled}')
  READY=$(kubectl -n "$NAMESPACE" get ds node-agent -o jsonpath='{.status.numberReady}')
  [[ "$DESIRED" == "$READY" && "$READY" != "0" ]] \
    && pass "node-agent DaemonSet unchanged and ready ($READY/$DESIRED)" \
    || fail "node-agent DaemonSet not ready ($READY/$DESIRED)"
else
  fail "node-agent DaemonSet missing"
fi

info "Create through existing POST /sandboxes"
IDEM="crd-e2e-$(date +%s)"
call POST "/sandboxes" \
  "{\"tenant_id\":\"crd-e2e\",\"image\":\"min\",\"cpu\":1,\"mem_mib\":2048,\"idempotency_key\":\"${IDEM}\"}"
SID=$(json_get id)
if [[ "$CODE" == "201" && -n "$SID" ]]; then
  pass "REST create returned 201 id=$SID"
else
  fail "REST create returned $CODE: $BODY"
  exit 1
fi
assert_cr "Running" "running"

info "Exec through existing API"
call POST "/sandboxes/${SID}/exec" '{"cmd":"echo crd-route-a-ok"}'
if [[ "$CODE" == "200" && "$(json_get stdout)" == *"crd-route-a-ok"* ]]; then
  pass "exec still reaches the Firecracker guest"
else
  fail "exec returned $CODE: $BODY"
fi

info "Manual suspend through CRD desired state"
call POST "/sandboxes/${SID}/suspend"
[[ "$CODE" == "200" && "$(json_get state)" == "suspended" ]] \
  && pass "REST suspend contract unchanged" \
  || fail "REST suspend returned $CODE: $BODY"
assert_cr "Suspended" "suspended" "manual"

info "Resume through CRD desired state"
call POST "/sandboxes/${SID}/resume"
[[ "$CODE" == "200" && "$(json_get state)" == "running" ]] \
  && pass "REST resume contract unchanged" \
  || fail "REST resume returned $CODE: $BODY"
assert_cr "Running" "running"

info "Delete through CR finalizer"
call DELETE "/sandboxes/${SID}"
if [[ "$CODE" == "200" ]] && ! kubectl -n "$NAMESPACE" \
  get firecrackersandbox "$SID" >/dev/null 2>&1; then
  pass "REST delete completed and finalizer removed the CR"
  SID=""
else
  fail "delete returned $CODE or CR still exists: $BODY"
fi

echo "========================================"
if [[ "$FAILED" -eq 0 ]]; then
  echo -e "${GREEN}  CRD E2E: ALL PASSED${NC}"
else
  echo -e "${RED}  CRD E2E: $FAILED FAILED${NC}"
fi
exit "$FAILED"
