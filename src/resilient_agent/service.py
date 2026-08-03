from __future__ import annotations

import hashlib
import uuid
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from resilient_agent.identity import Identity
from resilient_agent.models import AgentState, ResumeRequest, RunResponse, StartRunRequest
from resilient_agent.persistence import Repository, digest


class RunService:
    def __init__(
        self,
        graph: CompiledStateGraph[AgentState, None, AgentState, AgentState],
        repository: Repository,
    ) -> None:
        self.graph = graph
        self.repository = repository

    def start(
        self, identity: Identity, request: StartRunRequest, idempotency_key: str
    ) -> RunResponse:
        proposed_thread_id = str(uuid.uuid4())
        request_hash = digest(request.model_dump(mode="json"))
        thread_id, created = self.repository.reserve_run(
            thread_id=proposed_thread_id,
            tenant_id=identity.tenant_id,
            principal_id=identity.principal_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if created:
            operation_id = hashlib.sha256(f"{thread_id}:tool:v1".encode()).hexdigest()
            initial: AgentState = {
                "schema_version": 1,
                "thread_id": thread_id,
                "tenant_id": identity.tenant_id,
                "principal_id": identity.principal_id,
                "objective": request.objective,
                "action": request.action,
                "resource": request.resource,
                "operation_id": operation_id,
                "approval_required": False,
                "status": "started",
            }
            result = self.graph.invoke(initial, self._config(thread_id), version="v1")
            return self._response(thread_id, result)
        return self.status(identity, thread_id)

    def resume(self, identity: Identity, thread_id: str, request: ResumeRequest) -> RunResponse:
        self.repository.assert_owner(thread_id, identity.tenant_id, identity.principal_id)
        result = self.graph.invoke(
            Command[object](resume={**request.model_dump(), "approver_id": identity.principal_id}),
            self._config(thread_id),
            version="v1",
        )
        return self._response(thread_id, result)

    def status(self, identity: Identity, thread_id: str) -> RunResponse:
        self.repository.assert_owner(thread_id, identity.tenant_id, identity.principal_id)
        snapshot = self.graph.get_state(self._config(thread_id))
        return self._response(thread_id, dict(snapshot.values), snapshot.tasks)

    def audit(self, identity: Identity, thread_id: str) -> list[dict[str, str]]:
        self.repository.assert_owner(thread_id, identity.tenant_id, identity.principal_id)
        return self.repository.audit(thread_id)

    def _response(self, thread_id: str, values: dict[str, Any], tasks: Any = None) -> RunResponse:
        snapshot = self.graph.get_state(self._config(thread_id)) if tasks is None else None
        active_tasks = snapshot.tasks if snapshot is not None else tasks
        interrupt_payload = None
        if active_tasks:
            interrupts = active_tasks[0].interrupts
            if interrupts:
                interrupt_payload = dict(interrupts[0].value)
        status = "approval_required" if interrupt_payload else str(values.get("status", "unknown"))
        return RunResponse(
            thread_id=thread_id,
            status=status,
            approval_required=interrupt_payload is not None,
            result=values.get("result"),
            interrupt=interrupt_payload,
        )

    @staticmethod
    def _config(thread_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": thread_id}}
