#!/usr/bin/env bash
set -euo pipefail

: "${AMG_WORKSPACE_ID:?AMG_WORKSPACE_ID is required}"
: "${AMP_ENDPOINT:?AMP_ENDPOINT is required}"
: "${AWS_REGION:?AWS_REGION is required}"
: "${DASHBOARD_FILE:=sandbox-platform.json}"

for command in aws curl jq kubectl; do
  command -v "$command" >/dev/null || {
    printf 'required command is unavailable: %s\n' "$command" >&2
    exit 1
  }
done

workspace_endpoint="$(aws grafana describe-workspace \
  --workspace-id "$AMG_WORKSPACE_ID" \
  --region "$AWS_REGION" \
  --query 'workspace.endpoint' \
  --output text)"
grafana_url="https://${workspace_endpoint}"
account_name="terraform-p2-$(date +%s)-$$"
token_name="terraform-p2-$$"
service_account_id=""
token_id=""
token_key=""

cleanup() {
  if [[ -n "$token_id" && -n "$service_account_id" ]]; then
    aws grafana delete-workspace-service-account-token \
      --workspace-id "$AMG_WORKSPACE_ID" \
      --service-account-id "$service_account_id" \
      --token-id "$token_id" \
      --region "$AWS_REGION" >/dev/null 2>&1 || true
  fi
  if [[ -n "$service_account_id" ]]; then
    aws grafana delete-workspace-service-account \
      --workspace-id "$AMG_WORKSPACE_ID" \
      --service-account-id "$service_account_id" \
      --region "$AWS_REGION" >/dev/null 2>&1 || true
  fi
  token_key=""
}
trap cleanup EXIT

service_account_id="$(aws grafana create-workspace-service-account \
  --workspace-id "$AMG_WORKSPACE_ID" \
  --grafana-role ADMIN \
  --name "$account_name" \
  --region "$AWS_REGION" \
  --query id \
  --output text)"

token_response="$(aws grafana create-workspace-service-account-token \
  --workspace-id "$AMG_WORKSPACE_ID" \
  --service-account-id "$service_account_id" \
  --name "$token_name" \
  --seconds-to-live 900 \
  --region "$AWS_REGION" \
  --output json)"
token_id="$(jq -r '.serviceAccountToken.id' <<<"$token_response")"
token_key="$(jq -r '.serviceAccountToken.key' <<<"$token_response")"
unset token_response

api() {
  local method="$1"
  local path="$2"
  local payload="${3:-}"
  local response_file
  local status
  response_file="$(mktemp)"
  if [[ -n "$payload" ]]; then
    status="$(printf 'Authorization: Bearer %s\n' "$token_key" | curl --silent --show-error \
      -X "$method" \
      --header @- \
      -H "Content-Type: application/json" \
      --data-binary "$payload" \
      --output "$response_file" \
      --write-out '%{http_code}' \
      "${grafana_url}${path}")"
  else
    status="$(printf 'Authorization: Bearer %s\n' "$token_key" | curl --silent --show-error \
      -X "$method" \
      --header @- \
      --output "$response_file" \
      --write-out '%{http_code}' \
      "${grafana_url}${path}")"
  fi
  if (( status < 200 || status >= 300 )); then
    printf 'Grafana API %s %s failed with HTTP %s: ' "$method" "$path" "$status" >&2
    jq -r '.message // .error // "unknown error"' "$response_file" >&2 \
      || printf 'unparseable response\n' >&2
    rm -f "$response_file"
    return 1
  fi
  cat "$response_file"
  rm -f "$response_file"
}

datasource="$(
  jq -n \
    --arg endpoint "$AMP_ENDPOINT" \
    --arg region "$AWS_REGION" \
    '{
      name: "Amazon Managed Service for Prometheus",
      type: "prometheus",
      uid: "sandbox-amp",
      access: "proxy",
      url: $endpoint,
      isDefault: true,
      jsonData: {
        httpMethod: "POST",
        sigV4Auth: true,
        sigV4AuthType: "default",
        sigV4Region: $region
      }
    }'
)"

if api GET "/api/datasources/uid/sandbox-amp" >/dev/null 2>&1; then
  api PUT "/api/datasources/uid/sandbox-amp" "$datasource" >/dev/null
else
  api POST "/api/datasources" "$datasource" >/dev/null
fi

dashboard_json="$(kubectl -n monitoring get configmap sandbox-platform-dashboard \
  -o "jsonpath={.data.${DASHBOARD_FILE//./\\.}}")"
dashboard_payload="$(
  jq -n \
    --argjson dashboard "$dashboard_json" \
    '{dashboard: $dashboard, overwrite: true, folderId: 0}'
)"
api POST "/api/dashboards/db" "$dashboard_payload" >/dev/null

health="$(api GET "/api/datasources/uid/sandbox-amp/health")"
if [[ "$(jq -r '.status // empty' <<<"$health")" != "OK" ]]; then
  printf 'managed Grafana datasource health check failed\n' >&2
  exit 1
fi

printf 'Managed Grafana datasource and dashboard configured successfully.\n'
