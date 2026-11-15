"""Logic behind the Streamlit demo client, kept free of Streamlit so it can be tested on its own."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from azure.core.credentials import TokenCredential
from azure.identity import AzureCliCredential, InteractiveBrowserCredential

ARM_ENDPOINT = "https://management.azure.com"
ARM_SCOPE = "https://management.azure.com/.default"
# Same audience as `az account get-access-token`, which the testing guide sends to the gateway for Azure
# RBAC inference. The gateway rejects tokens issued for other audiences with 401.
INFERENCE_SCOPE = "https://management.core.windows.net//.default"
AIMANAGER_API_VERSION = "2026-09-02-preview"
AUTHORIZATION_API_VERSION = "2022-04-01"
INFERENCE_ROLE = "Azure AIManager Inference User"
INFERENCE_DATA_ACTION = "Microsoft.ContainerService/aiManagers/namespaces/inference/action"

AUTH_AZURE_CLI = "Azure CLI (az login)"
AUTH_BROWSER = "Interactive browser sign-in"

SHOWN_RESPONSE_HEADERS = ("www-authenticate", "x-request-id", "content-type", "server", "date")


@dataclass
class Target:
    namespace: str
    model_deployment: str
    expected: str  # "allow" or "deny"
    endpoint: str = ""
    scope: str = ""

    @property
    def label(self) -> str:
        return f"{self.namespace}/{self.model_deployment}"


@dataclass
class DemoConfig:
    subscription_id: str
    resource_group: str
    ai_manager: str
    demo_user: str  # the account setup.sh gave the role to; empty means whoever signs in
    targets: list[Target]
    tenant_id: str = ""
    source: str = ""  # file the config was loaded from; empty for the built-in defaults

    def namespace_scope(self, namespace: str) -> str:
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.ContainerService/aiManagers/{self.ai_manager}/namespaces/{namespace}"
        )

    def _fill_scopes(self) -> DemoConfig:
        for target in self.targets:
            target.scope = target.scope or self.namespace_scope(target.namespace)
        return self


def cli_default_subscription() -> str:
    """ID of the Azure CLI's default subscription (chosen with `az account set -s <id>`); "" if unavailable."""
    az = shutil.which("az")
    if not az:
        return ""
    try:
        result = subprocess.run(
            [az, "account", "show", "--query", "id", "-o", "tsv"],
            capture_output=True, text=True, timeout=60, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def default_config() -> DemoConfig:
    """What scripts/config.sh would use, without endpoints.

    The subscription is the Azure CLI default and the demo user is whoever signs in. RG, AIM_NAME, NS1/MD1 and
    NS2/MD2 honour the same environment overrides as the scripts.
    """
    env = os.environ.get
    return DemoConfig(
        subscription_id=cli_default_subscription(),
        resource_group=env("RG", "charlilirg"),
        ai_manager=env("AIM_NAME", "charlili-aim"),
        demo_user="",
        targets=[
            Target(env("NS1", "ns1"), env("MD1", "md1"), "allow"),
            Target(env("NS2", "ns2"), env("MD2", "md2"), "deny"),
        ],
    )._fill_scopes()


def load_config(path: str | Path) -> DemoConfig:
    """Loads the demo-config.json written by scripts/setup.sh."""
    data = json.loads(Path(path).read_text())
    return DemoConfig(
        subscription_id=data["subscriptionId"],
        resource_group=data["resourceGroup"],
        ai_manager=data["aiManager"],
        demo_user=data.get("demoUser", {}).get("userPrincipalName", ""),
        tenant_id=data.get("tenantId", ""),
        targets=[
            Target(
                namespace=t["namespace"],
                model_deployment=t["modelDeployment"],
                expected=t.get("expected", "allow"),
                endpoint=t.get("endpoint", ""),
                scope=t.get("scope", ""),
            )
            for t in data["targets"]
        ],
        source=str(path),
    )._fill_scopes()


def make_credential(kind: str, tenant_id: str = "", login_hint: str = "") -> TokenCredential:
    if kind == AUTH_BROWSER:
        return InteractiveBrowserCredential(tenant_id=tenant_id or None, login_hint=login_hint or None)
    return AzureCliCredential(tenant_id=tenant_id or None)


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Decodes a JWT payload without validating it; for display only."""
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def identity_of(claims: dict[str, Any]) -> str:
    for claim in ("upn", "unique_name", "preferred_username", "appid", "oid"):
        if claims.get(claim):
            return str(claims[claim])
    return "unknown"


def chat_completions_url(endpoint: str) -> str:
    """OpenAI chat URL for a namespace gateway endpoint (list-accesskeys returns the base URL ending in /v1)."""
    base = endpoint.strip().rstrip("/")
    return base if base.endswith("/chat/completions") else f"{base}/chat/completions"


@dataclass
class InferenceResult:
    target: Target
    url: str
    status: int | None = None
    reason: str = ""
    elapsed_ms: float = 0.0
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    error: str = ""
    prompt: str = ""
    sent_at: float = field(default_factory=time.time)

    @property
    def outcome(self) -> str:
        """'allowed' for 2xx, 'denied' for 401/403, 'error' for anything else (including no response)."""
        if self.status is None:
            return "error"
        if 200 <= self.status < 300:
            return "allowed"
        return "denied" if self.status in (401, 403) else "error"

    @property
    def as_expected(self) -> bool:
        return self.outcome == {"allow": "allowed", "deny": "denied"}.get(self.target.expected)

    def _first_choice(self) -> dict[str, Any]:
        choices = self.body.get("choices") if isinstance(self.body, dict) else None
        return choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}

    @property
    def reply(self) -> str | None:
        """The answer text: choices[0].message.content (a string or a list of text parts), or completions-style
        choices[0].text."""
        choice = self._first_choice()
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            content = "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
        if not content:
            content = choice.get("text")
        return content if isinstance(content, str) and content.strip() else None

    @property
    def reasoning(self) -> str | None:
        """Reasoning text from vLLM reasoning parsers. It can be all a reasoning model returns when max_tokens runs out
        before it starts answering."""
        message = self._first_choice().get("message")
        text = (message.get("reasoning_content") or message.get("reasoning")) if isinstance(message, dict) else None
        return text if isinstance(text, str) and text.strip() else None

    @property
    def finish_reason(self) -> str:
        return str(self._first_choice().get("finish_reason") or "")

    @property
    def completion_tokens(self) -> int | None:
        usage = self.body.get("usage") if isinstance(self.body, dict) else None
        tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
        return tokens if isinstance(tokens, int) else None

    @property
    def reasoning_tokens(self) -> int | None:
        usage = self.body.get("usage") if isinstance(self.body, dict) else None
        details = usage.get("completion_tokens_details") if isinstance(usage, dict) else None
        tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None
        return tokens if isinstance(tokens, int) else None

    @property
    def error_detail(self) -> str:
        """The gateway's error message for a failed call (plain text or OpenAI-style JSON), or why it never answered."""
        if self.error:
            return self.error
        if isinstance(self.body, dict):
            error = self.body.get("error")
            if isinstance(error, dict):
                error = error.get("message") or error.get("code")
            message = error or self.body.get("message")
            return str(message) if message else json.dumps(self.body)
        if isinstance(self.body, list):
            return json.dumps(self.body)
        return str(self.body or "").strip() or self.reason or "No details"


def _ms_since(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def call_inference(
    target: Target,
    token: str,
    prompt: str,
    *,
    max_tokens: int = 1024,
    timeout: float = 120.0,
    session: requests.Session | None = None,
) -> InferenceResult:
    """POSTs an OpenAI chat completion to the target's namespace gateway with the Entra token as Bearer."""
    url = chat_completions_url(target.endpoint)
    if not target.endpoint.strip():
        return InferenceResult(target, url, error="No endpoint configured for this model deployment.", prompt=prompt)
    payload = {
        "model": target.model_deployment,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    started = time.perf_counter()
    try:
        response = (session or requests).post(
            url, json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=timeout
        )
    except requests.RequestException as exc:
        return InferenceResult(
            target, url, elapsed_ms=_ms_since(started), error=f"{type(exc).__name__}: {exc}", prompt=prompt
        )
    try:
        body = response.json()
    except ValueError:
        body = response.text
    return InferenceResult(
        target,
        url,
        status=response.status_code,
        reason=response.reason or "",
        elapsed_ms=_ms_since(started),
        body=body,
        headers={k.lower(): v for k, v in response.headers.items() if k.lower() in SHOWN_RESPONSE_HEADERS},
        prompt=prompt,
    )


class ArmError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def _arm(
    method: str,
    path: str,
    token: str,
    api_version: str,
    *,
    params: dict[str, str] | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    response = (session or requests).request(
        method,
        ARM_ENDPOINT + path,
        params={"api-version": api_version, **(params or {})},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if response.status_code >= 400:
        try:
            error = response.json()["error"]
            message = f"{error['code']}: {error['message']}"
        except (ValueError, KeyError, TypeError):
            message = response.text[:500]
        raise ArmError(response.status_code, message)
    return response.json() if response.content else {}


def action_matches(pattern: str, action: str) -> bool:
    """Azure RBAC action matching: case-insensitive, with '*' as a wildcard."""
    regex = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(regex, action, re.IGNORECASE) is not None


def permits_data_action(permissions: Iterable[dict[str, Any]], action: str = INFERENCE_DATA_ACTION) -> bool:
    """True if a permission set allows the data action and does not exclude it through notDataActions."""
    return any(
        any(action_matches(p, action) for p in perm.get("dataActions", []))
        and not any(action_matches(p, action) for p in perm.get("notDataActions", []))
        for perm in permissions
    )


def signed_in_user_can_infer(scope: str, arm_token: str, session: requests.Session | None = None) -> bool:
    """ARM's evaluation of the caller's effective permissions at scope (inherited and group assignments included)."""
    data = _arm(
        "GET", f"{scope}/providers/Microsoft.Authorization/permissions", arm_token, AUTHORIZATION_API_VERSION,
        session=session,
    )
    return permits_data_action(data.get("value", []))


@dataclass
class RoleAssignment:
    role: str
    scope: str
    principal_type: str
    grants_inference: bool


def role_assignments_at(
    scope: str, principal_id: str, arm_token: str, session: requests.Session | None = None
) -> list[RoleAssignment]:
    """Role assignments that apply to the principal at scope: direct, inherited from parent scopes, or via groups."""
    data = _arm(
        "GET", f"{scope}/providers/Microsoft.Authorization/roleAssignments", arm_token, AUTHORIZATION_API_VERSION,
        params={"$filter": f"atScope() and assignedTo('{principal_id}')"}, session=session,
    )
    definitions: dict[str, dict[str, Any]] = {}
    assignments = []
    for item in data.get("value", []):
        props = item["properties"]
        definition_id = props["roleDefinitionId"]
        if definition_id not in definitions:
            definitions[definition_id] = _arm(
                "GET", definition_id, arm_token, AUTHORIZATION_API_VERSION, session=session
            )["properties"]
        definition = definitions[definition_id]
        assignments.append(
            RoleAssignment(
                role=definition["roleName"],
                scope=props["scope"],
                principal_type=props.get("principalType", ""),
                grants_inference=permits_data_action(definition.get("permissions", [])),
            )
        )
    return sorted(assignments, key=lambda a: len(a.scope), reverse=True)


def scope_label(assignment_scope: str, target_scope: str) -> str:
    """Where an assignment returned by role_assignments_at() was made, relative to the namespace scope."""
    scope = assignment_scope.rstrip("/").lower()
    if scope == target_scope.rstrip("/").lower():
        return "this namespace"
    if scope == "":
        return "root (inherited)"
    if "/managementgroups/" in scope:
        return "management group (inherited)"
    if "/aimanagers/" in scope:
        return "AIManager (inherited)"
    if "/resourcegroups/" in scope:
        return "resource group (inherited)"
    return "subscription (inherited)"


def discover_endpoint(
    config: DemoConfig, target: Target, arm_token: str, session: requests.Session | None = None
) -> str:
    """Namespace gateway base URL, as returned by `az aimanager namespace list-accesskeys --query endpoint`."""
    scope = config.namespace_scope(target.namespace)
    try:
        # The response also carries the namespace API keys; they are deliberately ignored.
        return _arm("POST", f"{scope}/listAccessKeys", arm_token, AIMANAGER_API_VERSION, session=session)["endpoint"]
    except (ArmError, KeyError):
        deployment = _arm(
            "GET", f"{scope}/modelDeployments/{target.model_deployment}", arm_token, AIMANAGER_API_VERSION,
            session=session,
        )
        endpoint = ((deployment.get("properties") or {}).get("status") or {}).get("endpoint")
        if not endpoint:
            raise ArmError(404, f"{target.label} does not report an endpoint yet") from None
        return endpoint
