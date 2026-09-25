from __future__ import annotations

from typing import Any, Dict, List, Optional

from .audit import utc_now
from .domain import ConflictError, NotFoundError

ENTRY_STATUSES = ("active", "superseded")


class Ledger:
    """处置核验台账：三类核验记录与核验结论，结论留档不可改。"""

    def __init__(self, repository):
        self.repository = repository
        self.conn = repository.conn
        self._lock = repository._lock
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s + "'" for s in ENTRY_STATUSES)
        with self._lock, self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS disposal_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('recovery','shoreline','waste')),
                    recovery_quantity REAL,
                    shoreline_result TEXT,
                    inspected_at TEXT,
                    waste_destination TEXT,
                    handed_over INTEGER NOT NULL DEFAULT 0,
                    handler TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ({statuses})),
                    supersedes_id INTEGER,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_disposal_active_ref
                    ON disposal_entries(item_id, external_ref)
                    WHERE status='active' AND external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS disposal_conclusions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    outcome TEXT NOT NULL CHECK(outcome IN ('closed','reopened')),
                    verdict TEXT NOT NULL,
                    reason TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _entry(row) -> Dict[str, Any]:
        item = dict(row)
        item["handed_over"] = bool(item["handed_over"])
        return item

    def add_entry(self, item_id: int, kind: str, fields: Dict[str, Any],
                  handler: str, actor: str,
                  external_ref: Optional[str]) -> Dict[str, Any]:
        self.repository.get_item(item_id)
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO disposal_entries(item_id, kind, recovery_quantity,
                       shoreline_result, inspected_at, waste_destination, handed_over,
                       handler, status, supersedes_id, external_ref, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, kind, fields["recovery_quantity"], fields["shoreline_result"],
                     fields["inspected_at"], fields["waste_destination"],
                     1 if fields["handed_over"] else 0, handler, "active", None,
                     external_ref, actor, now),
                )
                entry_id = int(cur.lastrowid)
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise ConflictError("核验记录唯一标识已存在") from exc
            raise
        return self.get_entry(entry_id)

    def get_entry(self, entry_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM disposal_entries WHERE id=?", (entry_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("核验记录不存在")
        return self._entry(row)

    def find_active_entry(self, entry_id: int) -> Dict[str, Any]:
        entry = self.get_entry(entry_id)
        if entry["status"] != "active":
            raise ConflictError("该核验记录已更正，只能更正最新生效记录")
        return entry

    def list_entries(self, item_id: int) -> List[Dict[str, Any]]:
        self.repository.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM disposal_entries WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [self._entry(row) for row in rows]

    def list_active_entries(self, item_id: int) -> List[Dict[str, Any]]:
        self.repository.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM disposal_entries WHERE item_id=? AND status='active' ORDER BY id",
                (item_id,),
            ).fetchall()
        return [self._entry(row) for row in rows]

    def correct_entry(self, entry: Dict[str, Any], kind: str, fields: Dict[str, Any],
                      handler: str, actor: str,
                      external_ref: Optional[str]) -> Dict[str, Any]:
        """旧记录标为superseded留档，写入更正后的新记录。"""
        now = utc_now()
        old_id = entry["id"]
        item_id = entry["item_id"]
        try:
            with self._lock, self.conn:
                self.conn.execute(
                    "UPDATE disposal_entries SET status='superseded' WHERE id=?", (old_id,))
                cur = self.conn.execute(
                    """INSERT INTO disposal_entries(item_id, kind, recovery_quantity,
                       shoreline_result, inspected_at, waste_destination, handed_over,
                       handler, status, supersedes_id, external_ref, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, kind, fields["recovery_quantity"], fields["shoreline_result"],
                     fields["inspected_at"], fields["waste_destination"],
                     1 if fields["handed_over"] else 0, handler, "active", old_id,
                     external_ref, actor, now),
                )
                new_id = int(cur.lastrowid)
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise ConflictError("核验记录唯一标识已存在") from exc
            raise
        return self.get_entry(new_id)

    def add_conclusion(self, item_id: int, outcome: str, verdict: Dict[str, Any],
                       actor: str, reason: Optional[str] = None) -> Dict[str, Any]:
        import json
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO disposal_conclusions(item_id, outcome, verdict, reason,
                   created_by, created_at) VALUES(?,?,?,?,?,?)""",
                (item_id, outcome,
                 json.dumps(verdict, ensure_ascii=False, sort_keys=True),
                 reason, actor, now),
            )
            conclusion_id = int(cur.lastrowid)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM disposal_conclusions WHERE id=?", (conclusion_id,)
            ).fetchone()
        result = dict(row)
        result["verdict"] = verdict
        return result

    def latest_conclusion(self, item_id: int) -> Optional[Dict[str, Any]]:
        import json
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM disposal_conclusions WHERE item_id=? ORDER BY id DESC LIMIT 1",
                (item_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["verdict"] = json.loads(result["verdict"])
        return result
