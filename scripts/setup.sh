#!/usr/bin/env bash
# Builds the AIManager namespace-RBAC demo in the Azure CLI's default subscription, following the
# "AIManager Partner Team Preview Testing Guide":
#
#   AIManager $AIM_NAME ($LOCATION)
#   ├── namespace $NS1 ── model deployment $MD1    your az login account has "Azure AIManager Inference User"
#   └── namespace $NS2 ── model deployment $MD2    your az login account has no inference role
#
# Both model deployments serve $MODEL_ID on $VM_SIZE. The namespace gateway endpoints are written to
# demo-config.json for scripts/verify.sh and the Streamlit client. Safe to re-run: existing resources are kept.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/config.sh
source "$SCRIPT_DIR/config.sh"

AUTO_APPROVE=false
SKIP_VERIFY=false
WAIT_TIMEOUT_MIN="${WAIT_TIMEOUT_MIN:-120}"
POLL_SECONDS="${POLL_SECONDS:-60}"
NAMESPACES=("$NS1" "$NS2")
DEPLOYMENTS=("$MD1" "$MD2")

usage() {
  cat <<EOF
Usage: $(basename "$0") [--yes] [--dry-run] [--skip-verify]

  -y, --yes          Create the GPU model deployments without asking for confirmation.
      --dry-run      Run the read-only checks and print the commands that would change Azure.
      --skip-verify  Don't call the inference endpoints at the end (scripts/verify.sh).

The demo goes into the Azure CLI's default subscription; choose it first with:
  az account set -s <subscription-id>

Other defaults are in scripts/config.sh; override them with environment variables, for example:
  AIM_NAME=my-aim MODEL_ID=Qwen/Qwen3.5-9B $(basename "$0")
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -y | --yes) AUTO_APPROVE=true ;;
    --dry-run) DRY_RUN=true ;;
    --skip-verify) SKIP_VERIFY=true ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; die "Unknown argument: $1" ;;
  esac
  shift
done

deployment_state() {
  az aimanager namespace modeldeployment show -g "$RG" -m "$AIM_NAME" --namespace "$1" -n "$2" \
    --query properties.provisioningState -o tsv --only-show-errors 2>/dev/null || true
}

preflight() {
  log "Pre-flight checks"
  require az jq curl
  use_default_subscription
  use_signed_in_account
  ok "Subscription: $SUB_NAME ($SUB), the Azure CLI default"
  ok "Account: $DEMO_USER (object ID $DEMO_USER_OID), the az login account"
  check_demo_config_subscription

  local feature_state
  feature_state="$(az feature show --namespace Microsoft.ContainerService --name AIManagerPreview \
    --query properties.state -o tsv --only-show-errors 2>/dev/null || true)"
  if [[ "$feature_state" != Registered ]]; then
    run az feature register --namespace Microsoft.ContainerService --name AIManagerPreview -o none
    die "Feature Microsoft.ContainerService/AIManagerPreview is '${feature_state:-NotRegistered}'. It needs manual approval (see the testing guide); re-run once it is 'Registered'."
  fi
  ok "Feature Microsoft.ContainerService/AIManagerPreview is registered"

  # The extension's reported version can lag behind the package, so probe for the command instead.
  if ! az extension show --name aimanager -o none >/dev/null 2>&1 ||
    ! az aimanager namespace modeldeployment create --help >/dev/null 2>&1; then
    info "Installing the latest aimanager CLI extension"
    az extension add --name aimanager --allow-preview true --upgrade --only-show-errors
  fi
  az aimanager namespace modeldeployment create --help >/dev/null 2>&1 ||
    die "The aimanager CLI extension is too old; 2.0.1b1 or later is required."
  ok "aimanager CLI extension is installed"

  INFERENCE_ROLE_ID="$(inference_role_id)"
  [[ -n "$INFERENCE_ROLE_ID" ]] || die "Built-in role '$INFERENCE_ROLE' was not found."
  ok "Role '$INFERENCE_ROLE' has ID $INFERENCE_ROLE_ID"

  MODEL_NAME="$(az aimanager model list --location "$LOCATION" \
    --query "[?properties.modelId=='$MODEL_ID'].name | [0]" -o tsv --only-show-errors)"
  [[ -n "$MODEL_NAME" ]] ||
    die "Model '$MODEL_ID' is not in the AIManager catalog for $LOCATION (list: az aimanager model list -l $LOCATION -o table)."
  MODEL_RESOURCE_ID="/subscriptions/$SUB/providers/Microsoft.ContainerService/locations/$LOCATION/aiModels/$MODEL_NAME"
  ok "Model $MODEL_ID is $MODEL_NAME"
}

check_capacity() {
  log "Checking that $VM_SIZE can serve $MODEL_ID in $LOCATION"
  local i
  MISSING_DEPLOYMENTS=0
  for i in 0 1; do
    if [[ ! "$(deployment_state "${NAMESPACES[i]}" "${DEPLOYMENTS[i]}")" =~ ^(Succeeded|Creating|Updating)$ ]]; then
      MISSING_DEPLOYMENTS=$((MISSING_DEPLOYMENTS + 1))
    fi
  done

  local plan feasible reason
  plan="$(az aimanager model calculate-cost --location "$LOCATION" --name "$MODEL_NAME" \
    --query "plans[?vmSize=='$VM_SIZE'] | [0]" -o json --only-show-errors)"
  [[ -n "$plan" && "$plan" != null ]] ||
    die "$VM_SIZE is not a deployment option for $MODEL_ID in $LOCATION (see: az aimanager model calculate-cost -l $LOCATION -n $MODEL_NAME -o table)."
  feasible="$(jq -r '.feasible // false' <<<"$plan")"
  reason="$(jq -r '.infeasibilityReason.code // "unknown reason"' <<<"$plan")"
  HOURLY_PRICE="$(jq -r '.totalHourlyPrice // empty' <<<"$plan")"
  if [[ "$feasible" == true ]]; then
    ok "$VM_SIZE is feasible: \$${HOURLY_PRICE:-?}/hour per replica, $(jq -r '.maxAvailableReplicas // "?"' <<<"$plan") replica(s) of quota available"
  elif ((MISSING_DEPLOYMENTS == 0)); then
    warn "$VM_SIZE reports '$reason', but both model deployments already exist."
  else
    die "$VM_SIZE cannot serve $MODEL_ID in $LOCATION right now: $reason."
  fi
}

confirm_cost() {
  if ((MISSING_DEPLOYMENTS == 0)) || [[ "$AUTO_APPROVE" == true || "$DRY_RUN" == true ]]; then return 0; fi
  local replicas=$((MISSING_DEPLOYMENTS * REPLICAS)) total answer
  total="$(awk -v price="${HOURLY_PRICE:-0}" -v n="$replicas" 'BEGIN { printf "%.2f", price * n }')"
  printf '\n    %sThis starts %s GPU replica(s) on %s: about $%s/hour until you run scripts/cleanup.sh.%s\n' \
    "$C_BOLD" "$replicas" "$VM_SIZE" "$total" "$C_OFF"
  [[ -t 0 ]] || die "Not running interactively; re-run with --yes to confirm."
  read -r -p "    Continue? [y/N] " answer
  [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]] || die "Aborted; nothing was changed."
}

ensure_resource_group() {
  log "Resource group $RG"
  if [[ "$(az group exists --name "$RG" --only-show-errors)" == true ]]; then
    ok "Exists"
  else
    run az group create --name "$RG" --location "$LOCATION" -o none
    applied "Created in $LOCATION"
  fi
}

ensure_aimanager() {
  log "AIManager $AIM_NAME"
  local state
  state="$(az aimanager show -g "$RG" -n "$AIM_NAME" --query properties.provisioningState -o tsv --only-show-errors 2>/dev/null || true)"
  if [[ "$state" == Succeeded ]]; then
    ok "Exists"
    return
  fi
  # Also grants the caller "Azure AIManager Contributor" and "Azure AIManager RBAC Reader" on the
  # AIManager; neither role includes the inference data action.
  run az aimanager create -g "$RG" -n "$AIM_NAME" --location "$LOCATION" -o none
  applied "Created"
}

ensure_namespace() {
  local ns="$1" state
  state="$(az aimanager namespace show -g "$RG" -m "$AIM_NAME" -n "$ns" \
    --query properties.provisioningState -o tsv --only-show-errors 2>/dev/null || true)"
  if [[ "$state" == Succeeded ]]; then
    ok "Namespace $ns exists"
    return
  fi
  # Like `az aimanager create`, this grants the caller Contributor + RBAC Reader (no inference) on the namespace.
  run az aimanager namespace create -g "$RG" -m "$AIM_NAME" -n "$ns" -o none
  applied "Namespace $ns created"
}

configure_rbac() {
  log "Azure RBAC for $DEMO_USER"
  local ns1_scope ns2_scope ids
  ns1_scope="$(ns_scope "$NS1")"
  ns2_scope="$(ns_scope "$NS2")"

  if [[ -n "$(inference_assignments_at "$DEMO_USER_OID" "$ns1_scope")" ]]; then
    ok "'$INFERENCE_ROLE' is already assigned on $NS1"
  else
    run az role assignment create --assignee-object-id "$DEMO_USER_OID" --assignee-principal-type "$DEMO_USER_TYPE" \
      --role "$INFERENCE_ROLE_ID" --scope "$ns1_scope" -o none --only-show-errors
    applied "Assigned '$INFERENCE_ROLE' on $ns1_scope"
  fi

  ids="$(inference_assignments_at "$DEMO_USER_OID" "$ns2_scope")"
  if [[ -n "$ids" ]]; then
    # shellcheck disable=SC2086 # one assignment ID per word
    run az role assignment delete --ids $ids --only-show-errors
    applied "Removed '$INFERENCE_ROLE' from $NS2"
  else
    ok "'$INFERENCE_ROLE' is not assigned on $NS2"
  fi

  # A role inherited from the AIManager, resource group, subscription or a group membership could still
  # allow inference on ns2. ARM's effective-permission API for the signed-in account covers all of those.
  local scope="$ns2_scope" allowed
  if [[ "$DRY_RUN" == true ]]; then scope="/subscriptions/$SUB"; fi
  if ! allowed="$(signed_in_can_infer "$scope")"; then
    warn "Could not read the effective permissions on $NS2; skipping the inherited-access check."
  elif [[ "$allowed" == true ]]; then
    warn "Azure RBAC still allows $DEMO_USER '$INFERENCE_DATA_ACTION' on $NS2 (inherited or group assignment), so $NS2/$MD2 will not return 401/403. Inspect: az role assignment list --assignee-object-id $DEMO_USER_OID --scope $ns2_scope --include-inherited --include-groups -o table"
  else
    ok "Effective Azure RBAC gives $DEMO_USER no inference permission on $NS2"
  fi
}

namespace_endpoint() {
  # list-accesskeys also returns the namespace API keys; only the endpoint is used.
  az aimanager namespace list-accesskeys -g "$RG" -m "$AIM_NAME" -n "$1" --query endpoint -o tsv --only-show-errors 2>/dev/null
}

# Returns 1 (without writing) while a namespace endpoint isn't available yet.
write_demo_config() {
  log "Writing $DEMO_CONFIG"
  if [[ "$DRY_RUN" == true ]]; then
    info "Skipped in dry-run mode (the namespace endpoints don't exist yet)."
    return 0
  fi
  local ep1 ep2
  ep1="$(namespace_endpoint "$NS1" || true)"
  ep2="$(namespace_endpoint "$NS2" || true)"
  if [[ -z "$ep1" || -z "$ep2" ]]; then
    warn "The namespace gateway endpoints aren't available yet (az aimanager namespace list-accesskeys)."
    return 1
  fi
  jq -n \
    --arg sub "$SUB" --arg tenant "$(az account show --query tenantId -o tsv)" --arg rg "$RG" \
    --arg location "$LOCATION" --arg aim "$AIM_NAME" --arg vmSize "$VM_SIZE" --arg modelId "$MODEL_ID" \
    --arg upn "$DEMO_USER" --arg oid "$DEMO_USER_OID" --arg role "$INFERENCE_ROLE" --arg roleId "$INFERENCE_ROLE_ID" \
    --arg ns1 "$NS1" --arg md1 "$MD1" --arg ep1 "$ep1" --arg scope1 "$(ns_scope "$NS1")" \
    --arg ns2 "$NS2" --arg md2 "$MD2" --arg ep2 "$ep2" --arg scope2 "$(ns_scope "$NS2")" \
    '{
      subscriptionId: $sub, tenantId: $tenant, resourceGroup: $rg, location: $location, aiManager: $aim,
      vmSize: $vmSize, modelId: $modelId,
      demoUser: {userPrincipalName: $upn, objectId: $oid},
      inferenceRole: {name: $role, id: $roleId},
      targets: [
        {namespace: $ns1, modelDeployment: $md1, endpoint: $ep1, scope: $scope1, expected: "allow"},
        {namespace: $ns2, modelDeployment: $md2, endpoint: $ep2, scope: $scope2, expected: "deny"}
      ]
    }' >"$DEMO_CONFIG"
  ok "$NS1/$MD1 -> $(chat_url "$ep1")  (expected: allowed)"
  ok "$NS2/$MD2 -> $(chat_url "$ep2")  (expected: 401/403)"
}

ensure_model_deployment() {
  local ns="$1" md="$2" state
  state="$(deployment_state "$ns" "$md")"
  case "$state" in
    Succeeded | Creating | Updating) ok "$ns/$md exists ($state)"; return ;;
    "") ;;
    *) warn "$ns/$md is '$state'; submitting it again" ;;
  esac
  run az aimanager namespace modeldeployment create -g "$RG" -m "$AIM_NAME" --namespace "$ns" -n "$md" \
    --model-resource-id "$MODEL_RESOURCE_ID" --vm-size "$VM_SIZE" --replicas "$REPLICAS" --no-wait -o none
  applied "$ns/$md submitted: $MODEL_ID on $VM_SIZE, $REPLICAS replica(s)"
}

wait_until_serving() {
  local ns="$1" md="$2" deadline=$((SECONDS + WAIT_TIMEOUT_MIN * 60)) status state ready desired
  while true; do
    status="$(az aimanager namespace modeldeployment show -g "$RG" -m "$AIM_NAME" --namespace "$ns" -n "$md" \
      --query '{state: properties.provisioningState, ready: properties.status.currentReplicas, desired: properties.status.desiredReplicas}' \
      -o json --only-show-errors 2>/dev/null || echo '{}')"
    state="$(jq -r '.state // "Unknown"' <<<"$status")"
    ready="$(jq -r '.ready // 0' <<<"$status")"
    desired="$(jq -r '.desired // "?"' <<<"$status")"
    if [[ "$state" == Succeeded && "$ready" -ge 1 ]]; then
      ok "$ns/$md is serving ($ready/$desired replicas ready)"
      return
    fi
    case "$state" in
      Failed | Canceled)
        die "$ns/$md provisioning ended in '$state'. Details: az aimanager namespace modeldeployment show -g $RG -m $AIM_NAME --namespace $ns -n $md" ;;
    esac
    ((SECONDS < deadline)) ||
      die "Timed out after $WAIT_TIMEOUT_MIN minutes waiting for $ns/$md (state=$state, ready=$ready/$desired). Re-run this script to keep waiting."
    info "$(date +%H:%M:%S) $ns/$md: $state, $ready/$desired replicas ready; checking again in ${POLL_SECONDS}s"
    sleep "$POLL_SECONDS"
  done
}

main() {
  preflight
  check_capacity
  confirm_cost
  ensure_resource_group
  ensure_aimanager
  log "Namespaces"
  ensure_namespace "$NS1"
  ensure_namespace "$NS2"
  # Assign the role before the slow model deployments so it has propagated by the time they serve:
  # the gateway caches authorization decisions, denials included, for about 5 minutes.
  configure_rbac
  local config_written=true
  write_demo_config || config_written=false
  log "Model deployments"
  ensure_model_deployment "$NS1" "$MD1"
  ensure_model_deployment "$NS2" "$MD2"
  if [[ "$DRY_RUN" == true ]]; then
    log "Dry run complete; nothing was changed."
    return
  fi

  log "Waiting for the model deployments (GPU node provisioning and model download take a while)"
  wait_until_serving "$NS1" "$MD1"
  wait_until_serving "$NS2" "$MD2"
  if [[ "$config_written" != true ]]; then
    write_demo_config || die "Could not read the namespace gateway endpoints; re-run this script to retry."
  fi

  if [[ "$SKIP_VERIFY" != true ]]; then
    "$SCRIPT_DIR/verify.sh" ||
      warn "Verification hasn't passed yet. Role assignments can take a few minutes to apply; re-run scripts/verify.sh shortly."
  fi
  log "Done"
  info "Streamlit client:             streamlit run client/app.py"
  info "Clean up (stops GPU billing): scripts/cleanup.sh"
}

main
