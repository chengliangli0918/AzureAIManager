import base64
import json
import time
from pathlib import Path
from unittest.mock import patch

import streamlit as st
from azure.core.credentials import AccessToken
from streamlit.testing.v1 import AppTest

import mock_gateway

APP = Path(__file__).resolve().parents[1] / "app.py"


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


class FakeCredential:
    def get_token(self, *scopes, **kwargs):
        expires = int(time.time()) + 3600
        claims = {
            "upn": "demo-user@contoso.com",
            "oid": "00000000-0000-0000-0000-000000000001",
            "aud": "https://management.core.windows.net/",
            "tid": "00000000-0000-0000-0000-000000000002",
            "exp": expires,
        }
        return AccessToken(f"{_b64({'alg': 'none'})}.{_b64(claims)}.sig", expires)


def test_history_keeps_the_ns1_reply_and_the_ns2_refusal(tmp_path, monkeypatch, gateway_url):
    config_path = tmp_path / "demo-config.json"
    config_path.write_text(json.dumps(mock_gateway.demo_config(gateway_url)))
    monkeypatch.setenv("DEMO_CONFIG", str(config_path))
    st.cache_resource.clear()

    with patch("aim_client.make_credential", return_value=FakeCredential()):
        app = AppTest.from_file(str(APP), default_timeout=30).run()
        assert not app.exception
        assert "demo-user@contoso.com" in app.success[0].value
        assert (app.selectbox(key="namespace").value, app.selectbox(key="md_ns1").value) == ("ns1", "md1")

        app.button(key="send").click().run()
        app.selectbox(key="namespace").set_value("ns2").run()
        assert app.selectbox(key="md_ns2").value == "md2"
        app.button(key="send").click().run()

    assert not app.exception
    entries = [m.value for m in app.markdown if m.value.startswith(("✅", "⛔"))]
    assert len(entries) == 2 and "403 Forbidden" in entries[0] and "200 OK" in entries[1]  # newest first
    assert any(
        e.value == "**403 Forbidden**: User does not have Inference access to the resource "
        "'/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/mock-rg/providers/"
        "Microsoft.ContainerService/aiManagers/mock-aim/namespaces/ns2'. Update role assignment to allow access."
        for e in app.error
    )
    assert [m.value for m in app.markdown if m.value == "**Answer**"] == ["**Answer**"]  # only the 200 entry
    assert any(m.value.startswith("[mock ns1]") for m in app.markdown)
    assert any("finish_reason `stop` · 20 completion tokens" in c.value for c in app.caption)
    assert not any("Demo passed" in s.value for s in app.success)
