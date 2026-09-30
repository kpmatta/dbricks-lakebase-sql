#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="${APP_NAME:-lakebase-workbench}"
PROFILE="${DATABRICKS_CONFIG_PROFILE:-${PROFILE:-}}"
STAGING_DIR=""

cleanup() {
  if [[ -n "$STAGING_DIR" ]]; then
    rm -rf "$STAGING_DIR"
  fi
}
trap cleanup EXIT

usage() {
  cat <<'EOF'
Usage: scripts/workbench.sh <run|sync|deploy>

Commands:
  run      Sync Python dependencies and run the app locally with uv.
  sync     Upload the project source to the current user's Databricks workspace.
  deploy   Sync the source, create the Databricks App if needed, and deploy it.

Environment:
  PROFILE or DATABRICKS_CONFIG_PROFILE  Databricks CLI profile (sync/deploy)
  APP_NAME                              App name (default: lakebase-workbench)
  WORKSPACE_PATH                        Override workspace source path
  DATABRICKS_HOST, PGHOST               Required by local run
EOF
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'Required command not found: %s\n' "$1" >&2
    exit 1
  }
}

require_profile() {
  require_command databricks
  if [[ -z "$PROFILE" ]]; then
    printf 'Set PROFILE or DATABRICKS_CONFIG_PROFILE to a configured Databricks CLI profile.\n' >&2
    exit 1
  fi
}

ensure_app_config() {
  local app_config="$ROOT_DIR/app.yaml"
  if [[ ! -f "$app_config" ]]; then
    cp "$ROOT_DIR/app.yaml.template" "$app_config"
    printf 'Created app.yaml from template. Set PGHOST in %s, then rerun.\n' "$app_config" >&2
    exit 1
  fi
  if grep -q '<your-lakebase-endpoint-host>' "$app_config"; then
    printf 'Replace the PGHOST placeholder in %s before syncing or deploying.\n' "$app_config" >&2
    exit 1
  fi
}

sync_source() {
  require_profile
  ensure_app_config
  require_command rsync

  local workspace_user workspace_path
  workspace_user="$(databricks current-user me --profile "$PROFILE" -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["userName"])')"
  workspace_path="${WORKSPACE_PATH:-/Workspace/Users/$workspace_user/apps/$APP_NAME}"
  STAGING_DIR="$(mktemp -d)"

  rsync -a \
    --exclude='.git/' \
    --exclude='.databricks/' \
    --exclude='.venv/' \
    --exclude='venv/' \
    --exclude='__pycache__/' \
    --exclude='.env' \
    --exclude='.env.*' \
    --exclude='*.pyc' \
    "$ROOT_DIR/" "$STAGING_DIR/"

  databricks workspace mkdirs "$workspace_path" --profile "$PROFILE"
  databricks workspace import-dir "$STAGING_DIR" "$workspace_path" --overwrite --profile "$PROFILE"
  printf 'Synced source to %s\n' "$workspace_path"
}

case "${1:-}" in
  run)
    require_command uv
    : "${DATABRICKS_HOST:?Set DATABRICKS_HOST before running locally.}"
    : "${PGHOST:?Set PGHOST to the Lakebase endpoint host before running locally.}"
    export PGPORT="${PGPORT:-5432}"
    export PGDATABASE="${PGDATABASE:-databricks_postgres}"
    export PGSSLMODE="${PGSSLMODE:-require}"
    cd "$ROOT_DIR"
    uv sync
    exec uv run app.py
    ;;
  sync)
    sync_source
    ;;
  deploy)
    require_profile
    sync_source
    workspace_user="$(databricks current-user me --profile "$PROFILE" -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["userName"])')"
    workspace_path="${WORKSPACE_PATH:-/Workspace/Users/$workspace_user/apps/$APP_NAME}"
    databricks apps get "$APP_NAME" --profile "$PROFILE" -o json >/dev/null 2>&1 || \
      databricks apps create "$APP_NAME" --description "Lakebase SQL Workbench" --profile "$PROFILE"
    databricks apps deploy "$APP_NAME" --source-code-path "$workspace_path" --profile "$PROFILE"
    databricks apps get "$APP_NAME" --profile "$PROFILE" -o json
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac