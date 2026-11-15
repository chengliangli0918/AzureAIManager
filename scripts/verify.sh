#!/usr/bin/env bash
# Calls every model deployment in demo-config.json the way the testing guide does for Azure RBAC
# (an Entra token from `az account get-access-token`, sent as a Bearer token) and checks the outcome:
# targets marked "allow" must answer with HTTP 2xx, targets marked "deny" must be refused with 401/403.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/config.sh
source "$SCRIPT_DIR/config.sh"

PROMPT="${PROMPT:-What is Kubernetes? Answer in one sentence.}"
RBAC_WAIT_SECONDS="${RBAC_WAIT_SECONDS:-600}"

require az jq curl
[[ -f "$DEMO_CONFIG" ]] || die "$DEMO_CONFIG not found. Run scripts/setup.sh first."
SUB="$(jq -r '.subscriptionId' "$DEMO_CONFIG")"
# The az login account that setup.sh gave the role to.
DEMO_USER="$(jq -r '.demoUser.userPrincipalName // empty' "$DEMO_CONFIG")"

log "Getting an Entra token for the signed-in user (az account get-access-token)"
TOKEN="$(az account get-access-token --subscription "$SUB" --query accessToken -o tsv --only-show-errors)"
CLAIMS="$(jwt_claims <<<"$TOKEN")"
CALLER="$(jq -r '.upn // .unique_name // .appid // .oid' <<<"$CLAIMS")"
ok "Caller: $CALLER (object ID $(jq -r '.oid' <<<"$CLAIMS"), audience $(jq -r '.aud' <<<"$CLAIMS"))"
CALLER_IS_DEMO_USER=false
if [[ -z "$DEMO_USER" || "$(lower "$CALLER")" == "$(lower "$DEMO_USER")" ]]; then
  CALLER_IS_DEMO_USER=true
else
  warn "setup.sh gave the role to $DEMO_USER, but az is signed in as $CALLER; these results reflect $CALLER's permissions."
fi

# A call made before a new role assignment has propagated is denied, and the gateway caches that
# denial for about 5 minutes, so wait until ARM reports the permission before calling an "allow" target.
wait_for_permission() {
  local scope="$1" label="$2" deadline=$((SECONDS + RBAC_WAIT_SECONDS))
  until [[ "$(signed_in_can_infer "$scope")" == true ]]; do
    if ((SECONDS >= deadline)); then
      warn "ARM still doesn't report inference permission on $label after ${RBAC_WAIT_SECONDS}s; calling anyway."
      return 0
    fi
    info "Waiting for the role assignment on $label to propagate..."
    sleep 20
  done
  ok "Azure RBAC allows inference on $label"
}

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

failures=0
summary=()
allowed=()
denied=()
count="$(jq '.targets | length' "$DEMO_CONFIG")"
((count > 0)) || die "$DEMO_CONFIG has no targets."
for ((i = 0; i < count; i++)); do
  target="$(jq -c ".targets[$i]" "$DEMO_CONFIG")"
  ns="$(jq -r '.namespace' <<<"$target")"
  md="$(jq -r '.modelDeployment' <<<"$target")"
  expected="$(jq -r '.expected' <<<"$target")"
  label="$ns/$md"
  url="$(chat_url "$(jq -r '.endpoint' <<<"$target")")"

  if [[ "$expected" == allow ]]; then
    log "$label (expected: allowed)"
    scope="$(jq -r '.scope // empty' <<<"$target")"
    if [[ "$CALLER_IS_DEMO_USER" == true && -n "$scope" ]]; then wait_for_permission "$scope" "$ns"; fi
  else
    log "$label (expected: denied with 401/403)"
  fi

  # Reasoning models think before they answer, so leave room for both.
  body="$(jq -nc --arg model "$md" --arg prompt "$PROMPT" \
    '{model: $model, messages: [{role: "user", content: $prompt}], max_tokens: 1024}')"
  info "POST $url"
  : >"$TMP_DIR/body"
  : >"$TMP_DIR/headers"
  # The Authorization header is read from stdin so the token never shows up in the process list.
  code="$(curl -sS -o "$TMP_DIR/body" -D "$TMP_DIR/headers" -w '%{http_code}' --max-time 120 -X POST "$url" \
    -H 'Content-Type: application/json' -H @- --data-binary "$body" <<<"Authorization: Bearer $TOKEN")" || code=000

  case "$code" in
    2??) outcome=allowed ;;
    401 | 403) outcome=denied ;;
    *) outcome=error ;;
  esac
  if [[ "$expected" == allow && "$outcome" == allowed ]] || [[ "$expected" == deny && "$outcome" == denied ]]; then
    ok "HTTP $code: $outcome, as expected"
    result=PASS
  else
    if [[ "$expected" == allow ]]; then want="allowed"; else want="denied (401/403)"; fi
    warn "HTTP $code: $outcome, but this target should be $want"
    result=FAIL
    failures=$((failures + 1))
  fi
  if [[ "$outcome" == allowed ]]; then
    info "Answer: $(jq -r '.choices[0].message.content // empty' "$TMP_DIR/body" 2>/dev/null | tr '\n' ' ' | cut -c1-300)"
    allowed+=("$label")
  else
    info "Body: $(tr '\n' ' ' <"$TMP_DIR/body" | cut -c1-300)"
    www_auth="$(grep -i '^www-authenticate:' "$TMP_DIR/headers" | tr -d '\r' || true)"
    if [[ -n "$www_auth" ]]; then info "$www_auth"; fi
    if [[ "$outcome" == denied ]]; then denied+=("$label"); fi
  fi
  summary+=("$(printf '%-16s %-9s %-5s %s' "$label" "$expected" "$code" "$result")")
done

log "Summary for $CALLER"
printf '    %-16s %-9s %-5s %s\n' TARGET EXPECTED HTTP RESULT
for line in "${summary[@]}"; do printf '    %s\n' "$line"; done
if ((failures > 0)); then
  die "$failures check(s) failed. A just-created role assignment can take a few minutes to apply, and the gateway caches decisions for ~5 minutes."
fi
ok "Namespace-scoped RBAC works: $CALLER can call ${allowed[*]:-nothing} and is refused on ${denied[*]:-nothing}"
