"""处置核验台账：回收数量、岸线复查、废弃物去向三类记录及核验结论留档。

三类记录均关联事件并登记经办人；更正采用"作废旧记录+写入新记录"，
旧记录与历史核验结论一律留档，核验只统计当前有效记录。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from .audit import utc_now
from .domain import (ConflictError, NotFoundError, ValidationError,
                     require_number, require_text)

RECORD_KINDS = ("recovery", "shoreline", "waste")
KIND_LABELS = {"recovery": "回收数量", "shoreline": "岸线复查", "waste": "废弃物去向"}
SHORELINE_RESULTS = ("normal", "abnormal")
TRANSFER_STATUSES = ("pending", "completed")
CONCLUSION_OUTCOMES = ("passed", "failed", "reopened")


def validate_record_fields(kind: Any, quantity: Any = None, result: Any = None,
                           destination: Any = None, transfer_status: Any = None,
                           handler: Any = None, note: Any = None) -> Dict[str, Any]:
    """按记录类型校验并规范化字段；不适用字段一律置空。"""
    if kind not in RECORD_KINDS:
        raise ValidationError("kind必须是recovery、shoreline或waste")
    cleaned = {"kind": kind, "handler": require_text(handler, "handler", 100),
               "quantity": None, "result": None, "destination": None,
               "transfer_status": None, "note": None}
    if note is not None:
        cleaned["note"] = require_text(note, "note", 500)
    if kind == "recovery":
        cleaned["quantity"] = require_number(quantity, "quantity")
    elif kind == "shoreline":
        if result not in SHORELINE_RESULTS:
            raise ValidationError("result必须是normal或abnormal")
        cleaned["result"] = result
    else:
        cleaned["destination"] = require_text(destination, "destination", 200)
        status = transfer_status if transfer_status is not None else "pending"
        if status not in TRANSFER_STATUSES:
            raise ValidationError("transfer_status必须是pending或completed")
        cleaned["transfer_status"] = status
    return cleaned


class DisposalLedger:
    """台账存储，与主仓库共用连接和锁，保证同事务一致性。"""

    def __init__(self, conn: sqlite3.Connection, lock) -> None:
        self.conn = conn
        self._lock = lock
        self._create_schema()

    def _create_schema(self) -> None:
        kinds = ",".join("'" + k + "'" for k in RECORD_KINDS)
        outcomes = ",".join("'" + o + "'" for o in CONCLUSION_OUTCOMES)
        with self._lock, self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS disposal_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ({kinds})),
                    quantity REAL,
                    result TEXT,
                    destination TEXT,
                    transfer_status TEXT,
                    handler TEXT NOT NULL,
                    note TEXT,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','superseded')),
                    corrects_id INTEGER REFERENCES disposal_records(id),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_disposal_records_item
                    ON disposal_records(item_id, kind);
                CREATE TABLE IF NOT EXISTS disposal_conclusions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    outcome TEXT NOT NULL CHECK(outcome IN ({outcomes})),
                    missing TEXT NOT NULL DEFAULT '[]',
                    recovery_total REAL,
                    required_quantity REAL,
                    shoreline_result TEXT,
                    waste_pending INTEGER,
                    detail TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_disposal_conclusions_item
                    ON disposal_conclusions(item_id);
            """)

    def add_record(self, item_id: int, fields: Dict[str, Any], actor: str,
                   corrects_id: Optional[int] = None) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO disposal_records(item_id, kind, quantity, result,
                   destination, transfer_status, handler, note, status, corrects_id,
                   created_by, created_at) VALUES(?,?,?,?,?,?,?,?,'active',?,?,?)""",
                (item_id, fields["kind"], fields["quantity"], fields["result"],
                 fields["destination"], fields["transfer_status"], fields["handler"],
                 fields["note"], corrects_id, actor, now),
            )
            record_id = int(cur.lastrowid)
        return self.get_record(record_id)

    def get_record(self, record_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM disposal_records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFoundError("处置记录不存在")
        return dict(row)

    def correct_record(self, record_id: int, fields: Dict[str, Any],
                       actor: str) -> Dict[str, Any]:
        """更正原始记录：旧记录标记superseded留档，新记录指向旧记录。"""
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM disposal_records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFoundError("处置记录不存在")
            old = dict(row)
            if old["status"] != "active":
                raise ConflictError("记录已被更正，请基于最新记录操作")
            self.conn.execute(
                "UPDATE disposal_records SET status='superseded' WHERE id=?",
                (record_id,))
            cur = self.conn.execute(
                """INSERT INTO disposal_records(item_id, kind, quantity, result,
                   destination, transfer_status, handler, note, status, corrects_id,
                   created_by, created_at) VALUES(?,?,?,?,?,?,?,?,'active',?,?,?)""",
                (old["item_id"], fields["kind"], fields["quantity"], fields["result"],
                 fields["destination"], fields["transfer_status"], fields["handler"],
                 fields["note"], record_id, actor, now),
            )
            new_id = int(cur.lastrowid)
        return self.get_record(new_id)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        """台账全量记录，含已被更正的旧记录（留档）。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM disposal_records WHERE item_id=? ORDER BY id",
                (item_id,)).fetchall()
        return [dict(row) for row in rows]

    def active_records(self, item_id: int) -> List[Dict[str, Any]]:
        """当前有效记录，按登记顺序，供核验使用。"""
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM disposal_records
                   WHERE item_id=? AND status='active' ORDER BY id""",
                (item_id,)).fetchall()
        return [dict(row) for row in rows]

    def add_conclusion(self, item_id: int, outcome: str, actor: str,
                       verification: Optional[Dict[str, Any]] = None,
                       detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if outcome not in CONCLUSION_OUTCOMES:
            raise ValidationError("未知结论类型")
        missing = verification["missing"] if verification else []
        recovery_total = verification["recovery"]["total"] if verification else None
        required = verification["required_quantity"] if verification else None
        shoreline_result = verification["shoreline"]["latest_result"] if verification else None
        waste_pending = verification["waste"]["pending"] if verification else None
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO disposal_conclusions(item_id, outcome, missing,
                   recovery_total, required_quantity, shoreline_result, waste_pending,
                   detail, created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (item_id, outcome, json.dumps(missing, ensure_ascii=False),
                 recovery_total, required, shoreline_result, waste_pending,
                 json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail else None,
                 actor, now),
            )
            conclusion_id = int(cur.lastrowid)
            row = self.conn.execute(
                "SELECT * FROM disposal_conclusions WHERE id=?",
                (conclusion_id,)).fetchone()
        return self._conclusion(row)

    def list_conclusions(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM disposal_conclusions WHERE item_id=? ORDER BY id",
                (item_id,)).fetchall()
        return [self._conclusion(row) for row in rows]

    @staticmethod
    def _conclusion(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["missing"] = json.loads(item["missing"])
        item["detail"] = json.loads(item["detail"]) if item["detail"] else None
        return item
