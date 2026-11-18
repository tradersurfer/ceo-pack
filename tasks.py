"""Persistent task registry and lifecycle management (v0.5.0).

Pure stdlib + sqlite3. No network, no external deps.
Schema: tasks, task_history, task_dependencies, task_runs
"""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DB_NAME = "ceo_pack_tasks.db"
SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,                 -- UUID
    run_id TEXT NOT NULL,                -- UUID, groups tasks in an orchestration run
    task_key TEXT,                       -- optional idempotency key from caller
    seat TEXT NOT NULL,                  -- canonical seat: CEO, CFO, COO, CTO, CMO, CLO, Chief of Staff, CRO
    title TEXT NOT NULL,                 -- human-readable task title
    description TEXT,                    -- detailed description
    status TEXT NOT NULL DEFAULT 'proposed',  -- proposed | approved | assigned | running | blocked | completed | verified | failed
    priority INTEGER DEFAULT 0,          -- higher = more urgent
    owner TEXT,                          -- profile name or Hermes profile id bound to the seat
    model_route TEXT,                    -- model_map option name or profile_routes profile name
    due_date TEXT,                       -- ISO date YYYY-MM-DD
    created_at TEXT NOT NULL,            -- ISO datetime
    updated_at TEXT NOT NULL,            -- ISO datetime
    started_at TEXT,                     -- ISO datetime when moved to running
    completed_at TEXT,                   -- ISO datetime when completed/verified
    approved_at TEXT,                    -- ISO datetime when approved
    approved_by TEXT,                    -- who approved (owner/human)
    approval_required BOOLEAN DEFAULT 0, -- Type 1 gate
    risk_flags TEXT,                     -- JSON array of risk flags
    result_json TEXT,                    -- JSON string of tool result
    error_json TEXT,                     -- JSON string of error if failed
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 1,
    parent_task_id TEXT,                 -- for subtasks
    metadata TEXT                        -- JSON for extensible fields
);

CREATE INDEX IF NOT EXISTS idx_tasks_run_id ON tasks(run_id);
CREATE INDEX IF NOT EXISTS idx_tasks_seat ON tasks(seat);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_task_key ON tasks(task_key);

CREATE TABLE IF NOT EXISTS task_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    actor TEXT,                          -- who/what caused the transition
    note TEXT,
    at TEXT NOT NULL,                    -- ISO datetime
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_history_task_id ON task_history(task_id);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL,               -- the dependent task
    depends_on_task_id TEXT NOT NULL,    -- the prerequisite task
    PRIMARY KEY (task_id, depends_on_task_id),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (depends_on_task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_runs (
    run_id TEXT PRIMARY KEY,             -- UUID
    goal TEXT NOT NULL,                  -- the orchestration goal
    head_seat TEXT NOT NULL,             -- seat that owns the run
    model_key TEXT,                      -- model choice key
    status TEXT NOT NULL DEFAULT 'planned',  -- planned | running | completed | failed | blocked
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    plan_json TEXT                       -- full plan from plan_tool_calls
);
"""

DEFAULT_DB_PATH = Path.home() / ".hermes" / "plugins" / "ceo-pack" / DB_NAME


def _resolve_db_path(custom_path: Optional[str] = None) -> Path:
    if custom_path:
        return Path(custom_path)
    # Allow env override
    import os
    env_path = os.environ.get("CEO_PACK_TASK_DB")
    if env_path:
        return Path(env_path)
    return DEFAULT_DB_PATH


@contextmanager
def _connect(db_path: Optional[str] = None):
    path = _resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Optional[str] = None) -> None:
    """Initialize the task database. Idempotent."""
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # Ensure schema_version row exists
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,)
        )


# ---------- Task Runs ----------

def create_task_run(
    goal: str,
    head_seat: str,
    model_key: str = "free_default",
    db_path: Optional[str] = None
) -> str:
    """Create a new orchestration run. Returns run_id."""
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO task_runs (run_id, goal, head_seat, model_key, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'planned', ?, ?)""",
            (run_id, goal, head_seat, model_key, now, now)
        )
    return run_id


def get_task_run(run_id: str, db_path: Optional[str] = None) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM task_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    return dict(row) if row else None


def update_task_run_status(run_id: str, status: str, db_path: Optional[str] = None) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    completed_at = now if status in ("completed", "failed", "blocked") else None
    with _connect(db_path) as conn:
        if completed_at:
            conn.execute(
                "UPDATE task_runs SET status = ?, updated_at = ?, completed_at = ? WHERE run_id = ?",
                (status, now, completed_at, run_id)
            )
        else:
            conn.execute(
                "UPDATE task_runs SET status = ?, updated_at = ? WHERE run_id = ?",
                (status, now, run_id)
            )
        return conn.total_changes > 0


def set_task_run_plan(run_id: str, plan_json: str, db_path: Optional[str] = None) -> bool:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE task_runs SET plan_json = ?, updated_at = ? WHERE run_id = ?",
            (plan_json, datetime.now(timezone.utc).isoformat(), run_id)
        )
        return conn.total_changes > 0


# ---------- Tasks ----------

def create_task(
    run_id: str,
    seat: str,
    title: str,
    description: str = "",
    task_key: Optional[str] = None,
    due_date: Optional[str] = None,
    owner: Optional[str] = None,
    model_route: Optional[str] = None,
    approval_required: bool = False,
    risk_flags: Optional[list] = None,
    parent_task_id: Optional[str] = None,
    metadata: Optional[dict] = None,
    db_path: Optional[str] = None
) -> str:
    """Create a new task. Returns task_id (UUID)."""
    task_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO tasks (
                id, run_id, task_key, seat, title, description, status, priority,
                owner, model_route, due_date, created_at, updated_at,
                approval_required, risk_flags, parent_task_id, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, 'proposed', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id, run_id, task_key, seat, title, description,
                owner, model_route, due_date, now, now,
                approval_required, json.dumps(risk_flags or []), parent_task_id,
                json.dumps(metadata or {})
            )
        )
        _log_history(conn, task_id, None, "proposed", "system", "Task created")
    return task_id


def _log_history(
    conn: sqlite3.Connection,
    task_id: str,
    from_status: Optional[str],
    to_status: str,
    actor: str,
    note: str = ""
) -> None:
    conn.execute(
        """INSERT INTO task_history (task_id, from_status, to_status, actor, note, at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (task_id, from_status, to_status, actor, note, datetime.now(timezone.utc).isoformat())
    )


def get_task(task_id: str, db_path: Optional[str] = None) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    # Parse JSON fields
    for field in ("risk_flags", "metadata"):
        if d.get(field):
            try:
                d[field] = json.loads(d[field])
            except json.JSONDecodeError:
                d[field] = {}
    if d.get("result_json"):
        try:
            d["result"] = json.loads(d["result_json"])
        except json.JSONDecodeError:
            d["result"] = {}
    if d.get("error_json"):
        try:
            d["error"] = json.loads(d["error_json"])
        except json.JSONDecodeError:
            d["error"] = {}
    return d


def get_tasks_by_run(run_id: str, db_path: Optional[str] = None) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE run_id = ? ORDER BY created_at", (run_id,)
        ).fetchall()
    tasks = []
    for row in rows:
        d = dict(row)
        for field in ("risk_flags", "metadata"):
            if d.get(field):
                try:
                    d[field] = json.loads(d[field])
                except json.JSONDecodeError:
                    d[field] = {}
        tasks.append(d)
    return tasks


def get_tasks_by_seat(seat: str, status: Optional[str] = None, db_path: Optional[str] = None) -> list[dict]:
    with _connect(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE seat = ? AND status = ? ORDER BY created_at DESC",
                (seat, status)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE seat = ? ORDER BY created_at DESC", (seat,)
            ).fetchall()
    tasks = []
    for row in rows:
        d = dict(row)
        for field in ("risk_flags", "metadata"):
            if d.get(field):
                try:
                    d[field] = json.loads(d[field])
                except json.JSONDecodeError:
                    d[field] = {}
        tasks.append(d)
    return tasks


def update_task_status(
    task_id: str,
    new_status: str,
    actor: str = "system",
    note: str = "",
    result: Optional[dict] = None,
    error: Optional[dict] = None,
    db_path: Optional[str] = None
) -> bool:
    """Transition task to a new status. Returns True if changed."""
    valid_statuses = {"proposed", "approved", "assigned", "running", "blocked", "completed", "verified", "failed"}
    if new_status not in valid_statuses:
        raise ValueError(f"Invalid status: {new_status}. Valid: {valid_statuses}")

    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        # Get current status for history
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return False
        old_status = row["status"]
        if old_status == new_status:
            return False

        # Build update
        updates = ["status = ?", "updated_at = ?"]
        params = [new_status, now]

        if new_status == "running" and old_status != "running":
            updates.append("started_at = ?")
            params.append(now)
        if new_status in ("completed", "verified", "failed"):
            updates.append("completed_at = ?")
            params.append(now)
        if result is not None:
            updates.append("result_json = ?")
            params.append(json.dumps(result))
        if error is not None:
            updates.append("error_json = ?")
            params.append(json.dumps(error))

        params.append(task_id)
        conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", params)
        changed = conn.total_changes > 0
        if changed:
            _log_history(conn, task_id, old_status, new_status, actor, note)
    return True


def assign_task(task_id: str, owner: str, model_route: Optional[str] = None, db_path: Optional[str] = None) -> bool:
    """Assign task to an owner/profile and optionally a model route."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET owner = ?, model_route = ?, updated_at = ? WHERE id = ?",
            (owner, model_route, datetime.now(timezone.utc).isoformat(), task_id)
        )
        changed = conn.total_changes > 0
        if changed:
            _log_history(conn, task_id, None, "assigned", "system", f"Assigned to {owner}")
    return changed


def approve_task(task_id: str, approved_by: str, db_path: Optional[str] = None) -> bool:
    """Mark task as approved (for Type 1 gates)."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET status = 'approved', approved_at = ?, approved_by = ?, updated_at = ? WHERE id = ? AND status = 'proposed'",
            (now, approved_by, now, task_id)
        )
        changed = conn.total_changes > 0
        if changed:
            _log_history(conn, task_id, "proposed", "approved", approved_by, "Type 1 approval granted")
    return changed


def add_task_dependency(task_id: str, depends_on_task_id: str, db_path: Optional[str] = None) -> bool:
    """Add a dependency: task_id depends on depends_on_task_id."""
    with _connect(db_path) as conn:
        try:
            conn.execute(
                "INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (?, ?)",
                (task_id, depends_on_task_id)
            )
            return True
        except sqlite3.IntegrityError:
            return False  # already exists or invalid FK


def get_task_dependencies(task_id: str, db_path: Optional[str] = None) -> list[str]:
    """Get task IDs this task depends on."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ?", (task_id,)
        ).fetchall()
    return [r["depends_on_task_id"] for r in rows]


def get_dependent_tasks(task_id: str, db_path: Optional[str] = None) -> list[str]:
    """Get task IDs that depend on this task."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT task_id FROM task_dependencies WHERE depends_on_task_id = ?", (task_id,)
        ).fetchall()
    return [r["task_id"] for r in rows]


def are_dependencies_met(task_id: str, db_path: Optional[str] = None) -> bool:
    """Check if all dependencies are completed/verified."""
    deps = get_task_dependencies(task_id, db_path)
    if not deps:
        return True
    for dep_id in deps:
        dep = get_task(dep_id, db_path)
        if not dep or dep["status"] not in ("completed", "verified"):
            return False
    return True


def increment_retry(task_id: str, db_path: Optional[str] = None) -> int:
    """Increment retry count, return new count."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET retry_count = retry_count + 1, updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), task_id)
        )
        row = conn.execute("SELECT retry_count FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row["retry_count"] if row else 0


def get_task_history(task_id: str, db_path: Optional[str] = None) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM task_history WHERE task_id = ? ORDER BY at", (task_id,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- Query helpers ----------

def get_pending_approvals(db_path: Optional[str] = None) -> list[dict]:
    """Tasks awaiting owner approval (Type 1)."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE approval_required = 1 AND status = 'proposed' ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def get_running_tasks(db_path: Optional[str] = None) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status = 'running' ORDER BY started_at"
        ).fetchall()
    return [dict(r) for r in rows]


def get_stuck_tasks(db_path: Optional[str] = None) -> list[dict]:
    """Tasks that are blocked or failed with retries exhausted."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE status IN ('blocked', 'failed')
               AND (retry_count >= max_retries OR max_retries = 0)
               ORDER BY updated_at"""
        ).fetchall()
    return [dict(r) for r in rows]