"""SQLite-backed approval state store for Feishu card approvals.

This module is intentionally independent from ``frontends.fsapp`` so it can be
introduced safely before wiring. It addresses the main limitation of in-memory
approval state: a card may be created in one process while a callback is handled
by another process, or the process may restart before the user clicks.

The store keeps two logical states in a single table:

- ``pending``: approval is waiting for a card action.
- ``resolved`` / ``expired``: approval has a final result and can be replayed
  idempotently for duplicate callbacks.

No secrets are read here; callers provide the database path explicitly.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


PENDING = "pending"
RESOLVED = "resolved"
EXPIRED = "expired"


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    status: str
    session_key: str
    receive_id: str
    receive_id_type: str
    cmd: str
    reason: str
    requester_open_id: Optional[str]
    message_id: Optional[str]
    result: Optional[str]
    operator_open_id: Optional[str]
    operator_name: Optional[str]
    created_at: float
    expires_at: float
    resolved_at: Optional[float]
    metadata: Dict[str, Any]

    @property
    def is_pending(self) -> bool:
        return self.status == PENDING

    @property
    def is_done(self) -> bool:
        return self.status in {RESOLVED, EXPIRED}

    @property
    def is_expired_by_time(self) -> bool:
        return self.expires_at <= time.time()


@dataclass(frozen=True)
class ResolveResult:
    ok: bool
    record: Optional[ApprovalRecord]
    reason: str = ""


class ApprovalStore:
    """Small SQLite store with idempotent resolve semantics."""

    def __init__(self, db_path: str | Path, *, done_ttl_sec: int = 600) -> None:
        self.db_path = Path(db_path)
        self.done_ttl_sec = int(done_ttl_sec)
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    receive_id TEXT NOT NULL,
                    receive_id_type TEXT NOT NULL,
                    cmd TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    requester_open_id TEXT,
                    message_id TEXT,
                    result TEXT,
                    operator_open_id TEXT,
                    operator_name TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    resolved_at REAL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_approvals_expires_at ON approvals(expires_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_approvals_resolved_at ON approvals(resolved_at)")

    def create_pending(
        self,
        *,
        approval_id: str,
        session_key: str,
        receive_id: str,
        receive_id_type: str,
        cmd: str,
        reason: str,
        requester_open_id: Optional[str] = None,
        timeout_sec: int = 300,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ApprovalRecord:
        now = time.time()
        expires_at = now + max(1, int(timeout_sec))
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO approvals (
                    approval_id, status, session_key, receive_id, receive_id_type,
                    cmd, reason, requester_open_id, message_id, result,
                    operator_open_id, operator_name, created_at, expires_at,
                    resolved_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, NULL, ?)
                """,
                (
                    approval_id,
                    PENDING,
                    session_key,
                    receive_id,
                    receive_id_type,
                    cmd,
                    reason,
                    requester_open_id,
                    now,
                    expires_at,
                    metadata_json,
                ),
            )
        rec = self.get(approval_id)
        assert rec is not None
        return rec

    def set_message_id(self, approval_id: str, message_id: Optional[str]) -> Optional[ApprovalRecord]:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE approvals SET message_id = ? WHERE approval_id = ?",
                (message_id, approval_id),
            )
        return self.get(approval_id)

    def get(self, approval_id: str) -> Optional[ApprovalRecord]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def resolve(
        self,
        approval_id: str,
        result: str,
        *,
        operator_open_id: Optional[str] = None,
        operator_name: Optional[str] = None,
    ) -> ResolveResult:
        """Resolve once; duplicate callbacks replay the stored resolved record."""
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()
            if row is None:
                return ResolveResult(False, None, "not_found")
            rec = self._row_to_record(row)
            if rec.status == RESOLVED:
                return ResolveResult(True, rec, "already_resolved")
            if rec.status == EXPIRED:
                return ResolveResult(False, rec, "expired")
            if rec.expires_at <= now:
                conn.execute(
                    """
                    UPDATE approvals
                    SET status = ?, result = ?, resolved_at = ?
                    WHERE approval_id = ? AND status = ?
                    """,
                    (EXPIRED, "timeout", now, approval_id, PENDING),
                )
                expired = self._row_to_record(conn.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone())
                return ResolveResult(False, expired, "expired")
            conn.execute(
                """
                UPDATE approvals
                SET status = ?, result = ?, operator_open_id = ?, operator_name = ?, resolved_at = ?
                WHERE approval_id = ? AND status = ?
                """,
                (RESOLVED, result, operator_open_id, operator_name, now, approval_id, PENDING),
            )
            resolved = self._row_to_record(conn.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone())
            return ResolveResult(True, resolved, "resolved")

    def expire_pending(self, *, now: Optional[float] = None) -> int:
        now = time.time() if now is None else float(now)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE approvals
                SET status = ?, result = ?, resolved_at = ?
                WHERE status = ? AND expires_at <= ?
                """,
                (EXPIRED, "timeout", now, PENDING, now),
            )
            return int(cur.rowcount or 0)

    def cleanup_done(self, *, now: Optional[float] = None) -> int:
        now = time.time() if now is None else float(now)
        cutoff = now - self.done_ttl_sec
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM approvals WHERE status IN (?, ?) AND resolved_at IS NOT NULL AND resolved_at < ?",
                (RESOLVED, EXPIRED, cutoff),
            )
            return int(cur.rowcount or 0)

    def list_recent(self, *, limit: int = 50, statuses: Optional[Iterable[str]] = None) -> list[ApprovalRecord]:
        params: list[Any] = []
        where = ""
        if statuses:
            vals = list(statuses)
            where = "WHERE status IN (%s)" % ",".join("?" for _ in vals)
            params.extend(vals)
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM approvals {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ApprovalRecord:
        metadata_raw = row["metadata_json"] or "{}"
        try:
            metadata = json.loads(metadata_raw)
        except Exception:
            metadata = {"_decode_error": metadata_raw}
        return ApprovalRecord(
            approval_id=row["approval_id"],
            status=row["status"],
            session_key=row["session_key"],
            receive_id=row["receive_id"],
            receive_id_type=row["receive_id_type"],
            cmd=row["cmd"],
            reason=row["reason"],
            requester_open_id=row["requester_open_id"],
            message_id=row["message_id"],
            result=row["result"],
            operator_open_id=row["operator_open_id"],
            operator_name=row["operator_name"],
            created_at=float(row["created_at"]),
            expires_at=float(row["expires_at"]),
            resolved_at=float(row["resolved_at"]) if row["resolved_at"] is not None else None,
            metadata=metadata,
        )


__all__ = [
    "ApprovalRecord",
    "ApprovalStore",
    "ResolveResult",
    "PENDING",
    "RESOLVED",
    "EXPIRED",
]
