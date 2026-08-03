from __future__ import annotations

from typing import Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from resilient_agent.models import AgentState
from resilient_agent.persistence import Repository

Graph = CompiledStateGraph[AgentState, None, AgentState, AgentState]


def build_graph(repository: Repository, checkpointer: BaseCheckpointSaver[str]) -> Graph:
    def classify(state: AgentState) -> dict[str, object]:
        approval_required = state["action"] == "create_change_request"
        repository.record_audit(
            event_key=f"{state['thread_id']}:classified",
            tenant_id=state["tenant_id"],
            principal_id=state["principal_id"],
            thread_id=state["thread_id"],
            event_type="action_classified",
            details={"action": state["action"], "approval_required": approval_required},
        )
        return {
            "approval_required": approval_required,
            "status": "approval_required" if approval_required else "ready",
        }

    def request_approval(state: AgentState) -> dict[str, object]:
        repository.record_audit(
            event_key=f"{state['thread_id']}:approval_requested",
            tenant_id=state["tenant_id"],
            principal_id=state["principal_id"],
            thread_id=state["thread_id"],
            event_type="approval_requested",
            details={"action": state["action"], "resource": state["resource"]},
        )
        decision = interrupt(
            {
                "kind": "approval",
                "action": state["action"],
                "resource": state["resource"],
                "operation_id": state["operation_id"],
            }
        )
        approved = decision.get("approved") is True
        approver_id = str(decision.get("approver_id", "unknown"))
        repository.record_audit(
            event_key=f"{state['thread_id']}:approval_decided",
            tenant_id=state["tenant_id"],
            principal_id=approver_id,
            thread_id=state["thread_id"],
            event_type="approval_granted" if approved else "approval_rejected",
            details={"approved": approved, "reason_hash_present": bool(decision.get("reason"))},
        )
        return {
            "approved": approved,
            "approver_id": approver_id,
            "status": "approved" if approved else "rejected",
        }

    def execute(state: AgentState) -> dict[str, object]:
        if state["action"] == "inspect_resource":
            result = {"resource": state["resource"], "outcome": "inspected"}
        else:
            result = repository.execute_change_once(state["operation_id"], state["resource"])
        repository.record_audit(
            event_key=f"{state['thread_id']}:tool_completed",
            tenant_id=state["tenant_id"],
            principal_id=state["principal_id"],
            thread_id=state["thread_id"],
            event_type="tool_completed",
            details={"operation_id": state["operation_id"], "result": result},
        )
        return {"result": result, "status": "completed"}

    def route_after_classification(state: AgentState) -> Literal["approval", "execute"]:
        return "approval" if state["approval_required"] else "execute"

    def route_after_approval(state: AgentState) -> Literal["execute", "end"]:
        return "execute" if state.get("approved") else "end"

    builder = StateGraph(AgentState)
    builder.add_node("classify", classify)
    builder.add_node("approval", request_approval)
    builder.add_node("execute", execute)
    builder.add_edge(START, "classify")
    builder.add_conditional_edges(
        "classify", route_after_classification, {"approval": "approval", "execute": "execute"}
    )
    builder.add_conditional_edges(
        "approval", route_after_approval, {"execute": "execute", "end": END}
    )
    builder.add_edge("execute", END)
    return builder.compile(checkpointer=checkpointer)
