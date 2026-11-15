import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mock_gateway  # noqa: E402


@pytest.fixture(scope="session")
def gateway_url():
    server = mock_gateway.start(allowed_namespaces=("ns1",))
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
