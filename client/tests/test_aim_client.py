import pytest

import aim_client as aim


def test_call_inference_tells_allowed_denied_and_failed_calls_apart(gateway_url):
    allowed = aim.call_inference(aim.Target("ns1", "md1", "allow", f"{gateway_url}/ns1/v1/"), "token", "hi")
    assert (allowed.status, allowed.outcome, allowed.as_expected) == (200, "allowed", True)
    assert allowed.url == f"{gateway_url}/ns1/v1/chat/completions"
    assert allowed.body["model"] == "md1" and allowed.reply

    denied = aim.call_inference(aim.Target("ns2", "md2", "deny", f"{gateway_url}/ns2/v1"), "token", "hi")
    assert (denied.status, denied.outcome, denied.as_expected) == (403, "denied", True)
    assert (denied.error_detail, denied.prompt) == ("RBAC: access denied", "hi")

    no_token = aim.call_inference(aim.Target("ns1", "md1", "allow", f"{gateway_url}/ns1/v1"), "", "hi")
    assert (no_token.status, no_token.outcome, no_token.as_expected) == (401, "denied", False)
    assert no_token.error_detail == "Jwt is missing"
    assert "invalid_token" in no_token.headers["www-authenticate"]

    # A deny target that can't be reached must not count as a successful RBAC denial.
    offline = aim.Target("ns2", "md2", "deny", "http://127.0.0.1:9/ns2/v1")
    unreachable = aim.call_inference(offline, "token", "hi", timeout=5)
    assert (unreachable.status, unreachable.outcome, unreachable.as_expected) == (None, "error", False)
    assert unreachable.error_detail.startswith("ConnectionError")


def test_error_detail_reads_openai_style_errors_and_falls_back_to_the_reason():
    target = aim.Target("ns1", "md9", "allow")
    error = {"error": {"message": "The model `md9` does not exist.", "code": "model_not_found"}}
    not_found = aim.InferenceResult(target, "url", status=404, reason="Not Found", body=error)
    assert not_found.error_detail == "The model `md9` does not exist."
    empty = aim.InferenceResult(target, "url", status=401, reason="Unauthorized", body="")
    assert empty.error_detail == "Unauthorized"


@pytest.mark.parametrize(
    ("choice", "reply", "reasoning"),
    [
        ({"message": {"content": "Kubernetes runs containers."}}, "Kubernetes runs containers.", None),
        # vLLM with a reasoning parser, as gemma-4-E4B-it on AIManager returns it.
        ({"message": {"content": "Kubernetes runs containers.", "reasoning": "The user wants"}},
         "Kubernetes runs containers.", "The user wants"),
        ({"message": {"content": [{"type": "text", "text": "Kubernetes "}, {"type": "text", "text": "runs."}]}},
         "Kubernetes runs.", None),
        ({"text": "Kubernetes runs containers."}, "Kubernetes runs containers.", None),
        ({"message": {"content": None, "reasoning_content": "The user asks"}, "finish_reason": "length"},
         None, "The user asks"),
    ],
)
def test_answer_text_is_read_from_any_openai_compatible_choice(choice, reply, reasoning):
    result = aim.InferenceResult(aim.Target("ns1", "md1", "allow"), "url", status=200, body={"choices": [choice]})
    assert (result.reply, result.reasoning) == (reply, reasoning)


@pytest.mark.parametrize(
    ("permissions", "allowed"),
    [
        ([{"dataActions": [aim.INFERENCE_DATA_ACTION]}], True),  # Azure AIManager Inference User
        ([{"dataActions": ["Microsoft.ContainerService/aiManagers/*"]}], True),  # Azure AIManager RBAC Admin
        ([{"dataActions": ["Microsoft.ContainerService/aiManagers/*/read"]}], False),  # Azure AIManager RBAC Reader
        ([{"actions": ["*"], "dataActions": []}], False),  # Owner: control plane only
        ([{"dataActions": ["*"], "notDataActions": ["Microsoft.ContainerService/aiManagers/namespaces/*"]}], False),
        ([{"dataActions": ["microsoft.containerservice/AIMANAGERS/namespaces/inference/action"]}], True),
    ],
)
def test_permits_data_action_follows_azure_rbac_matching(permissions, allowed):
    assert aim.permits_data_action(permissions) is allowed
