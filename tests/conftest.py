import os
import time
from pathlib import Path

# Match the Java build (tasks.test sets DISABLE_TX=true): unit tests never touch the network.
os.environ.setdefault("DISABLE_TX", "true")
os.environ.setdefault("PACKAGES_OFFLINE", "true")

import pytest
from fastapi.testclient import TestClient

from app.main import app

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _wait_ready(timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while not app.state.ready:
        if app.state.load_error:
            raise RuntimeError("validator failed to load")
        if time.time() > deadline:
            raise TimeoutError("validator did not become ready")
        time.sleep(0.05)


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        _wait_ready()
        yield test_client


@pytest.fixture
def load_fixture():
    def _load(filename: str) -> bytes:
        return (FIXTURES_DIR / filename).read_bytes()

    return _load


def errors_of(outcome: dict) -> list[dict]:
    return [i for i in outcome.get("issue", []) if i.get("severity") in ("error", "fatal")]


def messages_of(outcome: dict) -> str:
    return " | ".join((i.get("details") or {}).get("text", "") or i.get("diagnostics", "") for i in outcome.get("issue", []))
