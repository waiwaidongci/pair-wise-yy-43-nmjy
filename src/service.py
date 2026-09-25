from __future__ import annotations

from typing import Any, Dict, Optional

from .disposal_ledger import DisposalLedger, validate_record_fields
from .disposal_verification import verify_disposal
from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository, ledger: Optional[DisposalLedger] = None):
        self.repository = repository
        self.ledger = ledger or DisposalLedger(repository.conn, repository.lock)

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
            raise ConflictError("；".join(blockers))
        if target == "closed":
            verification = verify_disposal(item["quantity"],
                                           self.ledger.active_records(item_id))
            self.ledger.add_conclusion(
                item_id, "passed" if verification["ok"] else "failed", actor,
                verification=verification)
            if not verification["ok"]:
                raise ConflictError(
                    "处置核验未通过，缺少：" + "、".join(verification["missing_labels"]))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def add_disposal_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                            role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        self.repository.get_item(item_id)
        fields = validate_record_fields(
            payload.get("kind"), quantity=payload.get("quantity"),
            result=payload.get("result"), destination=payload.get("destination"),
            transfer_status=payload.get("transfer_status"),
            handler=payload.get("handler"), note=payload.get("note"))
        record = self.ledger.add_record(item_id, fields, actor)
        self.repository.append_audit("disposal_record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": record["kind"],
            "handler": record["handler"],
        })
        return record

    def list_disposal_records(self, item_id: int, role: str) -> list:
        self._view(role)
        self.repository.get_item(item_id)
        return self.ledger.list_records(item_id)

    def disposal_verification(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        item = self.repository.get_item(item_id)
        result = verify_disposal(item["quantity"], self.ledger.active_records(item_id))
        result["item_id"] = item_id
        result["item_status"] = item["status"]
        return result

    def list_disposal_conclusions(self, item_id: int, role: str) -> list:
        self._view(role)
        self.repository.get_item(item_id)
        return self.ledger.list_conclusions(item_id)

    def correct_disposal_record(self, record_id: int, payload: Dict[str, Any],
                                actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        old = self.ledger.get_record(record_id)
        fields = validate_record_fields(
            old["kind"],
            quantity=payload.get("quantity", old["quantity"]),
            result=payload.get("result", old["result"]),
            destination=payload.get("destination", old["destination"]),
            transfer_status=payload.get("transfer_status", old["transfer_status"]),
            handler=payload.get("handler", old["handler"]),
            note=payload.get("note", old["note"]))
        new_record = self.ledger.correct_record(record_id, fields, actor)
        item = self.repository.get_item(old["item_id"])
        reopened = False
        if item["status"] == "closed":
            self.repository.transition_item(item["id"], "pending_review",
                                            item["version"], actor)
            self.ledger.add_conclusion(
                item["id"], "reopened", actor,
                detail={"record_id": record_id, "new_record_id": new_record["id"]})
            self.repository.append_audit("transition", ENTITY, item["id"], actor, {
                "from": "closed", "to": "pending_review",
                "reason": "disposal_record_corrected", "record_id": record_id,
            })
            reopened = True
        self.repository.append_audit("disposal_correct", ENTITY, old["item_id"], actor, {
            "record_id": record_id, "new_record_id": new_record["id"],
            "reopened": reopened,
        })
        return {"record": new_record, "superseded_id": record_id,
                "reopened": reopened,
                "item_status": "pending_review" if reopened else item["status"]}

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
