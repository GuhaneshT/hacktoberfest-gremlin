from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.failproof import FailproofSession, PolicyViolation
from agent.runner import BASE_DIR, CHAOS_ROOT
from main import app

client = TestClient(app)
CALCULATOR = Path(__file__).resolve().parent.parent / "targets" / "calculator.py"


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_chaos_runs_real_tests_and_preserves_source() -> None:
    source_before = hashlib.sha256(CALCULATOR.read_bytes()).hexdigest()

    response = client.post(
        "/chaos",
        json={"intensity": "spicy", "max_tests": 5, "language": "python"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]
    assert 3 <= len(body["hypotheses"]) <= 5
    assert 1 <= len(body["tests"]) <= 5
    assert body["failures_triggered"] == sum(
        test["result"] == "FAILED" for test in body["tests"]
    )
    assert any(test["result"] == "FAILED" for test in body["tests"])
    assert body["failproof"]["tool_calls"] >= 1
    assert body["failproof"]["session_id"].startswith("fp_")

    source_after = hashlib.sha256(CALCULATOR.read_bytes()).hexdigest()
    assert source_before == source_after


def test_invalid_python_returns_422() -> None:
    response = client.post("/chaos", json={"target_code": "def broken("})
    assert response.status_code == 422


def test_failproof_blocks_source_writes_and_destructive_shell() -> None:
    session = FailproofSession(
        workspace_root=BASE_DIR,
        chaos_root=CHAOS_ROOT,
        target_path=CALCULATOR,
        session_id="fp_policy_test",
    )

    with session:
        with pytest.raises(PolicyViolation):
            session.invoke_tool(
                "Write",
                {"file_path": str(CALCULATOR), "content": "raise SystemExit"},
                lambda: None,
            )
        with pytest.raises(PolicyViolation):
            session.invoke_tool(
                "Bash",
                {"command": "rm -rf targets", "argv": ["rm", "-rf", "targets"]},
                lambda: None,
            )

    metrics = session.metrics()
    assert metrics["blocks"] == 2
    assert metrics["tool_calls"] == 2
