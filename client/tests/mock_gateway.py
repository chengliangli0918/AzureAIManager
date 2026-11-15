"""Local stand-in for the AIManager inference gateway, used by the tests and to rehearse the demo UI.

It mimics the gateway's authorization outcomes without validating tokens: a missing bearer token gets 401,
a namespace outside --allow gets 403 "RBAC: access denied", anything else gets an OpenAI-style chat
completion. Endpoints look like http://127.0.0.1:<port>/<namespace>/v1.

Rehearse the UI without Azure resources:
  python client/tests/mock_gateway.py --write-config /tmp/mock-demo-config.json &
  DEMO_CONFIG=/tmp/mock-demo-config.json streamlit run client/app.py
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _handler(allowed_namespaces: set[str]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            namespace, _, route = self.path.strip("/").partition("/")
            if route != "v1/chat/completions":
                self._send(404, {"error": {"code": "NotFound", "message": self.path}})
                return
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if not self.headers.get("Authorization", "").removeprefix("Bearer ").strip():
                challenge = 'Bearer realm="aimanager", error="invalid_token"'
                self._send(401, "Jwt is missing", {"WWW-Authenticate": challenge})
            elif namespace not in allowed_namespaces:
                self._send(403, "RBAC: access denied")
            else:
                self._send(200, {
                    "id": f"chatcmpl-{uuid.uuid4()}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": request.get("model"),
                    "choices": [{
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": f"[mock {namespace}] Kubernetes is an open-source system for automating "
                                       "deployment, scaling and management of containerized applications.",
                        },
                    }],
                    "usage": {"prompt_tokens": 9, "completion_tokens": 20, "total_tokens": 29},
                })

        def _send(self, status: int, payload: object, headers: dict[str, str] | None = None) -> None:
            is_text = isinstance(payload, str)
            data = (payload if is_text else json.dumps(payload)).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain" if is_text else "application/json")
            self.send_header("Content-Length", str(len(data)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


def start(allowed_namespaces: tuple[str, ...] = ("ns1",), port: int = 0) -> ThreadingHTTPServer:
    """Starts the mock gateway on a background thread; call shutdown() on the result to stop it."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(set(allowed_namespaces)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def demo_config(base_url: str) -> dict:
    """A demo-config.json (as written by scripts/setup.sh) whose endpoints point at the mock gateway."""
    return {
        "subscriptionId": "00000000-0000-0000-0000-000000000000",
        "resourceGroup": "mock-rg",
        "aiManager": "mock-aim",
        "targets": [
            {"namespace": "ns1", "modelDeployment": "md1", "endpoint": f"{base_url}/ns1/v1", "expected": "allow"},
            {"namespace": "ns2", "modelDeployment": "md2", "endpoint": f"{base_url}/ns2/v1", "expected": "deny"},
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow", nargs="+", default=["ns1"], help="namespaces whose calls succeed")
    parser.add_argument("--write-config", metavar="PATH", help="also write a demo config pointing at this mock")
    args = parser.parse_args()
    base_url = f"http://127.0.0.1:{args.port}"
    if args.write_config:
        Path(args.write_config).write_text(json.dumps(demo_config(base_url), indent=2))
    print(f"Mock AIManager gateway on {base_url}/<namespace>/v1 (allowed: {', '.join(args.allow)})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), _handler(set(args.allow))).serve_forever()
