from __future__ import annotations

import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from langgraph.checkpoint.sqlite import SqliteSaver

from resilient_agent.graph import build_graph
from resilient_agent.identity import Identity, authenticated_identity
from resilient_agent.models import ResumeRequest, RunResponse, StartRunRequest
from resilient_agent.persistence import Repository
from resilient_agent.service import RunService


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    data_dir = Path(os.getenv("AGENT_DATA_DIR", ".data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    repository = Repository(data_dir / "agent.db")
    checkpoint_connection = sqlite3.connect(data_dir / "checkpoints.db", check_same_thread=False)
    checkpointer = SqliteSaver(checkpoint_connection)
    app.state.repository = repository
    app.state.service = RunService(build_graph(repository, checkpointer), repository)
    yield
    checkpoint_connection.close()
    repository.close()


app = FastAPI(title="LangGraph Resilient Agent", version="0.1.0", lifespan=lifespan)


def service(request: Request) -> RunService:
    return cast(RunService, request.app.state.service)


@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/runs", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
def start_run(
    body: StartRunRequest,
    identity: Annotated[Identity, Depends(authenticated_identity)],
    run_service: Annotated[RunService, Depends(service)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> RunResponse:
    try:
        return run_service.start(identity, body, idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@app.get("/v1/runs/{thread_id}", response_model=RunResponse)
def get_run(
    thread_id: str,
    identity: Annotated[Identity, Depends(authenticated_identity)],
    run_service: Annotated[RunService, Depends(service)],
) -> RunResponse:
    try:
        return run_service.status(identity, thread_id)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found") from exc


@app.post("/v1/runs/{thread_id}/resume", response_model=RunResponse)
def resume_run(
    thread_id: str,
    body: ResumeRequest,
    identity: Annotated[Identity, Depends(authenticated_identity)],
    run_service: Annotated[RunService, Depends(service)],
) -> RunResponse:
    try:
        return run_service.resume(identity, thread_id, body)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found") from exc


@app.get("/v1/runs/{thread_id}/audit")
def get_audit(
    thread_id: str,
    identity: Annotated[Identity, Depends(authenticated_identity)],
    run_service: Annotated[RunService, Depends(service)],
) -> list[dict[str, str]]:
    try:
        return run_service.audit(identity, thread_id)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found") from exc
