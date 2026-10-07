"""SQLite-backed task queue and approval store.

Safe for several processes (WAL + BEGIN IMMEDIATE on claims) and for many asyncio
workers inside one process (a lock serialises the short synchronous calls).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

QUEUED, RUNNING, WAITING, DONE, FAILED = (
    "queued",
    "running",
    "waiting_approval",
    "done",
    "failed",
)
PENDING, APPROVED, DENIED = "pending", "approved", "denied"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  prompt TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 2,
  budget_usd REAL NOT NULL,
  cost_usd REAL NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  result TEXT,
  error TEXT,
  feedback TEXT,
  review_notes TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, id);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id),
  tool TEXT NOT NULL,
  tool_input TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  reason TEXT,
  created_at REAL NOT NULL,
  decided_at REAL
);
"""


@dataclass
class Task:
    id: int
    prompt: str
    status: str
    attempts: int
    max_attempts: int
    budget_usd: float
    cost_usd: float
    input_tokens: int
    output_tokens: int
    result: str | None
    error: str | None
    feedback: str | None
    review_notes: str | None
    created_at: float
    updated_at: float


@dataclass
class Approval:
    id: int
    task_id: int
    tool: str
    tool_input: dict
    status: str
    reason: str | None
    created_at: float
    decided_at: float | None


class Store:
    def __init__(self, path: str = "fleet.db") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ---- tasks -------------------------------------------------------------
    def add_task(self, prompt: str, budget_usd: float, max_attempts: int = 2) -> int:
        if budget_usd <= 0:
            raise ValueError("budget_usd must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        now = time.time()
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO tasks(prompt,budget_usd,max_attempts,created_at,updated_at)"
                " VALUES(?,?,?,?,?)",
                (prompt, budget_usd, max_attempts, now, now),
            )
            return int(cur.lastrowid)

    def get_task(self, task_id: int) -> Task | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return Task(**dict(row)) if row else None

    def list_tasks(self, status: str | None = None) -> list[Task]:
        q, args = "SELECT * FROM tasks", ()
        if status:
            q, args = q + " WHERE status=?", (status,)
        with self._lock:
            rows = self._conn.execute(q + " ORDER BY id", args).fetchall()
        return [Task(**dict(r)) for r in rows]

    def claim_next(self) -> Task | None:
        """Atomically move the oldest queued task to running and count the attempt."""
        with self._tx() as c:
            row = c.execute(
                "SELECT id FROM tasks WHERE status='queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            c.execute(
                "UPDATE tasks SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
                (time.time(), row["id"]),
            )
        return self.get_task(row["id"])

    def set_status(self, task_id: int, status: str) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                (status, time.time(), task_id),
            )

    def add_usage(self, task_id: int, input_tokens: int, output_tokens: int, cost: float) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE tasks SET input_tokens=input_tokens+?, output_tokens=output_tokens+?,"
                " cost_usd=cost_usd+?, updated_at=? WHERE id=?",
                (input_tokens, output_tokens, cost, time.time(), task_id),
            )

    def mark_done(self, task_id: int, result: str, review_notes: str) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE tasks SET status='done', result=?, review_notes=?, error=NULL,"
                " updated_at=? WHERE id=?",
                (result, review_notes, time.time(), task_id),
            )

    def fail_attempt(
        self, task_id: int, error: str, feedback: str | None = None, retry: bool = True
    ) -> str:
        """Record a failed attempt. Requeues if attempts remain, else marks failed.

        Returns the new status.
        """
        with self._tx() as c:
            row = c.execute(
                "SELECT attempts,max_attempts FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            again = retry and row["attempts"] < row["max_attempts"]
            status = QUEUED if again else FAILED
            c.execute(
                "UPDATE tasks SET status=?, error=?, feedback=?, updated_at=? WHERE id=?",
                (status, error, feedback, time.time(), task_id),
            )
        return status

    def requeue_interrupted(self) -> int:
        """Crash recovery: assumes a single runner process owns the queue."""
        with self._tx() as c:
            cur = c.execute(
                "UPDATE tasks SET status='queued', updated_at=? WHERE status IN (?,?)",
                (time.time(), RUNNING, WAITING),
            )
            c.execute(
                "UPDATE approvals SET status='denied', reason='runner restarted', decided_at=?"
                " WHERE status='pending'",
                (time.time(),),
            )
            return cur.rowcount

    # ---- approvals ---------------------------------------------------------
    def create_approval(self, task_id: int, tool: str, tool_input: dict) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO approvals(task_id,tool,tool_input,created_at) VALUES(?,?,?,?)",
                (task_id, tool, json.dumps(tool_input, default=str), time.time()),
            )
            return int(cur.lastrowid)

    def _approval(self, row: sqlite3.Row) -> Approval:
        d = dict(row)
        d["tool_input"] = json.loads(d["tool_input"])
        return Approval(**d)

    def get_approval(self, approval_id: int) -> Approval | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()
        return self._approval(row) if row else None

    def list_approvals(self, status: str | None = PENDING) -> list[Approval]:
        q, args = "SELECT * FROM approvals", ()
        if status:
            q, args = q + " WHERE status=?", (status,)
        with self._lock:
            rows = self._conn.execute(q + " ORDER BY id", args).fetchall()
        return [self._approval(r) for r in rows]

    def decide_approval(self, approval_id: int, approve: bool, reason: str | None = None) -> bool:
        """Decide a pending approval. Returns False if it was already decided."""
        with self._tx() as c:
            cur = c.execute(
                "UPDATE approvals SET status=?, reason=?, decided_at=?"
                " WHERE id=? AND status='pending'",
                (APPROVED if approve else DENIED, reason, time.time(), approval_id),
            )
            return cur.rowcount == 1
