# source 本文件后再跑 stage2 terraform:从 .sbxkeys 注入敏感变量(不写入仓库)
K="${SBXKEYS_DIR:-/Users/teaguexiao/Documents/aws-claude-sandbox-poc/.sbxkeys}"
export TF_VAR_api_keys="$(cat "$K/live_api_keys")"
export TF_VAR_litellm_master_key="$(cat "$K/live_litellm_key")"
