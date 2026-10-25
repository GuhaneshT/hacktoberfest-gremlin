from __future__ import annotations

import logging
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from agent.runner import TargetAnalysisError, run_chaos

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gremlinapi")

app = FastAPI(
    title="GremlinAPI",
    description="API-first chaos engineering with Failproof AI safety enforcement.",
    version="0.1.0",
)


class ChaosRequest(BaseModel):
    target_code: str | None = None
    intensity: Literal["mild", "spicy", "nuclear"] = "mild"
    max_tests: int = Field(default=5, ge=1, le=20)
    language: Literal["python"] = "python"


class ChaosTestResult(BaseModel):
    file: str
    failure_mode: str
    result: Literal["FAILED", "PASSED"]
    output: str


class FailproofMetrics(BaseModel):
    tool_calls: int
    blocks: int
    loops_stopped: int
    session_id: str


class ChaosResponse(BaseModel):
    summary: str
    hypotheses: list[str]
    tests: list[ChaosTestResult]
    failures_triggered: int
    failproof: FailproofMetrics
    notes: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "gremlin-api"}


@app.post("/chaos", response_model=ChaosResponse)
def chaos(request: ChaosRequest) -> dict:
    if request.target_code is not None and not request.target_code.strip():
        raise HTTPException(status_code=422, detail="target_code cannot be empty.")

    try:
        return run_chaos(
            target_code=request.target_code,
            intensity=request.intensity,
            max_tests=request.max_tests,
        )
    except TargetAnalysisError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Chaos run failed")
        raise HTTPException(
            status_code=500, detail=f"Chaos run failed: {type(exc).__name__}"
        ) from exc
