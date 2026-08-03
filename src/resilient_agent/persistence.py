from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class Repository:
    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS runs (
                    thread_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (tenant_id, principal_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS tool_operations (
                    operation_id TEXT PRIMARY KEY,
                    input_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS change_requests (
                    change_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL UNIQUE,
                    resource TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_key TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    details_hash TEXT NOT NULL
                );
                """
            )

    def reserve_run(
        self,
        *,
        thread_id: str,
        tenant_id: str,
        principal_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[str, bool]:
        with self.lock, self.connection:
            existing = self.connection.execute(
                "SELECT thread_id, request_hash FROM runs "
                "WHERE tenant_id=? AND principal_id=? AND idempotency_key=?",
                (tenant_id, principal_id, idempotency_key),
            ).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise ValueError("Idempotency-Key was already used for a different request")
                return str(existing["thread_id"]), False
            self.connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                (thread_id, tenant_id, principal_id, idempotency_key, request_hash, now()),
            )
            return thread_id, True

    def assert_owner(self, thread_id: str, tenant_id: str, principal_id: str) -> None:
        row = self.connection.execute(
            "SELECT 1 FROM runs WHERE thread_id=? AND tenant_id=? AND principal_id=?",
            (thread_id, tenant_id, principal_id),
        ).fetchone()
        if row is None:
            raise PermissionError("Run not found for this identity")

    def execute_change_once(self, operation_id: str, resource: str) -> dict[str, str]:
        input_hash = digest({"resource": resource})
        with self.lock, self.connection:
            existing = self.connection.execute(
                "SELECT input_hash, result_json FROM tool_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing:
                if existing["input_hash"] != input_hash:
                    raise RuntimeError("Operation id was reused with different inputs")
                return dict(json.loads(existing["result_json"]))

            change_id = f"chg_{hashlib.sha256(operation_id.encode()).hexdigest()[:12]}"
            result = {"change_id": change_id, "resource": resource, "outcome": "created"}
            self.connection.execute(
                "INSERT INTO change_requests VALUES (?, ?, ?, ?)",
                (change_id, operation_id, resource, now()),
            )
            self.connection.execute(
                "INSERT INTO tool_operations VALUES (?, ?, 'completed', ?, ?)",
                (operation_id, input_hash, canonical(result), now()),
            )
            return result

    def record_audit(
        self,
        *,
        event_key: str,
        tenant_id: str,
        principal_id: str,
        thread_id: str,
        event_type: str,
        details: dict[str, Any],
    ) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO audit_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_key, now(), tenant_id, principal_id, thread_id, event_type, digest(details)),
            )

    def audit(self, thread_id: str) -> list[dict[str, str]]:
        rows = self.connection.execute(
            "SELECT occurred_at, event_type, details_hash FROM audit_events "
            "WHERE thread_id=? ORDER BY occurred_at",
            (thread_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def count_changes(self, operation_id: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM change_requests WHERE operation_id=?", (operation_id,)
        ).fetchone()
        return int(row["count"])


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()
