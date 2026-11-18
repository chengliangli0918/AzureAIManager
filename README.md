# AIManager namespace-scoped inference RBAC demo

This demo shows how Azure RBAC on an AIManager **namespace** controls access to its inference endpoint.
The account signed in through `az login` has the **Azure AIManager Inference User** role on `ns1` only.
With the same Entra token, requests to `ns1/md1` succeed, while requests to `ns2/md2` receive **403 Forbidden**
(or 401 if the token is invalid).

> [!IMPORTANT]
> **Customer scenario:** multiple teams in an organization can **share a single AIManager**, with each team
> limited to running inference on model deployments in namespaces where it has the **Azure AIManager Inference
> User** role. Grant or revoke access through standard Azure role assignments on each namespace for users,
> groups, or service principals, rather than distributing namespace API keys.

```text
AIManager charlili-aim (italynorth, resource group charlilirg)
├── namespace ns1 ── model deployment md1   your az login account: Azure AIManager Inference User
└── namespace ns2 ── model deployment md2   your az login account: no inference role
```

Both model deployments serve `google/gemma-4-E4B-it` on `Standard_ND96amsr_A100_v4`.

## Prerequisites

- Azure CLI, jq, and curl. Sign in with `az login` as the account that should receive access (a user or a
  service principal).
- The Owner or User Access Administrator role on the target subscription to create role assignments.
- The `Microsoft.ContainerService/AIManagerPreview` feature registered, which requires manual approval.
- Python 3.10+ for the Streamlit client.

`setup.sh` installs or upgrades the `aimanager` CLI extension when needed.

## Run the demo

```bash
az login                                                  # the signed-in account is the demo user
az account set -s c582d154-33c4-47e4-a3c2-0632d20b12eb   # the scripts use the Azure CLI's default subscription

./scripts/setup.sh --dry-run   # read-only checks, plus the commands that would change Azure
./scripts/setup.sh             # asks before creating the GPU model deployments (--yes to skip)
./scripts/verify.sh            # curl check: ns1/md1 -> 200, ns2/md2 -> 401/403

python3 -m venv .venv && .venv/bin/pip install -r client/requirements.txt
.venv/bin/streamlit run client/app.py

./scripts/cleanup.sh           # deletes md1/md2, ns1/ns2 and the AIManager (add --delete-resource-group to drop charlilirg)
```

`setup.sh` is safe to rerun and preserves existing resources. It performs the following steps:

1. Validates the subscription, the signed-in account and its object ID, feature registration, and CLI extension.
   Checks that the model is available in the `italynorth` catalog and can use `Standard_ND96amsr_A100_v4`,
   including a quota check.
2. Creates resource group `charlilirg`, AIManager `charlili-aim`, and namespaces `ns1` and `ns2`.
3. Assigns **Azure AIManager Inference User** to the signed-in account at the `ns1` scope, removes that
   assignment from `ns2` if present, and uses ARM's effective-permission API to warn if an inherited or group role would
   still allow inference on `ns2`.
4. Writes namespace endpoints and scopes, without keys, to `demo-config.json` for `verify.sh` and the client.
   If the endpoints are not yet available, retries once the model deployments are serving.
5. Creates `md1` in `ns1` and `md2` in `ns2` with one replica each, waits until both are serving, then runs `verify.sh`.

The role is deliberately assigned before the model deployments are created. Azure RBAC changes take a few
minutes to propagate, and the gateway caches authorization decisions, including denials, for about 5 minutes.

### Settings

The scripts always use the Azure CLI's default subscription and signed-in account. Switch subscriptions with
`az account set -s <id>`; the account signed in through `az login` receives the role assignment.
`setup.sh` and `cleanup.sh` stop if `demo-config.json` refers to a demo in a different subscription.

Other defaults are defined in [scripts/config.sh](scripts/config.sh). Override them with environment variables,
for example: `AIM_NAME=aim-demo2 MODEL_ID=Qwen/Qwen3.5-9B ./scripts/setup.sh`.

| Variable | Default | Description |
|---|---|---|
| `RG` / `LOCATION` | `charlilirg` / `italynorth` | Resource group and region |
| `AIM_NAME` | `charlili-aim` | AIManager name |
| `NS1` `MD1` / `NS2` `MD2` | `ns1` `md1` / `ns2` `md2` | Namespaces and model deployments |
| `VM_SIZE` | `Standard_ND96amsr_A100_v4` | GPU VM size for both model deployments |
| `MODEL_ID` | `google/gemma-4-E4B-it` | Model ID from `az aimanager model list -l italynorth` |
| `REPLICAS` | `1` | Replicas per model deployment |
| `WAIT_TIMEOUT_MIN` | `120` | Maximum wait time in minutes for the model deployments during setup |

## Streamlit client

[client/app.py](client/app.py) authenticates with Entra ID through the Azure CLI or an interactive browser
and displays:

1. **Who is calling**: the token's user, object ID and audience (`https://management.core.windows.net/`,
   the audience accepted by the gateway and used by `az account get-access-token`).
2. **What Azure RBAC says**: the role assignments that apply to the user on each namespace (direct,
   inherited, or through a group) and ARM's effective-permission result for
   `Microsoft.ContainerService/aiManagers/namespaces/inference/action`.
3. **Call a model deployment**: select a namespace and model deployment from the dropdowns, enter
   a prompt, and select **Send**. Each call appears in the inference history, newest first, with its HTTP
   status, prompt, and either the model's answer (for a 200 response, including the finish reason and completion
   token count) or the error. The history also indicates whether the outcome was expected. A 403 response reads *User does not have
   Inference access to the resource '&lt;namespace resource ID&gt;'. Update role assignment to allow access.*;
   the gateway's response appears under **Request and response**.

The following screenshot shows a live run after sending *What is AKS Automatic in one sentence?* to
`ns1/md1` and then to `ns2/md2`. The history lists the newest call first.

![Streamlit client: ns1/md1 answers the prompt, ns2/md2 is refused with 403 Forbidden](docs/streamlit-client.png)

`gemma-4-E4B-it` generates reasoning before answering. The server returns the reasoning in `message.reasoning`
and the answer in `message.content`. The app displays the answer and keeps the reasoning collapsed under
**Model reasoning**. `max_tokens` defaults to 1024. Reasoning often requires a few hundred tokens, even for a
one-sentence answer; a limit that is too low can cut off the answer.

The endpoints come from `demo-config.json`. If the file is missing, **Discover endpoints from Azure** reads
them from ARM using the Azure CLI's default subscription. Run the app from the repository root:
[.streamlit/config.toml](.streamlit/config.toml) restricts it to localhost because the app uses
your Azure CLI credentials.

To try the UI without Azure resources, run it against a local mock gateway:

```bash
.venv/bin/python client/tests/mock_gateway.py --write-config /tmp/mock-demo-config.json &
DEMO_CONFIG=/tmp/mock-demo-config.json .venv/bin/streamlit run client/app.py
```

Tests: `.venv/bin/pip install pytest && .venv/bin/python -m pytest client/tests`

## Troubleshooting

- **`ns1/md1` returns 403.** The role assignment has not propagated yet, or the gateway has cached an earlier denial.
  Wait about 5 minutes and retry. `verify.sh` waits until ARM reports the permission before calling `ns1`.
- **`ns2/md2` returns 200.** Another role grants the inference data action on `ns2`, such as
  *Azure AIManager RBAC Admin* assigned at the AIManager, resource group, or subscription scope, or through a group. Check with
  `az role assignment list --assignee-object-id <oid> --scope <ns2 scope> --include-inherited --include-groups -o table`.
  Owner, Contributor, and the *Azure AIManager Contributor* / *RBAC Reader* roles that `az aimanager create`
  grants to the creator do not include this data action.
- **A request returns 401.** The token is missing, expired, or issued for the wrong audience. Use `az account get-access-token`
  (audience `https://management.core.windows.net/`).
- **Namespace API keys** from `az aimanager namespace list-accesskeys` authenticate without Entra RBAC.
  This demo uses only Entra tokens.
