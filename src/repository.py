"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)").fetchall()}
            if columns and "org" not in columns:
                connection.execute("ALTER TABLE records ADD COLUMN org TEXT NOT NULL DEFAULT ''")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    org TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    from_org TEXT NOT NULL,
                    to_org TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    initiated_by TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    confirmed_by TEXT,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_records_org ON records(org);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_transfers_pending ON transfers(record_id) WHERE status='pending';
                CREATE INDEX IF NOT EXISTS idx_transfers_to ON transfers(to_org, status);
                CREATE INDEX IF NOT EXISTS idx_transfers_from ON transfers(from_org);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str, org: str = "") -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at,org) VALUES(?,?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now, org),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100, org: Optional[str] = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses = []
        params: List[Any] = []
        if state:
            clauses.append("state=?")
            params.append(state)
        if org is not None:
            clauses.append("org=?")
            params.append(org)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM records%s ORDER BY id DESC LIMIT ?" % where, params).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self, org: Optional[str] = None) -> Dict[str, int]:
        with self._connect() as connection:
            if org is None:
                rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
            else:
                rows = connection.execute("SELECT state, COUNT(*) AS total FROM records WHERE org=? GROUP BY state", (org,)).fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def initiate_transfer(self, record_id: int, from_org: str, to_org: str, reason: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
                if row is None:
                    connection.rollback()
                    raise NotFound("记录不存在")
                cursor = connection.execute(
                    "INSERT INTO transfers(record_id,from_org,to_org,reason,initiated_by,status,created_at) VALUES(?,?,?,?,?,'pending',?)",
                    (record_id, from_org, to_org, reason, actor_id, now),
                )
                transfer_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "transfer_initiated", actor_id, int(row["version"]),
                     json.dumps({"transfer_id": transfer_id, "from_org": from_org, "to_org": to_org, "reason": reason}, ensure_ascii=False, sort_keys=True), now),
                )
                transfer = connection.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("该记录已存在待确认的转办单") from exc
        return dict(transfer)

    def get_transfer(self, transfer_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
        if row is None:
            raise NotFound("转办单不存在")
        return dict(row)

    def confirm_transfer(self, transfer_id: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            transfer = connection.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if transfer is None:
                connection.rollback()
                raise NotFound("转办单不存在")
            cursor = connection.execute(
                "UPDATE transfers SET status='confirmed', confirmed_by=?, confirmed_at=? WHERE id=? AND status='pending'",
                (actor_id, now, transfer_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise Conflict("转办单已被确认，仅先提交的生效")
            row = connection.execute("SELECT * FROM records WHERE id=?", (transfer["record_id"],)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            version = int(row["version"]) + 1
            connection.execute(
                "UPDATE records SET org=?,version=?,updated_by=?,updated_at=? WHERE id=?",
                (transfer["to_org"], version, actor_id, now, transfer["record_id"]),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (transfer["record_id"], "transfer_confirmed", actor_id, version,
                 json.dumps({"transfer_id": transfer_id, "from_org": transfer["from_org"], "to_org": transfer["to_org"],
                             "reason": transfer["reason"], "initiated_by": transfer["initiated_by"]}, ensure_ascii=False, sort_keys=True), now),
            )
            updated_transfer = connection.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            updated_record = connection.execute("SELECT * FROM records WHERE id=?", (transfer["record_id"],)).fetchone()
            connection.commit()
        return {"transfer": dict(updated_transfer), "record": self._row(updated_record)}

    def list_transfers(self, org: str, direction: str) -> List[Dict[str, Any]]:
        column = "to_org" if direction == "incoming" else "from_org"
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT t.*, r.reference AS reference FROM transfers t JOIN records r ON r.id=t.record_id WHERE t.%s=? ORDER BY t.id DESC" % column,
                (org,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_transfers_for_record(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM transfers WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [dict(row) for row in rows]

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
