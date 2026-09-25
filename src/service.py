from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import ensure_role, normalize_severity, require_number, require_text
from .ledger import Ledger
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)
from .verification import (KIND_LABELS, build_fields, ensure_closable, evaluate,
                           merge_fields, normalize_kind)

VERIFY_ROLES = RECORD_ROLES


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository
        self.ledger = Ledger(repository)

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    # ------------------------------------------------------------------
    # 处置核验台账
    # ------------------------------------------------------------------
    def register_verification(self, item_id: int, payload: Dict[str, Any],
                              actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, VERIFY_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = normalize_kind(payload.get("kind"))
        handler = require_text(payload.get("handler"), "handler", 100)
        fields = build_fields(kind, payload)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        entry = self.ledger.add_entry(item_id, kind, fields, handler, actor,
                                      external_ref)
        self.repository.append_audit("verification_register", "处置核验记录",
                                     entry["id"], actor, {
                                         "item_id": item_id, "kind": kind,
                                         "kind_label": KIND_LABELS[kind],
                                         "handler": handler,
                                     })
        return entry

    def correct_verification(self, entry_id: int, payload: Dict[str, Any],
                             actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, VERIFY_ROLES)
        actor = require_text(actor, "actor", 100)
        old = self.ledger.find_active_entry(entry_id)
        kind = old["kind"]
        handler = payload.get("handler")
        handler = require_text(handler, "handler", 100) if handler is not None \
            else old["handler"]
        fields = merge_fields(kind, old, payload)
        external_ref = payload.get("external_ref")
        if external_ref is None:
            external_ref = old["external_ref"]
        else:
            external_ref = require_text(external_ref, "external_ref", 100)
        reason = require_text(payload.get("reason"), "reason", 500)
        new = self.ledger.correct_entry(old, kind, fields, handler, actor,
                                        external_ref)
        item_id = new["item_id"]
        item = self.repository.get_item(item_id)
        reopened = item["status"] == "closed"
        if reopened:
            # 原始记录更正后，已关闭事件回到待复核，旧结论继续留档
            self.repository.reopen_to_monitoring(item_id, actor)
        verdict = evaluate(self.ledger.list_active_entries(item_id),
                           item["quantity"])
        if reopened:
            self.ledger.add_conclusion(item_id, "reopened", verdict, actor, reason)
        self.repository.append_audit("verification_correct", "处置核验记录",
                                     new["id"], actor, {
                                         "item_id": item_id, "kind": kind,
                                         "supersedes_id": old["id"],
                                         "reason": reason, "reopened": reopened,
                                     })
        if reopened:
            self.repository.append_audit("reopen", ENTITY, item_id, actor, {
                "from": "closed", "to": "monitoring", "reason": reason,
                "missing": verdict["missing"],
            })
        return new

    def verification_status(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        item = self.repository.get_item(item_id)
        verdict = evaluate(self.ledger.list_active_entries(item_id),
                           item["quantity"])
        verdict["item_id"] = item_id
        verdict["latest_conclusion"] = self.ledger.latest_conclusion(item_id)
        return verdict

    def list_verifications(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.ledger.list_entries(item_id)

    # ------------------------------------------------------------------
    # 状态流转
    # ------------------------------------------------------------------
    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        if target == "closed":
            # 回收量达八成、最近一次岸线复查正常、废弃物全部交接后才能关闭
            verdict = ensure_closable(self.ledger.list_active_entries(item_id),
                                      item["quantity"])
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        if target == "closed":
            self.ledger.add_conclusion(item_id, "closed", verdict, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
            "verification_ready": True if target == "closed" else None,
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
