#!/usr/bin/env bash
# 用 SSM 在一台或多台实例上执行本地脚本并打印输出。
# 用法: ssm_run.sh <script-file> <instance-id>[,<instance-id>...] [timeout_s]
set -euo pipefail
SCRIPT="$1"; IDS="${2//,/ }"; TIMEOUT="${3:-600}"
REGION="${AWS_REGION:-us-east-1}"
PARAMS=$(python3 -c "import json,sys;print(json.dumps({'commands':open(sys.argv[1]).read().splitlines(),'executionTimeout':[sys.argv[2]]}))" "$SCRIPT" "$TIMEOUT")
# shellcheck disable=SC2086
CID=$(aws ssm send-command --region "$REGION" --instance-ids $IDS \
  --document-name AWS-RunShellScript --parameters "$PARAMS" \
  --timeout-seconds 60 --query Command.CommandId --output text)
for i in $IDS; do
  while :; do
    ST=$(aws ssm get-command-invocation --region "$REGION" --command-id "$CID" --instance-id "$i" --query Status --output text 2>/dev/null || echo Pending)
    case "$ST" in Pending|InProgress|Delayed) sleep 3 ;; *) break ;; esac
  done
  echo "##### $i [$ST]"
  aws ssm get-command-invocation --region "$REGION" --command-id "$CID" --instance-id "$i" \
    --query '[StandardOutputContent,StandardErrorContent]' --output text
done
