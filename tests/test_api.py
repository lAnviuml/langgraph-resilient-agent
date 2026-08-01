from fastapi.testclient import TestClient

HEADERS = {
    "X-Tenant-Id": "acme",
    "X-Principal-Id": "alice",
    "Idempotency-Key": "request-0001",
}


def start_sensitive(client: TestClient, headers: dict[str, str] | None = None):
    return client.post(
        "/v1/runs",
        headers=headers or HEADERS,
        json={
            "objective": "Prepare the database change",
            "action": "create_change_request",
            "resource": "database/payments",
        },
    )


def test_sensitive_action_interrupts_then_resumes_once(client: TestClient) -> None:
    started = start_sensitive(client)
    assert started.status_code == 201
    pending = started.json()
    assert pending["status"] == "approval_required"
    assert pending["interrupt"]["action"] == "create_change_request"

    resumed = client.post(
        f"/v1/runs/{pending['thread_id']}/resume",
        headers=HEADERS,
        json={"approved": True, "reason": "Reviewed by on-call engineer"},
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "completed"
    assert resumed.json()["result"]["outcome"] == "created"

    operation_id = pending["interrupt"]["operation_id"]
    assert client.app.state.repository.count_changes(operation_id) == 1
    current = client.get(f"/v1/runs/{pending['thread_id']}", headers=HEADERS)
    assert current.json()["result"] == resumed.json()["result"]


def test_rejection_never_executes_tool(client: TestClient) -> None:
    pending = start_sensitive(client).json()
    rejected = client.post(
        f"/v1/runs/{pending['thread_id']}/resume",
        headers=HEADERS,
        json={"approved": False, "reason": "Change window is closed"},
    )
    assert rejected.json()["status"] == "rejected"
    assert client.app.state.repository.count_changes(pending["interrupt"]["operation_id"]) == 0


def test_idempotency_key_replays_and_rejects_mismatch(client: TestClient) -> None:
    first = start_sensitive(client)
    second = start_sensitive(client)
    assert second.json()["thread_id"] == first.json()["thread_id"]

    mismatch = client.post(
        "/v1/runs",
        headers=HEADERS,
        json={
            "objective": "Different request",
            "action": "inspect_resource",
            "resource": "api/orders",
        },
    )
    assert mismatch.status_code == 409


def test_thread_ownership_is_isolated(client: TestClient) -> None:
    thread_id = start_sensitive(client).json()["thread_id"]
    foreign = {"X-Tenant-Id": "other", "X-Principal-Id": "alice"}
    assert client.get(f"/v1/runs/{thread_id}", headers=foreign).status_code == 404
    assert client.get(f"/v1/runs/{thread_id}/audit", headers=foreign).status_code == 404


def test_safe_action_completes_without_interrupt(client: TestClient) -> None:
    response = client.post(
        "/v1/runs",
        headers={**HEADERS, "Idempotency-Key": "request-safe-1"},
        json={
            "objective": "Inspect current configuration",
            "action": "inspect_resource",
            "resource": "api/orders",
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "completed"
    assert response.json()["approval_required"] is False


def test_requires_identity_and_idempotency_key(client: TestClient) -> None:
    assert client.post("/v1/runs", json={}).status_code == 422
    assert client.get("/healthz").json() == {"status": "ok"}
