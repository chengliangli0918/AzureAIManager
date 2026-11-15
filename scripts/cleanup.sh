#!/usr/bin/env bash
# Deletes the demo from the Azure CLI's default subscription in reverse order (model deployments first, so
# GPU billing stops as early as possible), following the cleanup section of the testing guide. The resource
# group is kept unless asked otherwise.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/config.sh
source "$SCRIPT_DIR/config.sh"

AUTO_APPROVE=false
DELETE_RG=false

usage() {
  cat <<EOF
Usage: $(basename "$0") [--yes] [--delete-resource-group]

  -y, --yes                    Don't ask for confirmation.
      --delete-resource-group  Also delete resource group $RG (and everything else in it).

Works in the Azure CLI's default subscription (az account set -s <subscription-id>).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -y | --yes) AUTO_APPROVE=true ;;
    --delete-resource-group) DELETE_RG=true ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; die "Unknown argument: $1" ;;
  esac
  shift
done

require az jq
use_default_subscription
log "Subscription '$SUB_NAME' ($SUB), the Azure CLI default"
check_demo_config_subscription

exists() { "$@" -o none --only-show-errors >/dev/null 2>&1; }

wait_until_deleted() {
  local deadline=$((SECONDS + 3600))
  while exists "$@"; do
    ((SECONDS < deadline)) || die "Timed out waiting for the deletion to finish."
    sleep 20
  done
}

if [[ "$AUTO_APPROVE" != true ]]; then
  [[ -t 0 ]] || die "Not running interactively; re-run with --yes to confirm."
  what="AIManager $AIM_NAME ($NS1/$MD1, $NS2/$MD2) in $RG"
  if [[ "$DELETE_RG" == true ]]; then what+=" and resource group $RG"; fi
  read -r -p "Delete $what from subscription '$SUB_NAME'? [y/N] " answer
  [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]] || die "Aborted."
fi

if exists az aimanager show -g "$RG" -n "$AIM_NAME"; then
  use_signed_in_account
  log "Removing '$INFERENCE_ROLE' from $DEMO_USER on $NS1"
  INFERENCE_ROLE_ID="$(inference_role_id)"
  ids="$(inference_assignments_at "$DEMO_USER_OID" "$(ns_scope "$NS1")")"
  if [[ -n "$ids" ]]; then
    # shellcheck disable=SC2086 # one assignment ID per word
    az role assignment delete --ids $ids --only-show-errors
    ok "Removed"
  else
    ok "Not assigned"
  fi

  log "Deleting model deployments"
  for pair in "$NS1/$MD1" "$NS2/$MD2"; do
    ns="${pair%%/*}" md="${pair#*/}"
    if exists az aimanager namespace modeldeployment show -g "$RG" -m "$AIM_NAME" --namespace "$ns" -n "$md"; then
      az aimanager namespace modeldeployment delete -g "$RG" -m "$AIM_NAME" --namespace "$ns" -n "$md" --yes --no-wait --only-show-errors
      info "Deleting $pair"
    fi
  done
  for pair in "$NS1/$MD1" "$NS2/$MD2"; do
    ns="${pair%%/*}" md="${pair#*/}"
    wait_until_deleted az aimanager namespace modeldeployment show -g "$RG" -m "$AIM_NAME" --namespace "$ns" -n "$md"
    ok "$pair deleted"
  done

  log "Deleting namespaces"
  for ns in "$NS1" "$NS2"; do
    if exists az aimanager namespace show -g "$RG" -m "$AIM_NAME" -n "$ns"; then
      az aimanager namespace delete -g "$RG" -m "$AIM_NAME" -n "$ns" --yes --only-show-errors
    fi
    ok "Namespace $ns deleted"
  done

  log "Deleting AIManager $AIM_NAME"
  az aimanager delete -g "$RG" -n "$AIM_NAME" --yes --only-show-errors
  ok "Deleted"
else
  ok "AIManager $AIM_NAME was not found in $RG; nothing to delete."
fi

if [[ "$DELETE_RG" == true && "$(az group exists --name "$RG" --only-show-errors)" == true ]]; then
  log "Deleting resource group $RG"
  az group delete --name "$RG" --yes --no-wait --only-show-errors
  ok "Deletion started"
fi

rm -f "$DEMO_CONFIG"
