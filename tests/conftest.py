import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from resilient_agent.app import app


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    os.environ["AGENT_DATA_DIR"] = str(tmp_path)
    with TestClient(app) as test_client:
        yield test_client
