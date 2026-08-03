from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from pydantic import BaseModel, Field


class AgentState(TypedDict):
    schema_version: int
    thread_id: str
    tenant_id: str
    principal_id: str
    objective: str
    action: Literal["create_change_request", "inspect_resource"]
    resource: str
    operation_id: str
    approval_required: bool
    approved: NotRequired[bool]
    approver_id: NotRequired[str]
    status: str
    result: NotRequired[dict[str, str]]


class StartRunRequest(BaseModel):
    objective: str = Field(min_length=3, max_length=500)
    action: Literal["create_change_request", "inspect_resource"]
    resource: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_./:-]{1,127}$")


class ResumeRequest(BaseModel):
    approved: bool
    reason: str = Field(min_length=3, max_length=300)


class RunResponse(BaseModel):
    thread_id: str
    status: str
    approval_required: bool
    result: dict[str, str] | None = None
    interrupt: dict[str, object] | None = None
