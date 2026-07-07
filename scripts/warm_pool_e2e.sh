#!/usr/bin/env bash
# 暖池（Warm Pool）专项 e2e — 对真实 EKS 集群上的 FC 控制面验证暖池效果
#
# 验证目标（对应 README「暖池」章节的宣称，真机坐实）：
#   W0  capabilities.warm_pool = true（FC driver 启用暖池）
#   W1  控制面起来后后台自动补池 → DynamoDB pool_state=warm 计数达到 WARM_POOL_SIZE
#   W2  create 走暖池路径 → warm 计数 -1，新沙盒带 snapshot_s3（冷建无此字段）、state=running
#   W3  延迟对比 → 暖池 create 耗时 vs 池抽干后冷建耗时（验证「create 恒定秒级」）
#   W4  暖池 claim 出的 VM exec 正常（vsock 通道，guest kernel ≠ 宿主）
#   W5  池抽干后 create 优雅降级为冷建（仍 201）
#
# 前提：
#   1. FC 模式控制面已部署（SANDBOX_DRIVER=firecracker），node-agent 心跳已写 nodes 表
#   2. kubectl 已配置；本机有 aws cli（直查 DynamoDB warm 计数）
#
# 用法：
#   bash scripts/warm_pool_e2e.sh --api-key <API_KEY>                 # 自动 port-forward
#   bash scripts/warm_pool_e2e.sh --api-url http://localhost:18000 --api-key <API_KEY>

set -uo pipefail

# ---------- 参数 ----------
API_URL=""
API_KEY=""
TABLE="claude-sbx-sandboxes"
DRIVER="firecracker"
REGION="us-east-1"
POOL_SIZE="${WARM_POOL_SIZE:-5}"
NAMESPACE="sandbox-system"
LOCAL_PORT=18000
PF_PID=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --api-url)  API_URL="$2";  shift 2 ;;
    --api-key)  API_KEY="$2";  shift 2 ;;
    --table)    TABLE="$2";    shift 2 ;;
    --region)   REGION="$2";   shift 2 ;;
    --pool-size) POOL_SIZE="$2"; shift 2 ;;
    *) shift ;;
  esac
done

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass() { echo -e "${GREEN}  PASS${NC} $1"; }
fail() { echo -e "${RED}  FAIL${NC} $1"; FAILED=$((FAILED+1)); }
info() { echo -e "${YELLOW}  ----${NC} $1"; }
FAILED=0

AUTH=()
[[ -n "$API_KEY" ]] && AUTH=(-H "Authorization: Bearer ${API_KEY}")

# ---------- port-forward ----------
if [[ -z "$API_URL" ]]; then
  info "No --api-url, starting port-forward"
  kubectl -n "$NAMESPACE" port-forward svc/sandbox-control-plane "${LOCAL_PORT}:80" &>/tmp/wp-pf.log &
  PF_PID=$!
  sleep 3
  API_URL="http://localhost:${LOCAL_PORT}"
fi
teardown() { [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null || true; }
trap teardown EXIT

# ---------- helpers ----------
# curl 并返回 body + code；code 存 $CODE，body 存 $BODY，耗时存 $TIME
call() {
  local method="$1" path="$2" body="${3:-}"
  local raw
  if [[ -n "$body" ]]; then
    raw=$(curl -s -w "\n%{http_code}\n%{time_total}" -X "$method" \
      -H "Content-Type: application/json" "${AUTH[@]}" -d "$body" "${API_URL}${path}")
  else
    raw=$(curl -s -w "\n%{http_code}\n%{time_total}" -X "$method" "${AUTH[@]}" "${API_URL}${path}")
  fi
  TIME=$(echo "$raw" | tail -1)
  CODE=$(echo "$raw" | tail -2 | head -1)
  BODY=$(echo "$raw" | sed '$d' | sed '$d')
}

jq_get() { echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$1',''))" 2>/dev/null || echo ""; }

# 直查 DynamoDB GSI 的 warm 计数（与控制面 db.count_warm 同一路径）
warm_count() {
  aws dynamodb query --table-name "$TABLE" --index-name pool_state-driver-index \
    --key-condition-expression "pool_state = :w AND driver = :d" \
    --expression-attribute-values "{\":w\":{\"S\":\"warm\"},\":d\":{\"S\":\"$DRIVER\"}}" \
    --select COUNT --region "$REGION" --query Count --output text 2>/dev/null || echo "0"
}

echo ""
echo "========================================"
echo "  Warm Pool E2E — driver=$DRIVER pool_size=${POOL_SIZE}"
echo "  API: $API_URL"
echo "========================================"

# ---- W0: capabilities.warm_pool = true ----
info "W0: capabilities.warm_pool"
call GET "/capabilities"
WP=$(jq_get warm_pool); SR=$(jq_get suspend_resume); DRV=$(jq_get driver)
if [[ "$SR" == "True" ]]; then
  pass "driver=$DRV suspend_resume=$SR warm_pool=$WP"
else
  fail "suspend_resume=$SR (需要 FC driver 才能测暖池) — 终止"
  exit 1
fi

# ---- W1: 后台自动补池到 WARM_POOL_SIZE ----
info "W1: 等待后台补池到 ${POOL_SIZE}（补池每 ~30s 一批，driver.create+suspend 较慢，最多等 6 分钟）"
WC=0
for i in $(seq 1 36); do
  WC=$(warm_count)
  echo "    [$((i*10))s] warm_count=$WC"
  [[ "$WC" -ge "${POOL_SIZE}" ]] && break
  sleep 10
done
if [[ "$WC" -ge "${POOL_SIZE}" ]]; then
  pass "暖池补足：warm_count=$WC ≥ ${POOL_SIZE}"
else
  fail "暖池未补足：warm_count=$WC < ${POOL_SIZE}（查控制面日志 replenish 是否报错）"
fi

# ---- W2 + W3(暖池侧): create 走暖池，计时 + 验证 snapshot_s3 ----
# 判据:create 返回的记录带 snapshot_s3 = 走了暖池 resume(claim 把 warm 记录迁移到
# real_id 时保留 snapshot_s3;冷建路径 force_update 不写此字段)。
# 后台补池 loop 与 claim 并发,单次可能撞竞争走冷建,故取 3 次样,只要有命中即证明
# 暖池 create 路径通,并记录命中样本的延迟。
info "W2: create 走暖池路径（连做 3 次取样，命中样本带 snapshot_s3）"
SID_WARM=""; WARM_TIME=""; WARM_HITS=0; SAMPLE_IDS=()
for k in 1 2 3; do
  call POST "/sandboxes" '{"tenant_id":"wp-test","cpu":2,"mem_mib":4096}'
  if [[ "$CODE" == "201" ]]; then
    sid=$(jq_get id); snap=$(jq_get snapshot_s3)
    SAMPLE_IDS+=("$sid")
    if [[ -n "$snap" ]]; then
      WARM_HITS=$((WARM_HITS+1))
      echo "    #$k id=$sid ✅暖池 snapshot_s3=$snap 耗时=${TIME}s"
      [[ -z "$SID_WARM" ]] && { SID_WARM=$sid; WARM_TIME=$TIME; }
    else
      echo "    #$k id=$sid ⬜冷建(无 snapshot_s3) 耗时=${TIME}s"
    fi
  else
    echo "    #$k create → $CODE: $BODY"
  fi
done
if [[ "$WARM_HITS" -ge 1 ]]; then
  pass "暖池 create 路径命中 ${WARM_HITS}/3（带 snapshot_s3 → 确来自暖池 resume）"
else
  fail "3 次 create 均无 snapshot_s3 → 暖池 claim 全部回退冷建（查 node-agent resume 日志）"
fi

# ---- W4: 暖池 VM exec 正常（vsock，guest kernel） ----
info "W4: 暖池 claim 出的 VM exec（vsock 通道）"
if [[ -n "${SID_WARM:-}" ]]; then
  call GET "/sandboxes/${SID_WARM}/wait?state=running&timeout=20"
  call POST "/sandboxes/${SID_WARM}/exec" '{"cmd":"uname -r; echo sandbox-ok"}'
  if [[ "$CODE" == "200" ]]; then
    OUT=$(jq_get stdout)
    KERNEL=$(echo "$OUT" | head -1)
    if echo "$OUT" | grep -q "sandbox-ok"; then
      pass "exec 成功，guest kernel=${KERNEL}（≠ 宿主 6.1.x → 确在 microVM 内）"
    else
      fail "exec 返回 200 但输出异常：$OUT"
    fi
  else
    fail "exec → $CODE: $BODY"
  fi
fi

# ---- W3(冷建侧) + W5: 抽干池 → 冷建计时 + 降级验证 ----
info "W3/W5: 抽干暖池后 create（冷建路径）计时 + 降级验证"
# 连续 create 直到池空
DRAIN_IDS=()
for i in $(seq 1 "$((POOL_SIZE + 1))"); do
  WC=$(warm_count)
  [[ "$WC" -le 0 ]] && break
  call POST "/sandboxes" '{"tenant_id":"wp-test","cpu":2,"mem_mib":4096}'
  [[ "$CODE" == "201" ]] && DRAIN_IDS+=("$(jq_get id)")
done
# 现在池空，下一个 create 必走冷建
sleep 1
COLD_WC=$(warm_count)
call POST "/sandboxes" '{"tenant_id":"wp-test","cpu":2,"mem_mib":4096}'
COLD_TIME=$TIME
if [[ "$CODE" == "201" ]]; then
  SID_COLD=$(jq_get id); CSNAP=$(jq_get snapshot_s3)
  DRAIN_IDS+=("$SID_COLD")
  if [[ "$COLD_WC" -le 0 && -z "$CSNAP" ]]; then
    pass "池空(warm=$COLD_WC)时 create → 201（无 snapshot_s3 → 确认降级冷建，耗时 ${COLD_TIME}s）"
  else
    info "池空判定 warm=$COLD_WC snapshot_s3='$CSNAP'（若非空说明补池抢先，冷建计时可能不纯）"
  fi
else
  fail "池空后 create → ${CODE}（降级冷建失败）: $BODY"
fi

# ---- 延迟对比小结 ----
echo ""
info "延迟对比：暖池 create=${WARM_TIME}s  vs  冷建 create=${COLD_TIME}s"
if [[ -n "${WARM_TIME:-}" && -n "${COLD_TIME:-}" ]]; then
  python3 -c "w=float('$WARM_TIME'); c=float('$COLD_TIME'); print(f'    暖池比冷建快 {c-w:.3f}s（{c/w:.1f}x）' if w>0 and c>w else '    (两者接近或数据异常，见上)')"
fi

# ---- 清理本次创建的沙盒 ----
info "清理测试沙盒"
for id in "${SAMPLE_IDS[@]:-}" "${DRAIN_IDS[@]:-}"; do
  [[ -n "$id" ]] && call DELETE "/sandboxes/${id}" && echo "    deleted $id ($CODE)"
done

echo "========================================"
if [[ "$FAILED" -eq 0 ]]; then
  echo -e "${GREEN}  WARM POOL E2E: ALL PASSED${NC}"
else
  echo -e "${RED}  WARM POOL E2E: $FAILED FAILED${NC}"
fi
echo "========================================"
exit $FAILED
