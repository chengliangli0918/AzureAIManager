"""Streamlit client for the AIManager namespace-scoped Inference RBAC demo.

The signed-in Entra user calls the OpenAI-compatible inference endpoints of two AIManager namespaces with
the same bearer token. "Azure AIManager Inference User" on ns1 lets the ns1/md1 call through; with no
role on ns2, the gateway refuses ns2/md2 with 401/403.

Run from the repository root:  streamlit run client/app.py
"""

from __future__ import annotations

import json
import os
import shlex
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

import aim_client as aim

CONFIG_PATH = Path(os.environ.get("DEMO_CONFIG", Path(__file__).resolve().parent.parent / "demo-config.json"))
DEFAULT_PROMPT = "What is Kubernetes? Answer in one sentence."
HISTORY_LIMIT = 50

st.set_page_config(page_title="AIManager namespace-scoped Inference RBAC", page_icon="🔐", layout="wide")


def load_state() -> None:
    config = aim.load_config(CONFIG_PATH) if CONFIG_PATH.exists() else aim.default_config()
    st.session_state.config = config
    st.session_state.tenant_id = config.tenant_id
    for i, target in enumerate(config.targets):
        st.session_state[f"endpoint_{i}"] = target.endpoint
    # The namespaces and model deployments may differ in the reloaded config.
    for key in [k for k in st.session_state if k == "namespace" or k.startswith("md_")]:
        del st.session_state[key]
    st.session_state.history = []
    st.session_state.rbac = None
    st.session_state.notice = None


def clear_history() -> None:
    st.session_state.history = []


@st.cache_resource(show_spinner=False)
def credential_for(kind: str, tenant_id: str, login_hint: str):
    return aim.make_credential(kind, tenant_id, login_hint)


def get_token(scope: str) -> str:
    """Entra access token for scope, cached in the session until 5 minutes before it expires."""
    kind, tenant_id = st.session_state.auth_kind, st.session_state.tenant_id.strip()
    key = f"token|{kind}|{tenant_id}|{scope}"
    token = st.session_state.get(key)
    if token is None or token.expires_on - time.time() < 300:
        token = credential_for(kind, tenant_id, st.session_state.config.demo_user).get_token(scope)
        st.session_state[key] = token
    return token.token


def discover_endpoints() -> None:
    config = st.session_state.config
    try:
        arm_token = get_token(aim.ARM_SCOPE)
        for i, target in enumerate(config.targets):
            st.session_state[f"endpoint_{i}"] = aim.discover_endpoint(config, target, arm_token)
        st.session_state.notice = ("success", "Endpoints discovered from Azure.")
    except Exception as exc:  # surfaced in the sidebar
        st.session_state.notice = ("error", f"Endpoint discovery failed: {exc}")


def check_rbac(config: aim.DemoConfig, principal_id: str) -> dict[str, dict]:
    arm_token = get_token(aim.ARM_SCOPE)
    report = {}
    for target in config.targets:
        try:
            report[target.scope] = {
                "assignments": aim.role_assignments_at(target.scope, principal_id, arm_token),
                "allowed": aim.signed_in_user_can_infer(target.scope, arm_token),
            }
        except Exception as exc:  # e.g. the namespace doesn't exist yet
            report[target.scope] = {"error": str(exc)}
    return report


def expectation(target: aim.Target) -> str:
    if target.expected == "allow":
        return f"Expected: ✅ allowed ({aim.INFERENCE_ROLE} on `{target.namespace}`)"
    return f"Expected: ⛔ 401/403 (no inference role on `{target.namespace}`)"


def render_rbac(target: aim.Target, entry: dict | None) -> None:
    st.markdown(f"#### Namespace `{target.namespace}`")
    st.caption(expectation(target))
    if entry is None:
        return
    if "error" in entry:
        st.error(entry["error"])
        return
    rows = [
        {
            "Role": a.role,
            "Assigned at": aim.scope_label(a.scope, target.scope),
            "Via": a.principal_type,
            "Allows inference": "✅" if a.grants_inference else "—",
        }
        for a in entry["assignments"]
    ]
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")
    else:
        st.caption("No role assignments apply to this user here.")
    allowed, wanted = entry["allowed"], target.expected == "allow"
    verdict = "allows" if allowed else "does not allow"
    message = f"Effective Azure RBAC **{verdict}** `namespaces/inference/action` here"
    if allowed == wanted:
        st.success(f"{message}, as intended.")
    elif wanted:
        st.warning(f"{message} yet. A new role assignment can take a few minutes to show up.")
    else:
        st.error(f"{message}, so this call will not be refused. Remove the inherited or group assignment listed above.")


def verdict(result: aim.InferenceResult) -> str:
    ns = result.target.namespace
    if result.as_expected:
        if result.outcome == "allowed":
            return f"Allowed: the caller holds {aim.INFERENCE_ROLE} on `{ns}`."
        return f"Refused with {result.status}, as expected: the caller has no inference role on `{ns}`."
    if result.outcome == "allowed":
        return (
            f"Expected 401/403, but the call went through: a role inherited from a parent scope or a group "
            f"still allows inference on `{ns}` (see ②)."
        )
    if result.status == 401:
        return (
            "401: the gateway rejected the token itself. Check the token scope "
            f"(default `{aim.INFERENCE_SCOPE}`) and the tenant."
        )
    if result.status == 403:
        return (
            f"403: Azure RBAC denied inference on `{ns}`. A new role assignment can take a few minutes to apply, "
            "and the gateway caches decisions (denials too) for about 5 minutes."
        )
    problem = result.error or f"HTTP {result.status} {result.reason}"
    return f"No usable answer ({problem}). Check the endpoint and that the model deployment is ready."


def markdown_text(text: str) -> str:
    # Streamlit markdown treats "$...$" as LaTeX math, which garbles answers like "costs $5 to $10".
    return text.replace("$", "\\$")


def error_text(result: aim.InferenceResult) -> str:
    # A 403 from the namespace gateway means the caller lacks the inference data action on the namespace scope;
    # name that scope so it's clear where the role is missing. The gateway's own text stays under Request and
    # response.
    if result.status == 403:
        return (
            f"User does not have Inference access to the resource '{result.target.scope}'. "
            "Update role assignment to allow access."
        )
    detail = result.error_detail
    return detail if len(detail) <= 500 else detail[:500] + "…"


def answer_details(result: aim.InferenceResult) -> str:
    details = []
    if not result.reply and result.reasoning:
        details.append("reasoning only: the model stopped before writing its answer")
    if result.finish_reason:
        details.append(f"finish_reason `{result.finish_reason}`")
    if result.completion_tokens is not None:
        reasoning = f" ({result.reasoning_tokens} reasoning)" if result.reasoning_tokens else ""
        details.append(f"{result.completion_tokens} completion tokens{reasoning}")
    if result.finish_reason == "length":
        details.append("cut off at max_tokens; raise it in the sidebar")
    return " · ".join(details)


def render_history_entry(result: aim.InferenceResult) -> None:
    target = result.target
    status = f"{result.status} {result.reason}".strip() if result.status else "No response"
    icon = {"allowed": "✅", "denied": "⛔"}.get(result.outcome, "⚠️")
    with st.container(border=True):
        st.markdown(f"{icon} **{status}** · `{target.namespace}` / `{target.model_deployment}`")
        st.caption(f"{datetime.fromtimestamp(result.sent_at):%H:%M:%S} · {result.elapsed_ms:,.0f} ms")
        with st.chat_message("user"):
            st.markdown(markdown_text(result.prompt))
        with st.chat_message("assistant", avatar="🤖" if result.outcome == "allowed" else "🚫"):
            if result.outcome == "allowed":
                st.markdown("**Answer**")
                answer = result.reply or result.reasoning
                if answer:
                    st.markdown(markdown_text(answer))
                else:
                    st.caption("The response has no answer text; the raw body is under Request and response.")
                if details := answer_details(result):
                    st.caption(details)
                if result.reply and result.reasoning:
                    with st.expander("Model reasoning"):
                        st.markdown(markdown_text(result.reasoning))
            else:
                st.error(f"**{status}**: {error_text(result)}")
                if "www-authenticate" in result.headers:
                    st.caption(f"WWW-Authenticate: `{result.headers['www-authenticate']}`")
        if result.as_expected:
            st.caption(f"✔ {verdict(result)}")
        else:
            st.warning(verdict(result))
        with st.expander("Request and response"):
            st.code(f"POST {result.url}\nmodel: {target.model_deployment}", language="text", wrap_lines=True)
            if result.headers:
                st.json(result.headers)
            if isinstance(result.body, (dict, list)):
                st.json(result.body)
            elif result.body or result.error:
                st.code(str(result.body or result.error), language="text", wrap_lines=True)


def curl_commands(targets: list[aim.Target], prompt: str) -> str:
    lines = ["TOKEN=$(az account get-access-token --query accessToken -o tsv)"]
    for t in targets:
        body = json.dumps({"model": t.model_deployment, "messages": [{"role": "user", "content": prompt}]})
        lines += [
            "",
            f"# {t.label}: expected {'200' if t.expected == 'allow' else '401/403'}",
            f"curl -sS -X POST {shlex.quote(aim.chat_completions_url(t.endpoint))} \\",
            "  -H 'Content-Type: application/json' -H \"Authorization: Bearer $TOKEN\" \\",
            f"  -d {shlex.quote(body)} -w '\\nHTTP %{{http_code}}\\n'",
        ]
    return "\n".join(lines)


if "config" not in st.session_state:
    load_state()
config: aim.DemoConfig = st.session_state.config
allow_labels = ", ".join(f"`{t.label}`" for t in config.targets if t.expected == "allow")
deny_labels = ", ".join(f"`{t.label}`" for t in config.targets if t.expected != "allow")

with st.sidebar:
    st.header("⚙️ Demo setup")
    if config.source:
        st.caption(f"Loaded from `{config.source}`")
    else:
        st.warning(
            f"`{CONFIG_PATH.name}` not found. Run `scripts/setup.sh`, or discover the endpoints in the "
            "Azure CLI's default subscription."
        )
        if not config.subscription_id:
            st.error(
                "Couldn't read the Azure CLI default subscription. "
                "Run `az login` and `az account set -s <id>`, then reload."
            )
    st.markdown(
        f"**Subscription** `{config.subscription_id or 'unknown'}`  \n"
        f"**Resource group** `{config.resource_group}`  \n**AIManager** `{config.ai_manager}`"
    )
    for i, target in enumerate(config.targets):
        st.text_input(
            f"{target.label} endpoint", key=f"endpoint_{i}",
            placeholder="https://<namespace>.<aimanager>.<region>.aksapp.io/v1",
        )
        target.endpoint = st.session_state[f"endpoint_{i}"]
    st.button("Discover endpoints from Azure", on_click=discover_endpoints, width="stretch")
    st.button(f"Reload {CONFIG_PATH.name}", on_click=load_state, width="stretch")
    if st.session_state.notice:
        level, message = st.session_state.notice
        getattr(st, level)(message)

    st.header("🔑 Sign-in")
    st.radio("Credential", [aim.AUTH_AZURE_CLI, aim.AUTH_BROWSER], key="auth_kind")
    st.text_input("Tenant ID", key="tenant_id", help="Leave empty to use the Azure CLI's default tenant.")
    st.text_input(
        "Inference token scope", value=aim.INFERENCE_SCOPE, key="inference_scope",
        help="Same audience as `az account get-access-token`, which the AIManager gateway accepts.",
    )
    st.slider(
        "max_tokens", min_value=64, max_value=4096, value=1024, step=64, key="max_tokens",
        help="Reasoning models such as gemma-4 think before they answer; too low a limit cuts the answer off.",
    )

st.title("🔐 AIManager namespace-scoped Inference RBAC")
demo_user_label = f"`{config.demo_user}`" if config.demo_user else "the signed-in account"
st.markdown(
    f"One Entra identity, one token, two namespaces. **{aim.INFERENCE_ROLE}** is assigned to "
    f"{demo_user_label} on {allow_labels} only, so the AIManager gateway serves inference there and "
    f"refuses {deny_labels} with **401/403**."
)
with st.expander("How it works"):
    st.graphviz_chart(
        """
        digraph {
          rankdir=LR; bgcolor="transparent";
          node [shape=box, style="rounded,filled", fillcolor="#f5f7fb", fontname="Helvetica", fontsize=11];
          edge [fontname="Helvetica", fontsize=10];
          user [label="Signed-in user\\nEntra access token"];
          gw [label="AIManager gateway\\nAzure RBAC check:\\nnamespaces/inference/action"];
          ns1 [label="ns1 / md1\\nAzure AIManager Inference User", fillcolor="#e3f6e8"];
          ns2 [label="ns2 / md2\\nno inference role", fillcolor="#fde8e8"];
          user -> gw [label="Bearer token"];
          gw -> ns1 [label="200 OK", color="#2e7d32", fontcolor="#2e7d32"];
          gw -> ns2 [label="403 Forbidden", color="#c62828", fontcolor="#c62828", style=dashed];
        }
        """
    )
    st.markdown(
        "1. The client gets an Entra access token (the same one `az account get-access-token` returns) and sends "
        "it as `Authorization: Bearer` to each namespace's OpenAI-compatible endpoint.\n"
        "2. The gateway validates the token, takes the namespace from the host name and the caller from the "
        f"token's `oid`, and asks Azure RBAC whether the caller may perform `{aim.INFERENCE_DATA_ACTION}` on "
        "that namespace.\n"
        "3. Allowed: the request goes to the model deployment named in `model`. Denied: `403 Forbidden` "
        "(`401` when the token itself is missing or invalid). Decisions are cached for about 5 minutes."
    )

st.subheader("① Who is calling?")
try:
    token = get_token(st.session_state.inference_scope)
except Exception as exc:
    st.error(f"Could not get an Entra token with {st.session_state.auth_kind}: {exc}")
    st.stop()
claims = aim.decode_jwt_claims(token)
caller = aim.identity_of(claims)
st.markdown(
    f"**User** `{caller}`  \n"
    f"**Object ID** `{claims.get('oid', '?')}`  \n"
    f"**Token audience** `{claims.get('aud', '?')}`"
)
st.caption(
    f"Tenant `{claims.get('tid', '?')}` · token expires at {datetime.fromtimestamp(claims.get('exp', 0)):%H:%M:%S}"
    " · the same token is sent to every namespace"
)
if not config.demo_user or caller.lower() == config.demo_user.lower():
    st.success(f"Signed in as the demo user **{caller}**.", icon="👤")
else:
    st.warning(
        f"Signed in as **{caller}**, but `scripts/setup.sh` gave the role to **{config.demo_user}**: "
        f"the results below reflect {caller}'s permissions."
    )

st.subheader("② What does Azure RBAC say?")
st.caption(
    "Role assignments that apply to the caller on each namespace (direct, inherited from the AIManager, resource "
    "group or subscription, or through a group), plus ARM's effective-permission verdict for the inference action."
)
if st.button("Check role assignments", key="check_rbac"):
    with st.spinner("Reading Azure role assignments…"):
        try:
            st.session_state.rbac = check_rbac(config, claims.get("oid", ""))
        except Exception as exc:
            st.error(f"Could not read role assignments: {exc}")
if st.session_state.rbac:
    for column, target in zip(st.columns(len(config.targets)), config.targets, strict=True):
        with column.container(border=True):
            render_rbac(target, st.session_state.rbac.get(target.scope))

st.subheader("③ Call a model deployment")
if not config.targets:
    st.warning(f"`{CONFIG_PATH.name}` lists no model deployments.")
    st.stop()
ns_column, md_column = st.columns(2)
namespace = ns_column.selectbox("Namespace", list(dict.fromkeys(t.namespace for t in config.targets)), key="namespace")
deployment = md_column.selectbox(
    "Model deployment", [t.model_deployment for t in config.targets if t.namespace == namespace], key=f"md_{namespace}"
)
target = next(t for t in config.targets if (t.namespace, t.model_deployment) == (namespace, deployment))
if target.endpoint.strip():
    st.caption(f"{expectation(target)} · POST `{aim.chat_completions_url(target.endpoint)}`")
else:
    st.warning(f"No endpoint for `{target.label}` yet: set it in the sidebar, or discover it from Azure.")
prompt = st.text_area("Prompt", value=DEFAULT_PROMPT, key="prompt", height=100)
actions = st.container(horizontal=True)
send = actions.button("Send", type="primary", icon="🚀", key="send", disabled=not target.endpoint.strip())
if send and not prompt.strip():
    st.warning("Enter a prompt first.")
elif send:
    with st.spinner(f"Calling {target.label}…"):
        st.session_state.history.insert(
            0, aim.call_inference(target, token, prompt, max_tokens=st.session_state.max_tokens)
        )
    del st.session_state.history[HISTORY_LIMIT:]
actions.button("Clear history", key="clear_history", on_click=clear_history, disabled=not st.session_state.history)
with st.expander("Equivalent curl command"):
    st.code(curl_commands([target], prompt), language="bash")

history: list[aim.InferenceResult] = st.session_state.history
st.markdown("#### Inference history")
if not history:
    st.caption("Replies and errors from your calls appear here, newest first.")
for result in history:
    render_history_entry(result)
