#!/usr/bin/env bash
# 内存压力分阶段：在选中沙盒里各起一个 memtouch(后台常驻),然后持续采样宿主直到稳定。
# 用法: mem_stage.sh <node> <instance-id> <prefix> <range> <mb> <tag> <out.jsonl> [pattern]
set -uo pipefail
NODE=$1; IID=$2; PREFIX=$3; RANGE=$4; MB=$5; TAG=$6; OUT=$7; PATTERN=${8:-mixed}
HERE=$(cd "$(dirname "$0")" && pwd)
python3 "$HERE/overcommit_bench.py" --node "$NODE" --out "$OUT" --prefix "$PREFIX" --tag "$TAG-launch" exec --range "$RANGE" --concurrency 32 --timeout 30 \
  --exec-cmd "setsid nohup /usr/local/bin/python3 /root/memtouch.py --mb $MB --pattern $PATTERN --random-pct 20 > /run/mt-\$(date +%s%N).log 2>&1 < /dev/null & sleep 0.1; echo started" \
  | python3 -c "import sys,json;r=json.loads(sys.stdin.read());print('launch ok',r['ok'],'/',r['n'],r['errors'][:2])"
for i in $(seq 1 12); do
  sleep 15
  python3 "$HERE/overcommit_bench.py" --node "$NODE" --out "$OUT" --tag "$TAG-t$i" sample --seconds 3 | python3 -c "
import sys,json
h=json.loads(sys.stdin.read())['host']
print('t=%3ds fc=%d fc_rss=%dGiB avail=%dGiB free=%dGiB psi_mem_full10=%s oom_kill=%s pswpout=%s'%($i*15,h['fc_count'],h['fc_rss_mib']//1024,h['mem_available_mib']//1024,h['mem_free_mib']//1024,h['psi_mem'].get('full',{}).get('avg10'),h['vmstat'].get('oom_kill'),h['vmstat'].get('pswpout')))"
done
kubectl get node "$NODE" -o jsonpath='{range .status.conditions[*]}{.type}={.status} {end}'; echo
kubectl get events -A --field-selector involvedObject.kind=Node,involvedObject.name="$NODE" --sort-by=.lastTimestamp 2>/dev/null | tail -5
kubectl get events -A --sort-by=.lastTimestamp 2>/dev/null | grep -i -E 'evict|oom|pressure' | tail -5
