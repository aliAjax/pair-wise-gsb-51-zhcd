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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    branch TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
                    from_branch TEXT NOT NULL,
                    to_branch TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    initiated_by TEXT NOT NULL,
                    confirmed_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_transfers_record ON transfers(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_transfers_incoming ON transfers(to_branch, status);
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(records)")}
            if "branch" not in columns:
                connection.execute("ALTER TABLE records ADD COLUMN branch TEXT NOT NULL DEFAULT ''")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_records_branch ON records(branch)")

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str, branch: str = "") -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,branch,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (reference, state, branch, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state, "branch": branch}, ensure_ascii=False, sort_keys=True), now),
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

    def list_records(self, state: Optional[str] = None, limit: int = 100, branch: Optional[str] = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses = []
        params: List[Any] = []
        if state:
            clauses.append("state=?")
            params.append(state)
        if branch is not None:
            clauses.append("branch=?")
            params.append(branch)
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

    def stats(self, branch: Optional[str] = None) -> Dict[str, int]:
        with self._connect() as connection:
            if branch is None:
                rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
            else:
                rows = connection.execute("SELECT state, COUNT(*) AS total FROM records WHERE branch=? GROUP BY state", (branch,)).fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def create_transfer(self, record_id: int, from_branch: str, to_branch: str, reason: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            pending = connection.execute("SELECT id FROM transfers WHERE record_id=? AND status='pending'", (record_id,)).fetchone()
            if pending is not None:
                connection.rollback()
                raise Conflict("已存在待确认的转办")
            cursor = connection.execute(
                "INSERT INTO transfers(record_id,from_branch,to_branch,reason,status,initiated_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (record_id, from_branch, to_branch, reason, "pending", actor_id, now),
            )
            transfer_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "transfer_initiated", actor_id, int(record["version"]), json.dumps({"transfer_id": transfer_id, "from_branch": from_branch, "to_branch": to_branch, "reason": reason}, ensure_ascii=False, sort_keys=True), now),
            )
            row = connection.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            connection.commit()
        return dict(row)

    def get_transfer(self, transfer_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
        if row is None:
            raise NotFound("转办不存在")
        return dict(row)

    def list_transfers(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM transfers WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_incoming_transfers(self, to_branch: str, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT transfers.*, records.reference AS reference, records.state AS record_state FROM transfers JOIN records ON records.id = transfers.record_id WHERE transfers.to_branch=? AND transfers.status='pending' ORDER BY transfers.id DESC LIMIT ?",
                (to_branch, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def confirm_transfer(self, transfer_id: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            transfer = connection.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if transfer is None:
                connection.rollback()
                raise NotFound("转办不存在")
            cursor = connection.execute(
                "UPDATE transfers SET status='confirmed', confirmed_by=?, confirmed_at=? WHERE id=? AND status='pending'",
                (actor_id, now, transfer_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise Conflict("转办已被处理，仅最先提交的确认生效")
            record = connection.execute("SELECT version FROM records WHERE id=?", (transfer["record_id"],)).fetchone()
            version = int(record["version"]) + 1
            connection.execute(
                "UPDATE records SET branch=?, version=?, updated_by=?, updated_at=? WHERE id=?",
                (transfer["to_branch"], version, actor_id, now, transfer["record_id"]),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (transfer["record_id"], "transfer_confirmed", actor_id, version, json.dumps({"transfer_id": transfer_id, "from_branch": transfer["from_branch"], "to_branch": transfer["to_branch"]}, ensure_ascii=False, sort_keys=True), now),
            )
            row = connection.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            connection.commit()
        return dict(row)

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
