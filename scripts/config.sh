# shellcheck shell=bash
# Shared settings and helpers for the AIManager namespace-RBAC demo scripts.
# The scripts act as the `az login` account, in the Azure CLI's default subscription (`az account set -s <id>`).
# Every other setting can be overridden from the environment, e.g. `AIM_NAME=aim-demo2 ./scripts/setup.sh`.

# Filled in by use_default_subscription. A SUB exported in the shell (the testing guide exports one) is ignored.
SUB=""
SUB_NAME=""
RG="${RG:-charlilirg}"
LOCATION="${LOCATION:-italynorth}"
AIM_NAME="${AIM_NAME:-charlili-aim}"
NS1="${NS1:-ns1}"
MD1="${MD1:-md1}"
NS2="${NS2:-ns2}"
MD2="${MD2:-md2}"
VM_SIZE="${VM_SIZE:-Standard_ND96amsr_A100_v4}"
MODEL_ID="${MODEL_ID:-google/gemma-4-E4B-it}"
REPLICAS="${REPLICAS:-1}"

# The `az login` account, filled in by use_signed_in_account. It gets "Azure AIManager Inference User" on $NS1
# and must not be able to call $NS2. Values exported in the shell are ignored.
DEMO_USER=""
DEMO_USER_OID=""
DEMO_USER_TYPE=""

INFERENCE_ROLE="Azure AIManager Inference User"
INFERENCE_DATA_ACTION="Microsoft.ContainerService/aiManagers/namespaces/inference/action"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEMO_CONFIG="${DEMO_CONFIG:-$REPO_ROOT/demo-config.json}"
DRY_RUN="${DRY_RUN:-false}"

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  C_BOLD=$'\033[1m' C_BLUE=$'\033[1;34m' C_GREEN=$'\033[32m' C_YELLOW=$'\033[33m' C_RED=$'\033[1;31m' C_OFF=$'\033[0m'
else
  C_BOLD='' C_BLUE='' C_GREEN='' C_YELLOW='' C_RED='' C_OFF=''
fi

log()  { printf '\n%s==> %s%s\n' "$C_BLUE" "$*" "$C_OFF"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %s✔ %s%s\n' "$C_GREEN" "$*" "$C_OFF"; }
warn() { printf '    %s⚠ %s%s\n' "$C_YELLOW" "$*" "$C_OFF" >&2; }
die()  { printf '\n%s✘ %s%s\n' "$C_RED" "$*" "$C_OFF" >&2; exit 1; }

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

require() {
  local cmd
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null 2>&1 || die "'$cmd' is required but was not found in PATH."
  done
}

# Prints a command so it can be copied into a shell.
quote_cmd() {
  local arg out=""
  for arg in "$@"; do
    if [[ "$arg" =~ ^[A-Za-z0-9_./:=@,+-]+$ ]]; then
      out+="$arg "
    else
      out+="'${arg//\'/\'\\\'\'}' "
    fi
  done
  printf '%s' "${out% }"
}

# Runs a command that changes Azure, or only prints it when DRY_RUN=true.
run() {
  if [[ "$DRY_RUN" == true ]]; then
    printf '    %s[dry-run]%s %s\n' "$C_BOLD" "$C_OFF" "$(quote_cmd "$@")"
    return 0
  fi
  "$@"
}

# Success message for a change made through `run` (suppressed in dry-run mode).
applied() {
  if [[ "$DRY_RUN" != true ]]; then ok "$*"; fi
}

use_default_subscription() {
  local account
  account="$(az account show --query '{id: id, name: name}' -o json --only-show-errors 2>/dev/null)" ||
    die "Not signed in to the Azure CLI. Run 'az login', then 'az account set -s <subscription-id>'."
  SUB="$(jq -r '.id' <<<"$account")"
  SUB_NAME="$(jq -r '.name' <<<"$account")"
}

# Stops if demo-config.json was written for another subscription, so that switching the Azure CLI default
# doesn't make setup.sh or cleanup.sh lose track of a demo that is still running (and billing) there.
check_demo_config_subscription() {
  [[ -f "$DEMO_CONFIG" ]] || return 0
  local recorded
  recorded="$(jq -r '.subscriptionId // empty' "$DEMO_CONFIG")"
  if [[ -n "$recorded" && "$(lower "$recorded")" != "$(lower "$SUB")" ]]; then
    die "$DEMO_CONFIG is for a demo in subscription $recorded, but the Azure CLI default is '$SUB_NAME' ($SUB). Run 'az account set -s $recorded' to work with that demo, or delete $DEMO_CONFIG if it no longer exists."
  fi
}

aim_scope() { printf '/subscriptions/%s/resourceGroups/%s/providers/Microsoft.ContainerService/aiManagers/%s' "$SUB" "$RG" "$AIM_NAME"; }
ns_scope() { printf '%s/namespaces/%s' "$(aim_scope)" "$1"; }

# OpenAI-compatible chat URL for a namespace gateway endpoint (list-accesskeys returns the base URL ending in /v1).
chat_url() {
  local base="${1%/}"
  if [[ "$base" == */chat/completions ]]; then printf '%s' "$base"; else printf '%s/chat/completions' "$base"; fi
}

# Decodes the payload of a JWT read from stdin. No signature validation: display/identity only.
jwt_claims() {
  jq -rR 'split(".")[1] | gsub("-"; "+") | gsub("_"; "/") | . + "=="[0:(4 - length % 4) % 4] | @base64d | fromjson'
}

# The object ID comes from the access token, like the testing guide's PRINCIPAL_ID, because Microsoft Graph
# lookups can be blocked by conditional access (AADSTS530084).
# shellcheck disable=SC2034 # the DEMO_USER* variables are read by the scripts that source this file
use_signed_in_account() {
  local claims
  claims="$(az account get-access-token --query accessToken -o tsv --only-show-errors | jwt_claims)"
  DEMO_USER="$(jq -r '.upn // .unique_name // .appid // .oid' <<<"$claims")"
  DEMO_USER_OID="$(jq -r '.oid' <<<"$claims")"
  # Same rule as the aimanager CLI extension: app-only tokens (service principals, managed identities) have idtyp "app".
  DEMO_USER_TYPE="$(jq -r 'if .idtyp == "app" then "ServicePrincipal" else "User" end' <<<"$claims")"
}

inference_role_id() {
  az role definition list --name "$INFERENCE_ROLE" --scope "/subscriptions/$SUB" --query '[0].name' -o tsv --only-show-errors
}

# IDs of the "Azure AIManager Inference User" assignments of object ID $1 made exactly at scope $2
# (needs INFERENCE_ROLE_ID). Prints nothing if there are none or the scope doesn't exist yet.
inference_assignments_at() {
  az role assignment list --assignee-object-id "$1" --role "$INFERENCE_ROLE_ID" --scope "$2" \
    --fill-principal-name false --query '[].id' -o tsv --only-show-errors 2>/dev/null || true
}

# Prints "true"/"false": whether the signed-in identity's effective Azure RBAC permissions at scope $1
# allow the inference data action. Uses ARM's own evaluation, so wildcards, notDataActions, inherited and
# group-based role assignments are all taken into account. Fails if the permissions can't be read.
signed_in_can_infer() {
  local permissions
  permissions="$(az rest --method get --only-show-errors -o json 2>/dev/null \
    --url "https://management.azure.com$1/providers/Microsoft.Authorization/permissions?api-version=2022-04-01")" || return 1
  jq --arg action "$INFERENCE_DATA_ACTION" '
    def matches($pattern):
      ($action | ascii_downcase) | test("^" + ($pattern | ascii_downcase | gsub("\\."; "\\.") | gsub("\\*"; ".*")) + "$");
    any(.value[]; any(.dataActions[]?; matches(.)) and all(.notDataActions[]?; matches(.) | not))' <<<"$permissions"
}
