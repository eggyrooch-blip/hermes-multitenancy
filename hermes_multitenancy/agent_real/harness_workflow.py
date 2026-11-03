"""Persistent workflow gates and hard operation policy for online Harness runs."""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..trusted_runtime_principal import TrustedRuntimePrincipal


GATES = ("A", "B", "C", "D", "E", "F")
FLOW_GATES = {
    "server-dev": ("A", "B", "C", "D", "E"),
    "server-dev-light": ("A", "C", "D", "E"),
    "server-bugfix": ("F", "C", "D", "E"),
}
OPERATION_GATES = {
    "git_commit": "D",
    "git_push": "D",
    "mr_create": "D",
    "routekey_add": "E",
    "deploy": "E",
    "bug_close": "E",
    "spec_defect_route": "F",
}
GATE_REQUIRED_EVIDENCE = {
    "A": {"ub"},
    "B": {"p4"},
    "C": {"cr", "red"},
    "D": {"test"},
    "E": {"pre"},
    "F": {"defect"},
}
CREDENTIAL_CONNECTORS = {
    "mobius": "kep-cli-online",
    "kep-cli-online": "kep-cli-online",
    "kep-cli-pre": "kep-cli-pre",
    "feishu-project": "feishu-project",
    "lark-cli": "lark-cli",
    "gitlab": "gitlab",
}
_OPAQUE = re.compile(r"[A-Za-z0-9_.:-]{1,256}")


class HarnessWorkflowRejected(ValueError):
    pass


def connector_for_credential(credential_kind: str) -> str:
    connector = CREDENTIAL_CONNECTORS.get(str(credential_kind or "").strip())
    if not connector:
        raise HarnessWorkflowRejected("credential_kind_invalid")
    return connector


def _opaque(name: str, value: Any) -> str:
    text = str(value or "").strip()
    if not _OPAQUE.fullmatch(text):
        raise HarnessWorkflowRejected(f"{name}_invalid")
    return text


def _principal(principal: TrustedRuntimePrincipal) -> tuple[str, str]:
    if (
        not isinstance(principal, TrustedRuntimePrincipal)
        or not principal.is_authentic()
        or principal.channel != "webui"
        or principal.credential_subject != principal.actor_subject
    ):
        raise HarnessWorkflowRejected("principal_invalid")
    return principal.profile_name, principal.actor_subject


def _gate_evidence(flow: str, gate: str, checklist: list[Any]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    if not isinstance(checklist, list) or len(checklist) > 20:
        raise HarnessWorkflowRejected("gate_evidence_invalid")
    for item in checklist:
        if not isinstance(item, dict):
            raise HarnessWorkflowRejected("gate_evidence_invalid")
        kind = str(item.get("kind") or "").strip().lower()
        evidence_id = str(item.get("id") or "").strip()
        if kind not in set().union(*GATE_REQUIRED_EVIDENCE.values()) or not evidence_id:
            raise HarnessWorkflowRejected("gate_evidence_invalid")
        normalized.append(
            {
                "kind": kind,
                "id": evidence_id[:500],
                "summary": str(item.get("summary") or "").strip()[:500],
            }
        )
    required = set(GATE_REQUIRED_EVIDENCE[gate])
    if flow == "server-dev-light" and gate == "A":
        required.add("p4")
    if not required.issubset({item["kind"] for item in normalized}):
        raise HarnessWorkflowRejected("gate_evidence_invalid")
    return normalized


class HarnessWorkflowStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS harness_workflows (
              workflow_id TEXT PRIMARY KEY, profile_name TEXT NOT NULL,
              actor_subject TEXT NOT NULL, thread_id TEXT NOT NULL,
              flow TEXT NOT NULL, status TEXT NOT NULL,
              credential_kind TEXT NOT NULL DEFAULT '', updated_at_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS harness_gates (
              workflow_id TEXT NOT NULL, gate TEXT NOT NULL,
              approval_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
              checklist_json TEXT NOT NULL, comment TEXT NOT NULL DEFAULT '',
              combined_gate TEXT NOT NULL DEFAULT '', updated_at_ms INTEGER NOT NULL,
              PRIMARY KEY(workflow_id, gate)
            );
            CREATE TABLE IF NOT EXISTS harness_operations (
              workflow_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
              operation TEXT NOT NULL, arguments_json TEXT NOT NULL,
              result_json TEXT NOT NULL,
              updated_at_ms INTEGER NOT NULL,
              PRIMARY KEY(workflow_id, idempotency_key)
            );
            CREATE TABLE IF NOT EXISTS harness_workflow_stages (
              workflow_id TEXT PRIMARY KEY, stage TEXT NOT NULL,
              status TEXT NOT NULL, summary TEXT NOT NULL,
              related_ids_json TEXT NOT NULL, audit_id TEXT NOT NULL,
              updated_at_ms INTEGER NOT NULL
            );
            """
        )
        columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(harness_operations)")
        }
        if "arguments_json" not in columns:
            self._conn.execute(
                "ALTER TABLE harness_operations ADD COLUMN arguments_json TEXT NOT NULL DEFAULT '{}'"
            )
        self._conn.commit()
        Path(self.db_path).chmod(0o600)

    def close(self) -> None:
        self._conn.close()

    def _workflow(self, principal: TrustedRuntimePrincipal, workflow_id: str) -> sqlite3.Row:
        profile, actor = _principal(principal)
        workflow_id = _opaque("workflow_id", workflow_id)
        row = self._conn.execute(
            "SELECT * FROM harness_workflows WHERE workflow_id=?", (workflow_id,)
        ).fetchone()
        if row is None:
            raise HarnessWorkflowRejected("workflow_missing")
        if row["profile_name"] != profile or row["actor_subject"] != actor:
            raise HarnessWorkflowRejected("principal_mismatch")
        return row

    def start(
        self,
        principal: TrustedRuntimePrincipal,
        workflow_id: str,
        thread_id: str,
        flow: str,
    ) -> None:
        profile, actor = _principal(principal)
        workflow_id = _opaque("workflow_id", workflow_id)
        thread_id = _opaque("thread_id", thread_id)
        if flow not in FLOW_GATES:
            raise HarnessWorkflowRejected("flow_invalid")
        now = int(time.time() * 1000)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM harness_workflows WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            if row is not None:
                if (row["profile_name"], row["actor_subject"], row["flow"]) != (
                    profile, actor, flow
                ):
                    raise HarnessWorkflowRejected("principal_mismatch")
                if row["thread_id"].startswith("pending:") and not thread_id.startswith("pending:"):
                    self._conn.execute(
                        "UPDATE harness_workflows SET thread_id=?,updated_at_ms=? WHERE workflow_id=?",
                        (thread_id, now, workflow_id),
                    )
                    self._conn.commit()
                elif row["thread_id"] != thread_id:
                    raise HarnessWorkflowRejected("principal_mismatch")
                return
            self._conn.execute(
                "INSERT INTO harness_workflows VALUES (?,?,?,?,?,'running','',?)",
                (workflow_id, profile, actor, thread_id, flow, now),
            )
            self._conn.commit()

    def request_gate(
        self,
        principal: TrustedRuntimePrincipal,
        workflow_id: str,
        gate: str,
        checklist: list[dict[str, Any]],
    ) -> str:
        if gate not in GATES:
            raise HarnessWorkflowRejected("gate_invalid")
        with self._lock:
            workflow = self._workflow(principal, workflow_id)
            checklist = _gate_evidence(workflow["flow"], gate, checklist)
            flow_gates = FLOW_GATES[workflow["flow"]]
            if gate not in flow_gates:
                raise HarnessWorkflowRejected("gate_out_of_order")
            prior = flow_gates[: flow_gates.index(gate)]
            approved = {
                row["gate"]
                for row in self._conn.execute(
                    "SELECT gate FROM harness_gates WHERE workflow_id=? AND status='approved'",
                    (workflow_id,),
                )
            }
            if any(item not in approved for item in prior):
                raise HarnessWorkflowRejected("gate_out_of_order")
            existing = self._conn.execute(
                "SELECT * FROM harness_gates WHERE workflow_id=? AND gate=?",
                (workflow_id, gate),
            ).fetchone()
            if existing is not None and existing["status"] == "waiting":
                return str(existing["approval_id"])
            approval_id = f"gate_{secrets.token_urlsafe(18)}"
            combined = "B" if workflow["flow"] == "server-dev-light" and gate == "A" else ""
            now = int(time.time() * 1000)
            self._conn.execute(
                "INSERT INTO harness_gates VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(workflow_id,gate) DO UPDATE SET "
                "approval_id=excluded.approval_id,status='waiting',"
                "checklist_json=excluded.checklist_json,comment='',"
                "combined_gate=excluded.combined_gate,updated_at_ms=excluded.updated_at_ms",
                (
                    workflow_id,
                    gate,
                    approval_id,
                    "waiting",
                    json.dumps(checklist, ensure_ascii=False, sort_keys=True),
                    "",
                    combined,
                    now,
                ),
            )
            self._conn.execute(
                "UPDATE harness_workflows SET status='waiting_gate',updated_at_ms=? WHERE workflow_id=?",
                (now, workflow_id),
            )
            self._conn.commit()
            return approval_id

    def resolve_gate(
        self,
        principal: TrustedRuntimePrincipal,
        approval_id: str,
        decision: str,
        comment: str,
        workflow_id: str | None = None,
    ) -> None:
        approval_id = _opaque("approval_id", approval_id)
        if decision not in {"approve", "reject", "rework"}:
            raise HarnessWorkflowRejected("decision_invalid")
        with self._lock:
            gate = self._conn.execute(
                "SELECT * FROM harness_gates WHERE approval_id=?", (approval_id,)
            ).fetchone()
            if gate is None:
                raise HarnessWorkflowRejected("approval_missing")
            if workflow_id is not None and gate["workflow_id"] != _opaque("workflow_id", workflow_id):
                raise HarnessWorkflowRejected("workflow_mismatch")
            self._workflow(principal, gate["workflow_id"])
            if gate["status"] != "waiting":
                raise HarnessWorkflowRejected("approval_already_resolved")
            status = {"approve": "approved", "reject": "rejected", "rework": "rework"}[decision]
            now = int(time.time() * 1000)
            self._conn.execute(
                "UPDATE harness_gates SET status=?,comment=?,updated_at_ms=? WHERE approval_id=?",
                (status, str(comment or "")[:1000], now, approval_id),
            )
            if status == "approved" and gate["combined_gate"]:
                self._conn.execute(
                    "INSERT OR REPLACE INTO harness_gates VALUES (?,?,?,?,?,?,?,?)",
                    (
                        gate["workflow_id"], gate["combined_gate"], approval_id + ".combined",
                        "approved", "[]", str(comment or "")[:1000], "", now,
                    ),
                )
            workflow_status = "running" if status == "approved" else status
            self._conn.execute(
                "UPDATE harness_workflows SET status=?,updated_at_ms=? WHERE workflow_id=?",
                (workflow_status, now, gate["workflow_id"]),
            )
            self._conn.commit()

    def pending(self, principal: TrustedRuntimePrincipal, workflow_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._workflow(principal, workflow_id)
            row = self._conn.execute(
                "SELECT * FROM harness_gates WHERE workflow_id=? AND status='waiting'",
                (workflow_id,),
            ).fetchone()
            return None if row is None else dict(row)

    def snapshot(self, principal: TrustedRuntimePrincipal, workflow_id: str) -> dict[str, Any]:
        with self._lock:
            workflow = self._workflow(principal, workflow_id)
            stage = self._conn.execute(
                "SELECT * FROM harness_workflow_stages WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
            pending_gate = self._conn.execute(
                "SELECT approval_id,gate,checklist_json FROM harness_gates "
                "WHERE workflow_id=? AND status='waiting'",
                (workflow_id,),
            ).fetchone()
            approved = {
                row["gate"]
                for row in self._conn.execute(
                    "SELECT gate FROM harness_gates WHERE workflow_id=? AND status='approved'",
                    (workflow_id,),
                )
            }
            result = {
                "workflow_id": workflow_id,
                "flow": workflow["flow"],
                "status": workflow["status"],
                "approved_gates": [gate for gate in GATES if gate in approved],
                "credential_kind": workflow["credential_kind"],
                "connector_id": (
                    connector_for_credential(workflow["credential_kind"])
                    if workflow["credential_kind"]
                    else ""
                ),
            }
            if stage is not None:
                result.update(
                    stage=stage["stage"],
                    stage_status=stage["status"],
                    summary=stage["summary"],
                    related_ids=json.loads(stage["related_ids_json"]),
                    audit_id=stage["audit_id"],
                )
            if pending_gate is not None:
                result["pending_gate"] = {
                    "approval_id": pending_gate["approval_id"],
                    "gate": pending_gate["gate"],
                    "checklist": json.loads(pending_gate["checklist_json"]),
                }
            return result

    def set_stage(
        self,
        principal: TrustedRuntimePrincipal,
        workflow_id: str,
        stage: str,
        status: str,
        summary: str,
        related_ids: dict[str, Any],
    ) -> dict[str, Any]:
        stage = _opaque("stage", stage)
        status = _opaque("status", status)
        if not isinstance(related_ids, dict) or len(related_ids) > 20:
            raise HarnessWorkflowRejected("related_ids_invalid")
        related = {
            _opaque("related_id_key", key): str(value).strip()[:1000]
            for key, value in related_ids.items()
            if str(value).strip()
        }
        audit_id = f"audit_{secrets.token_urlsafe(18)}"
        summary = str(summary or "")[:1000]
        with self._lock:
            self._workflow(principal, workflow_id)
            self._conn.execute(
                "INSERT INTO harness_workflow_stages VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(workflow_id) DO UPDATE SET "
                "stage=excluded.stage,status=excluded.status,summary=excluded.summary,"
                "related_ids_json=excluded.related_ids_json,audit_id=excluded.audit_id,"
                "updated_at_ms=excluded.updated_at_ms",
                (
                    workflow_id,
                    stage,
                    status,
                    summary,
                    json.dumps(related, ensure_ascii=False, sort_keys=True),
                    audit_id,
                    int(time.time() * 1000),
                ),
            )
            self._conn.commit()
        return {
            "event": "workflow_stage",
            "workflow_id": workflow_id,
            "stage": stage,
            "status": status,
            "summary": summary,
            "related_ids": related,
            "audit_id": audit_id,
        }

    def pause_for_credential(
        self, principal: TrustedRuntimePrincipal, workflow_id: str, credential_kind: str
    ) -> None:
        credential_kind = _opaque("credential_kind", credential_kind)
        connector_for_credential(credential_kind)
        with self._lock:
            self._workflow(principal, workflow_id)
            self._conn.execute(
                "UPDATE harness_workflows SET status='waiting_credential',credential_kind=?,updated_at_ms=? WHERE workflow_id=?",
                (credential_kind, int(time.time() * 1000), workflow_id),
            )
            self._conn.commit()

    def resume_credential(
        self,
        principal: TrustedRuntimePrincipal,
        workflow_id: str,
        credential_kind: str,
        *,
        validator: Callable[[TrustedRuntimePrincipal, str], bool] | None = None,
    ) -> None:
        credential_kind = _opaque("credential_kind", credential_kind)
        with self._lock:
            workflow = self._workflow(principal, workflow_id)
            if (
                workflow["status"] != "waiting_credential"
                or workflow["credential_kind"] != credential_kind
            ):
                raise HarnessWorkflowRejected("credential_resume_invalid")
            if validator is None or not validator(principal, credential_kind):
                raise HarnessWorkflowRejected("credential_unavailable")
            self._conn.execute(
                "UPDATE harness_workflows SET status='running',credential_kind='',updated_at_ms=? WHERE workflow_id=?",
                (int(time.time() * 1000), workflow_id),
            )
            self._conn.commit()

    def execute(
        self,
        principal: TrustedRuntimePrincipal,
        workflow_id: str,
        operation: str,
        arguments: dict[str, Any],
        idempotency_key: str,
        adapter: Callable[[str, dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        if operation not in OPERATION_GATES:
            raise HarnessWorkflowRejected("operation_invalid")
        idempotency_key = _opaque("idempotency_key", idempotency_key)
        arguments_json = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                workflow = self._workflow(principal, workflow_id)
                if workflow["status"] != "running":
                    raise HarnessWorkflowRejected("workflow_not_running")
                required_gate = OPERATION_GATES[operation]
                approved = self._conn.execute(
                    "SELECT 1 FROM harness_gates WHERE workflow_id=? AND gate=? AND status='approved'",
                    (workflow_id, required_gate),
                ).fetchone()
                if approved is None:
                    raise HarnessWorkflowRejected("gate_not_approved")
                existing = self._conn.execute(
                    "SELECT operation,arguments_json,result_json FROM harness_operations WHERE workflow_id=? AND idempotency_key=?",
                    (workflow_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if existing["operation"] != operation or existing["arguments_json"] != arguments_json:
                        raise HarnessWorkflowRejected("idempotency_conflict")
                    result = json.loads(existing["result_json"])
                    if result.get("_harness_operation_status") == "pending":
                        raise HarnessWorkflowRejected("operation_outcome_unknown")
                    self._conn.commit()
                    return result
                self._conn.execute(
                    "INSERT INTO harness_operations (workflow_id,idempotency_key,operation,arguments_json,result_json,updated_at_ms) VALUES (?,?,?,?,?,?)",
                    (
                        workflow_id, idempotency_key, operation,
                        arguments_json,
                        '{"_harness_operation_status":"pending"}',
                        int(time.time() * 1000),
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            try:
                result = adapter(operation, dict(arguments))
                if (
                    not isinstance(result, dict)
                    or "_harness_operation_status" in result
                ):
                    raise HarnessWorkflowRejected("adapter_result_invalid")
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "UPDATE harness_operations SET result_json=? WHERE workflow_id=? AND idempotency_key=?",
                    (
                        json.dumps(result, ensure_ascii=False, sort_keys=True),
                        workflow_id,
                        idempotency_key,
                    ),
                )
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise
