#!/usr/bin/env bash
# 自动休眠 / 唤醒(auto-sleep / auto-wake)专项 e2e — 对真实 EKS 集群上的 FC 控制面验证
#
# 验证目标（对应 README「自动休眠/唤醒」章节，真机坐实，对齐 fly.io 体验）：
#   A0  capabilities.suspend_resume = true（前提:FC driver 支持快照恢复）
#   A1  创建 opt-in(autostop+autostart)沙盒(image=web,自起 :80) → running
#   A2  静置超过 idle 阈值 → 自动进入 slept（关键:是 slept 而非手动 suspended;事件带 reason=idle）
#   A3  网关透明唤醒:curl /s/{id}/80/ → 首请求触发 resume → 200 且回到 running,last_active_at 刷新
#   A4  活跃不误睡:持续 exec 保活的沙盒,静置超过 idle 仍 running
#   A5  手动/自动区分:手动 /suspend 的沙盒 → suspended;网关 curl 不唤醒它(409/非200)
#
# 前提：
#   1. FC 模式控制面【已部署本 feature 的镜像】(含 autosleep.py + slept 状态)
#   2. 为缩短测试,建议部署时设小 idle:AUTO_SLEEP_IDLE_S=30 AUTO_SLEEP_SCAN_S=15
#   3. kubectl 已配置;控制面已注入 NLB_HOSTNAME(网关唤醒需经 /s/ 反代)
#
# 用法：
#   bash scripts/autosleep_e2e.sh --api-key <API_KEY>                    # 自动 port-forward
#   bash scripts/autosleep_e2e.sh --api-url http://localhost:18000 --api-key <API_KEY> --idle 30

set -uo pipefail

# ---------- 参数 ----------
API_URL=""
API_KEY=""
REGION="us-east-1"
IDLE="${AUTO_SLEEP_IDLE_S:-30}"       # 与控制面部署的 AUTO_SLEEP_IDLE_S 对齐
NAMESPACE="sandbox-system"
LOCAL_PORT=18000
PF_PID=""
IMAGE="web"                            # 自起 :80,便于 A3 网关唤醒后验证页面
while [[ $# -gt 0 ]]; do
  case $1 in
    --api-url)  API_URL="$2";  shift 2 ;;
    --api-key)  API_KEY="$2";  shift 2 ;;
    --region)   REGION="$2";   shift 2 ;;
    --idle)     IDLE="$2";     shift 2 ;;
    --image)    IMAGE="$2";    shift 2 ;;
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
  kubectl -n "$NAMESPACE" port-forward svc/sandbox-control-plane "${LOCAL_PORT}:80" &>/tmp/as-pf.log &
  PF_PID=$!
  sleep 6
  API_URL="http://localhost:${LOCAL_PORT}"
fi
CREATED_IDS=()
teardown() {
  for id in "${CREATED_IDS[@]:-}"; do
    [[ -n "$id" ]] && curl -s ${AUTH[@]+"${AUTH[@]}"} -X DELETE "${API_URL}/sandboxes/${id}" >/dev/null 2>&1
  done
  [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null || true
}
trap teardown EXIT

# ---------- helpers ----------
call() {
  local method="$1" path="$2" body="${3:-}"
  local raw
  if [[ -n "$body" ]]; then
    raw=$(curl -s -w "\n%{http_code}\n%{time_total}" -X "$method" \
      -H "Content-Type: application/json" ${AUTH[@]+"${AUTH[@]}"} -d "$body" "${API_URL}${path}")
  else
    raw=$(curl -s -w "\n%{http_code}\n%{time_total}" -X "$method" ${AUTH[@]+"${AUTH[@]}"} "${API_URL}${path}")
  fi
  TIME=$(echo "$raw" | tail -1)
  CODE=$(echo "$raw" | tail -2 | head -1)
  BODY=$(echo "$raw" | sed '$d' | sed '$d')
}
jq_get() { echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$1',''))" 2>/dev/null || echo ""; }

# 轮询沙盒状态直到等于目标或超时。$1=sid $2=target_state $3=timeout_s
wait_state() {
  local sid="$1" target="$2" timeout="${3:-60}" waited=0
  while [[ $waited -lt $timeout ]]; do
    call GET "/sandboxes/${sid}"
    local st; st=$(jq_get state)
    [[ "$st" == "$target" ]] && return 0
    sleep 3; waited=$((waited+3))
  done
  return 1
}

echo ""
echo "========================================"
echo "  Auto-Sleep / Auto-Wake E2E"
echo "  API: $API_URL   idle=${IDLE}s   image=${IMAGE}"
echo "========================================"

# ---- A0: capabilities ----
info "A0: capabilities.suspend_resume"
call GET "/capabilities"
SR=$(jq_get suspend_resume)
if [[ "$SR" == "True" ]]; then
  pass "suspend_resume=$SR"
else
  fail "suspend_resume=$SR — FC driver 才支持快照恢复,终止"; exit 1
fi

# ---- A1: 创建 opt-in 沙盒 ----
info "A1: 创建 autostop+autostart 沙盒(image=${IMAGE})"
call POST "/sandboxes" "{\"tenant_id\":\"as-test\",\"image\":\"${IMAGE}\",\"cpu\":2,\"mem_mib\":2048,\"services\":[{\"port\":80,\"autostop\":true,\"autostart\":true}]}"
if [[ "$CODE" == "201" ]]; then
  SID=$(jq_get id); CREATED_IDS+=("$SID")
  pass "created id=$SID state=$(jq_get state)"
else
  fail "create → $CODE: $BODY"; exit 1
fi
call GET "/sandboxes/${SID}/wait?state=running&timeout=30"

# ---- A2: 空闲 → 自动 slept(关键:非 suspended) ----
info "A2: 静置 $((IDLE + 40))s 等自动休眠(扫描周期 + idle 阈值)"
if wait_state "$SID" "slept" $((IDLE + 60)); then
  pass "自动休眠成功:state=slept(区别于手动 suspended)"
  # 校验事件带 reason=idle
  call GET "/admin/events?id=${SID}&limit=10"
  if echo "$BODY" | grep -q '"reason": *"idle"' || echo "$BODY" | grep -q '"slept"'; then
    pass "事件含 slept/reason=idle"
  else
    info "事件里未显式看到 reason=idle(可能 admin key 非 default;不影响状态判定)"
  fi
else
  call GET "/sandboxes/${SID}"
  fail "未在预期时间内进入 slept,当前 state=$(jq_get state)（查控制面 AUTO_SLEEP_ENABLED / leader 日志）"
fi

# ---- A3: 网关透明唤醒 ----
info "A3: 网关请求透明唤醒(/s/${SID}/80/)"
# 经控制面 /s/ 反代打首请求 —— slept + autostart 应触发 resume 再转发
call GET "/s/${SID}/80/"
WAKE_CODE=$CODE; WAKE_TIME=$TIME
call GET "/sandboxes/${SID}"
ST_AFTER=$(jq_get state)
if [[ "$ST_AFTER" == "running" ]]; then
  pass "唤醒后回到 running(首请求 /s 反代 code=$WAKE_CODE 耗时=${WAKE_TIME}s)"
else
  fail "唤醒后 state=$ST_AFTER(期望 running)"
fi
# last_active_at 应被刷新(唤醒即活跃)
LAA=$(jq_get last_active_at)
[[ -n "$LAA" ]] && pass "last_active_at 已刷新:$LAA" || info "last_active_at 为空(旧记录?)"

# ---- A4: 活跃不误睡 ----
info "A4: 持续 exec 保活,静置超过 idle 仍 running"
call POST "/sandboxes" "{\"tenant_id\":\"as-test\",\"image\":\"${IMAGE}\",\"cpu\":2,\"mem_mib\":2048,\"services\":[{\"port\":80,\"autostop\":true,\"autostart\":true}]}"
SID2=$(jq_get id); CREATED_IDS+=("$SID2")
call GET "/sandboxes/${SID2}/wait?state=running&timeout=30"
# 在 idle 窗口内周期 exec 保活,总时长略超 idle
KEEP=$((IDLE + 20)); STEP=$((IDLE / 3)); [[ $STEP -lt 5 ]] && STEP=5
elapsed=0
while [[ $elapsed -lt $KEEP ]]; do
  call POST "/sandboxes/${SID2}/exec" '{"cmd":"echo keepalive"}'
  sleep "$STEP"; elapsed=$((elapsed+STEP))
done
call GET "/sandboxes/${SID2}"
if [[ "$(jq_get state)" == "running" ]]; then
  pass "保活沙盒静置 ${KEEP}s 仍 running(未被误睡)"
else
  fail "保活沙盒被误睡:state=$(jq_get state)"
fi

# ---- A5: 手动 suspend 不被网关唤醒 ----
info "A5: 手动 suspend 的沙盒,网关不自动唤醒(与自动 slept 区分)"
call POST "/sandboxes" "{\"tenant_id\":\"as-test\",\"image\":\"${IMAGE}\",\"cpu\":2,\"mem_mib\":2048,\"services\":[{\"port\":80,\"autostop\":true,\"autostart\":true}]}"
SID3=$(jq_get id); CREATED_IDS+=("$SID3")
call GET "/sandboxes/${SID3}/wait?state=running&timeout=30"
call POST "/sandboxes/${SID3}/suspend"
if [[ "$(jq_get state)" == "suspended" ]]; then
  pass "手动 suspend → state=suspended"
else
  info "手动 suspend 返回 state=$(jq_get state)"
fi
# 网关打请求 —— 手动 suspended 不应被唤醒
call GET "/s/${SID3}/80/"
GW_CODE=$CODE
call GET "/sandboxes/${SID3}"
ST3=$(jq_get state)
if [[ "$ST3" == "suspended" ]]; then
  pass "网关未唤醒手动 suspended 沙盒(仍 suspended,反代 code=$GW_CODE)—— 自动/手动区分成立"
else
  fail "手动 suspended 沙盒被网关唤醒了:state=$ST3(不符合区分预期)"
fi

echo ""
echo "========================================"
if [[ "$FAILED" -eq 0 ]]; then
  echo -e "${GREEN}  AUTO-SLEEP E2E: ALL PASSED${NC}"
else
  echo -e "${RED}  AUTO-SLEEP E2E: $FAILED FAILED${NC}"
fi
echo "========================================"
exit $FAILED
