"""Persistent task registry and lifecycle management (v0.5.0).

Pure stdlib + sqlite3. No network, no external deps.
Schema: tasks, task_history, task_dependencies, task_runs,
approval_tokens, gate_audit, tool_intents (v2, Type 1 gate).
"""

import hashlib
import json
import logging
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DB_NAME = "ceo_pack_tasks.db"
# Registry schema version. v2 adds the Type 1 gate tables (approval_tokens,
# gate_audit, tool_intents) and the action_tool / action_args_hash columns
# on tasks. Migrations are additive only; see _migrate().
SCHEMA_VERSION = 2

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
    status TEXT NOT NULL DEFAULT 'proposed',  -- proposed | approved | assigned | running | blocked | completed | verified | failed | rejected
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
    metadata TEXT,                       -- JSON for extensible fields
    action_tool TEXT,                    -- exact host tool the task will execute (Type 1 gate, Stage 1)
    action_args_hash TEXT                -- SHA-256 hex of the canonical arguments (hash only, never values)
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

-- Type 1 approval gate (Stage 1, warn/observe only). Argument VALUES are
-- never stored here — only SHA-256 hashes of the canonical arguments.
CREATE TABLE IF NOT EXISTS approval_tokens (
    token_id TEXT PRIMARY KEY,           -- UUID, one-time approval ticket
    task_id TEXT NOT NULL,               -- ceo-pack task this approval belongs to
    tool_name TEXT NOT NULL,             -- exact host tool covered
    args_hash TEXT NOT NULL,             -- SHA-256 hex of canonical arguments
    approved_by TEXT NOT NULL,           -- who approved (stored as given)
    approved_at TEXT NOT NULL,           -- ISO datetime of minting
    expires_at TEXT NOT NULL,            -- ISO datetime; token dead after this
    used_at TEXT,                        -- ISO datetime of the single use, NULL until used
    status TEXT NOT NULL DEFAULT 'live', -- live | used | expired | revoked
    minted_via TEXT NOT NULL             -- "chat" or "dashboard"
);

CREATE INDEX IF NOT EXISTS idx_tokens_tool_hash ON approval_tokens(tool_name, args_hash);
CREATE INDEX IF NOT EXISTS idx_tokens_task ON approval_tokens(task_id);

CREATE TABLE IF NOT EXISTS gate_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,                    -- ISO datetime of the decision
    decision TEXT NOT NULL,              -- allowed | blocked | would_block | would_allow | token_minted | token_revoked | hook_error
    tool_name TEXT,                      -- tool that was called (NULL for mint/revoke events)
    args_hash TEXT,                      -- SHA-256 of canonical args; never the args themselves
    task_id TEXT,                        -- from the matched token, else NULL
    token_id TEXT,                       -- token that authorized the call, else NULL
    approved_by TEXT,                    -- from the token, else NULL
    actor TEXT,                          -- "gate" for hook decisions; approver for mint/revoke events
    session_id TEXT,                     -- host session id from hook kwargs, when present
    reason TEXT,                         -- token_match | no_token | token_expired | token_used | token_revoked | modified_arguments | registry_unavailable | hook_error | uncertain_classification
    mode TEXT NOT NULL                   -- enforcement mode live at decision time ("warn" in Stage 1)
);

CREATE INDEX IF NOT EXISTS idx_audit_at ON gate_audit(at);

CREATE TABLE IF NOT EXISTS tool_intents (
    intent_id TEXT PRIMARY KEY,          -- UUID
    task_id TEXT NOT NULL,               -- ceo-pack task
    tool_name TEXT NOT NULL,
    args_hash TEXT NOT NULL,             -- SHA-256 hex of canonical arguments
    created_at TEXT NOT NULL,            -- ISO datetime
    session_id TEXT                      -- host session id, when present
);

CREATE INDEX IF NOT EXISTS idx_intents_tool_hash ON tool_intents(tool_name, args_hash);
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
    # WAL + a real busy timeout: the agent process and the dashboard
    # web-server process both write this DB now (dashboard POST routes).
    # WAL lets readers proceed while a writer holds the DB; the timeout
    # turns a still-contended write into a quick, catchable error instead
    # of an indefinite hang.
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for registries created by an older plugin.
    Fresh DBs already match SCHEMA; this only upgrades existing ones.
    Never drops or rewrites anything."""
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    version = row[0] if row else 0
    if version < 2:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "action_tool" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN action_tool TEXT")
        if "action_args_hash" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN action_args_hash TEXT")
        # Exactly one version row: delete + insert, never UPDATE — an
        # old registry upgraded by an earlier init_db shape could hold
        # two rows, and UPDATE would collide on the PRIMARY KEY.
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (2)")


def init_db(db_path: Optional[str] = None) -> None:
    """Initialize the task database. Idempotent."""
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # Ensure exactly one schema_version row exists; the migration
        # below upgrades it in place. Do NOT INSERT OR IGNORE the new
        # version here: on an old registry that would leave two rows
        # (old + new) and break the upgrade with a UNIQUE constraint
        # failure.
        row = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
        if row[0] == 0:
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,)
            )
        _migrate(conn)


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
    tool_name: Optional[str] = None,
    arguments: Optional[dict] = None,
    db_path: Optional[str] = None
) -> str:
    """Create a new task. Returns task_id (UUID).

    Idempotent on task_key within a run: creating twice with the same
    (run_id, task_key) returns the existing task id instead of a
    duplicate row.

    Stage 1 gate: tool_name + arguments record the exact action this
    task will execute (tool name + SHA-256 of the canonical arguments;
    raw values are never stored). Approving the task later mints a
    one-time ticket for that exact action. Invalid action params are
    ignored (the task is still created, with no action recorded).
    """
    if task_key is not None:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT id FROM tasks WHERE run_id = ? AND task_key = ?",
                (run_id, task_key)
            ).fetchone()
            if row:
                return row["id"]
    task_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    action_tool, action_hash = _action_pair(tool_name, arguments)
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO tasks (
                id, run_id, task_key, seat, title, description, status, priority,
                owner, model_route, due_date, created_at, updated_at,
                approval_required, risk_flags, parent_task_id, metadata,
                action_tool, action_args_hash
            ) VALUES (?, ?, ?, ?, ?, ?, 'proposed', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id, run_id, task_key, seat, title, description,
                owner, model_route, due_date, now, now,
                approval_required, json.dumps(risk_flags or []), parent_task_id,
                json.dumps(metadata or {}), action_tool, action_hash
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


# Legal lifecycle transitions (plugin.yaml). update_task_status enforces
# this table: stage skips, backwards moves, and any move out of a
# terminal state are refused (return False, no change, no history).
# proposed -> approved is the owner-approval step; approve_task() is the
# usual path, but the raw transition is legal too (concurrent-update
# tests drive proposed -> approved through here).
_LEGAL_TRANSITIONS = {
    ("proposed", "approved"),
    ("proposed", "rejected"),
    ("approved", "assigned"),
    ("assigned", "running"),
    ("running", "blocked"),
    ("running", "completed"),
    ("running", "failed"),
    ("blocked", "running"),
    ("blocked", "failed"),
    ("completed", "verified"),
    ("failed", "running"),
}


def update_task_status(
    task_id: str,
    new_status: str,
    actor: str = "system",
    note: str = "",
    result: Optional[dict] = None,
    error: Optional[dict] = None,
    db_path: Optional[str] = None
) -> bool:
    """Transition task to a new status. Returns True if changed.

    Illegal transitions (not in _LEGAL_TRANSITIONS) return False and
    leave the row untouched; unknown statuses raise ValueError.
    """
    valid_statuses = {"proposed", "approved", "assigned", "running", "blocked", "completed", "verified", "failed", "rejected"}
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
        if (old_status, new_status) not in _LEGAL_TRANSITIONS:
            return False

        # Build update
        updates = ["status = ?", "updated_at = ?"]
        params = [new_status, now]

        if new_status == "running" and old_status != "running":
            updates.append("started_at = ?")
            params.append(now)
        if new_status in ("completed", "verified", "failed", "rejected"):
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
            if new_status == "rejected":
                # Rejecting revokes the task's live tickets: a rejected
                # action must never be executable on a stale approval.
                _revoke_tokens_for_task_conn(conn, task_id, actor)
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


def move_to_assigned(task_id: str, actor: str = "system", note: str = "", db_path: Optional[str] = None) -> bool:
    """Assign-operation transition: proposed/approved -> assigned.

    ceo_task_assign binds an owner AND activates the task in one step, so
    it legitimately moves a merely-proposed task to assigned. The raw
    update_task_status() table still refuses proposed -> assigned (a stage
    skip); this is the assign operation's own path, not the raw one.
    Returns True if the task moved to assigned.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row or row["status"] not in ("proposed", "approved"):
            return False
        old_status = row["status"]
        conn.execute(
            "UPDATE tasks SET status = 'assigned', updated_at = ? WHERE id = ?",
            (now, task_id)
        )
        changed = conn.total_changes > 0
        if changed:
            _log_history(conn, task_id, old_status, "assigned", actor, note)
        return changed


def approve_task(task_id: str, approved_by: str, note: Optional[str] = None, tool_name: Optional[str] = None, arguments: Optional[dict] = None, minted_via: str = "chat", db_path: Optional[str] = None) -> bool:
    """Mark task as approved (for Type 1 gates). Returns True when the
    status moved to approved.

    Stage 1 gate: approving also mints a one-time approval ticket when
    the task names the exact action — either the task row's recorded
    action_tool / action_args_hash, or the tool_name + arguments passed
    here (hashed immediately; raw values are never stored). A task with
    no recorded action still flips to approved, but NO ticket is minted:
    approving alone authorizes nothing. Minting is idempotent per
    (task, tool, args), in the same transaction as the approval."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT action_tool, action_args_hash FROM tasks WHERE id = ? AND status = 'proposed'",
            (task_id,),
        ).fetchone()
        if not row:
            return False
        action_tool, action_hash = _action_pair(tool_name, arguments)
        if action_tool is None and row["action_tool"] and row["action_args_hash"]:
            action_tool = row["action_tool"]
            action_hash = row["action_args_hash"]
        conn.execute(
            "UPDATE tasks SET status = 'approved', approved_at = ?, approved_by = ?, updated_at = ?, action_tool = COALESCE(?, action_tool), action_args_hash = COALESCE(?, action_args_hash) WHERE id = ? AND status = 'proposed'",
            (now, approved_by, now, action_tool, action_hash, task_id)
        )
        changed = conn.total_changes > 0
        if changed:
            _log_history(conn, task_id, "proposed", "approved", approved_by, note or "Type 1 approval granted")
            if action_tool and action_hash:
                _mint_token_conn(conn, task_id, action_tool, action_hash, approved_by, minted_via)
    return changed


def complete_task(task_id: str, actor: str = "system", note: str = "", db_path: Optional[str] = None) -> bool:
    """Mark a running task completed. Completed is NOT verified: the
    legal-transition table allows completed only from running, and nothing
    here touches the verified state — verification is a separate step
    (completed -> verified) and has no tool yet. Returns True when the
    task moved to completed."""
    return update_task_status(task_id, "completed", actor=actor, note=note, db_path=db_path)


def try_assign_task(task_id: str, owner: str, model_route: Optional[str] = None, actor: str = "system", db_path: Optional[str] = None) -> tuple:
    """Assign with duplicate protection. Returns (outcome, task_or_None).

    Outcomes:
    - "assigned": fresh assign (proposed/approved -> assigned, owner set).
    - "already_assigned": the task is already assigned to this owner —
      idempotent success, nothing changed, no duplicate work, no history
      row (a no-op is not an event).
    - "conflict": the task is already assigned to a DIFFERENT owner —
      refused, owner unchanged, no silent overwrite.
    - "refused": status cannot be assigned (running/blocked/failed or any
      terminal state) — the legal-transition table is the authority.
    - "not_found": no such task.
    One transaction: the read, the decision, and the write happen together,
    so a concurrent assign cannot slip between the check and the write.
    Assign stays staging only, never a security boundary (Type 1 gate
    design): this stops duplicate work, it authorizes nothing.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return "not_found", None
        status = row["status"]
        current_owner = row["owner"]
        if status == "assigned":
            if current_owner == owner:
                return "already_assigned", dict(row)
            return "conflict", dict(row)
        if status not in ("proposed", "approved"):
            return "refused", dict(row)
        conn.execute(
            "UPDATE tasks SET owner = ?, model_route = ?, status = 'assigned', updated_at = ? WHERE id = ?",
            (owner, model_route, now, task_id)
        )
        _log_history(conn, task_id, status, "assigned", actor, "Assigned to %s" % owner)
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return "assigned", dict(row)


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


# ---------- Delegated execution records (v0.5.0 Phase 3) ----------
# The Hermes host reports per-task execution metadata on a delegate_task
# result entry (tools/delegate_tool_child_run.py): route, tokens, cost_usd,
# cost_status, api_calls, duration_seconds, exit_reason, failure_reason.
# This is the ONLY shape recorded here. Cost is never estimated: when the
# host cannot compute it, cost_status is "unknown" and no cost is shown.

def _clean_execution(entry: Any) -> Optional[dict]:
    """Validate a host result entry into the storable execution record.

    Allowlisted fields only; everything else is dropped (summary/error
    prose, _child_* internals, tool_trace, anything key-shaped). Returns
    None when the entry is not a mapping.
    """
    if not isinstance(entry, dict):
        return None
    out: dict = {}
    route = entry.get("route")
    if isinstance(route, dict):
        clean_route = {}
        for key in ("backend", "label", "provider", "model",
                    "reasoning_effort", "service_tier"):
            value = route.get(key)
            if isinstance(value, str) and value.strip():
                clean_route[key] = value.strip()
        for key in ("fallback_activated", "refused"):
            if route.get(key) is True:
                clean_route[key] = True
        if clean_route:
            out["route"] = clean_route
    tokens = entry.get("tokens")
    if isinstance(tokens, dict):
        clean_tokens = {}
        for key in ("input", "output"):
            value = tokens.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                clean_tokens[key] = value
        if clean_tokens:
            out["tokens"] = clean_tokens
    cost = entry.get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        out["cost_usd"] = round(float(cost), 6)
    cost_status = entry.get("cost_status")
    out["cost_status"] = (cost_status.strip() if isinstance(cost_status, str)
                          and cost_status.strip() else "unknown")
    api_calls = entry.get("api_calls")
    if isinstance(api_calls, int) and not isinstance(api_calls, bool) and api_calls >= 0:
        out["api_calls"] = api_calls
    duration = entry.get("duration_seconds")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
        out["duration_seconds"] = round(float(duration), 3)
    for key in ("exit_reason", "failure_reason", "status"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    if entry.get("truncated") is True:
        out["truncated"] = True
    task_index = entry.get("task_index")
    if isinstance(task_index, int) and not isinstance(task_index, bool) and task_index >= 0:
        out["task_index"] = task_index
    return out


def execution_from_metadata(metadata: Any) -> dict:
    """Validated execution record out of a task's parsed metadata.

    Returns {} when absent. Re-validates on read: a hand-edited DB cannot
    smuggle fields past the API. ``recorded_at`` is preserved when present.
    """
    if not isinstance(metadata, dict):
        return {}
    raw = metadata.get("execution")
    if not isinstance(raw, dict):
        return {}
    clean = _clean_execution(raw)
    if clean is None:
        return {}
    recorded_at = raw.get("recorded_at")
    if isinstance(recorded_at, str) and recorded_at.strip():
        clean["recorded_at"] = recorded_at.strip()
    return clean


def record_execution(task_id: str, entry: Any, db_path: Optional[str] = None) -> bool:
    """Record a host delegate_task result entry on a task.

    Stores the validated record under ``metadata["execution"]`` (no schema
    change; existing DBs unaffected). Returns True when stored, False for a
    missing task or an unusable entry. Never raises.
    """
    try:
        clean = _clean_execution(entry)
        if clean is None:
            return False
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT metadata FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return False
            try:
                metadata = json.loads(row["metadata"]) if row["metadata"] else {}
            except json.JSONDecodeError:
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            clean["recorded_at"] = datetime.now(timezone.utc).isoformat()
            metadata["execution"] = clean
            conn.execute(
                "UPDATE tasks SET metadata = ?, updated_at = ? WHERE id = ?",
                (json.dumps(metadata), datetime.now(timezone.utc).isoformat(), task_id))
        return True
    except Exception:
        return False


def get_execution(task_id: str, db_path: Optional[str] = None) -> dict:
    """Validated execution record for a task, {} when none recorded."""
    task = get_task(task_id, db_path=db_path)
    if not task:
        return {}
    return execution_from_metadata(task.get("metadata"))


# ---------- Query helpers ----------

def roster_snapshot_path(db_path: Optional[str] = None) -> Path:
    """Where the roster snapshot lives: next to the task DB.

    The agent process writes it when install config is applied; the
    dashboard process reads it back. Same resolution as the DB itself,
    so the CEO_PACK_TASK_DB override moves both together.
    """
    return _resolve_db_path(db_path).parent / "ceo_pack_roster.json"


def _parse_task_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    for field in ("risk_flags", "metadata"):
        if d.get(field):
            try:
                d[field] = json.loads(d[field])
            except json.JSONDecodeError:
                d[field] = {}
    return d


def list_tasks(limit: int = 100, status: Optional[str] = None,
               seat: Optional[str] = None, run_id: Optional[str] = None,
               db_path: Optional[str] = None) -> list[dict]:
    """All tasks, newest first. Read-only; used by the dashboard board."""
    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(status)
    if seat:
        where.append("seat = ?")
        params.append(seat)
    if run_id:
        where.append("run_id = ?")
        params.append(run_id)
    sql = "SELECT * FROM tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(0, int(limit)))
    with _connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_parse_task_row(r) for r in rows]


def get_task_runs(limit: int = 20, db_path: Optional[str] = None) -> list[dict]:
    """Orchestration runs, newest first. Read-only."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM task_runs ORDER BY created_at DESC LIMIT ?",
            (max(0, int(limit)),)
        ).fetchall()
    return [dict(r) for r in rows]


def get_run_count(db_path: Optional[str] = None) -> int:
    """Total orchestration runs. Read-only."""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM task_runs").fetchone()
    return row["n"] if row else 0


def get_status_counts(db_path: Optional[str] = None) -> dict:
    """Task counts by lifecycle status. A count is a read, never an inference."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
        ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def get_recent_history(limit: int = 50, db_path: Optional[str] = None) -> list[dict]:
    """Most recent task-history events across all tasks, newest first.

    Each event carries the task title + seat so the activity feed can show
    it without a second lookup. Read-only.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT h.id, h.task_id, h.from_status, h.to_status, h.actor,
                      h.note, h.at, t.title, t.seat
               FROM task_history h
               JOIN tasks t ON t.id = h.task_id
               ORDER BY h.at DESC, h.id DESC LIMIT ?""",
            (max(0, int(limit)),)
        ).fetchall()
    return [dict(r) for r in rows]


def get_seat_last_activity(db_path: Optional[str] = None) -> dict:
    """Latest registry activity per seat, as ISO timestamps.

    A seat's last activity is the newest of (a) the ``updated_at`` of any
    task owned by the seat and (b) any task-history event on one of the
    seat's tasks. Seats with no tasks and no history are absent from the
    result — the caller shows "unknown", never an invented timestamp.
    Read-only. Used by the dashboard org chart so the seat that is
    working can light up; assigned is still not "executing".
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT t.seat AS seat, MAX(x.at) AS last_activity
               FROM (SELECT id AS task_id, updated_at AS at FROM tasks
                     UNION ALL
                     SELECT task_id, at FROM task_history) x
               JOIN tasks t ON t.id = x.task_id
               GROUP BY t.seat"""
        ).fetchall()
    return {r["seat"]: r["last_activity"] for r in rows}


def get_approval_history(limit: int = 50, db_path: Optional[str] = None) -> list[dict]:
    """History events where a task moved to approved — the approval record.

    The approval center reads this; it is registry evidence, not a
    separate log the dashboard could drift from.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT h.id, h.task_id, h.from_status, h.to_status, h.actor,
                      h.note, h.at, t.title, t.seat
               FROM task_history h
               JOIN tasks t ON t.id = h.task_id
               WHERE h.to_status = 'approved'
               ORDER BY h.at DESC, h.id DESC LIMIT ?""",
            (max(0, int(limit)),)
        ).fetchall()
    return [dict(r) for r in rows]


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


# ---------- Type 1 approval gate (Stage 1: warn/observe only) ----------
# Warn mode is NOT enforcement: the hook observes every tool call and logs
# what it WOULD have stopped; nothing is blocked and tokens are never
# consumed in Stage 1. The hook decision lives in gate.py; this section
# owns the storage (tokens, audit, intents) and the ticket lifecycle.
#
# Argument VALUES are never stored or logged anywhere in this section —
# only SHA-256 hashes of the canonical arguments. A token authorizes one
# exact (tool, arguments) pair; anything else is a different call.

TOKEN_TTL_MINUTES = 30

_GATE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_args_hash(arguments: Any) -> Optional[str]:
    """SHA-256 hex of the canonical arguments, or None when unhashable.

    Canonical form: JSON, recursively sorted keys, UTF-8, no whitespace.
    The same arguments always hash the same way at mint time and at check
    time; key order does not matter, one changed value does. Non-dict
    arguments and non-JSON-serializable values hash to None (no ticket).
    """
    if not isinstance(arguments, dict):
        return None
    try:
        canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _action_pair(tool_name: Optional[str], arguments: Optional[dict]) -> tuple:
    """Validate a (tool_name, arguments) pair into (tool, hash) or
    (None, None). A pair is only an action when the tool name is a
    non-empty string and the arguments hash cleanly."""
    if not isinstance(tool_name, str) or not tool_name.strip():
        return None, None
    args_hash = canonical_args_hash(arguments)
    if args_hash is None:
        return None, None
    return tool_name.strip(), args_hash


@contextmanager
def _read_connect(db_path: Optional[str] = None, timeout: float = 1.0):
    """Read-only connection for gate checks. Never creates the file or
    its parent directory: a missing registry raises FileNotFoundError so
    the caller can report registry_unavailable instead of inventing an
    empty one. Short timeout so a locked registry fails fast instead of
    stalling the hook past the host's callback budget."""
    path = _resolve_db_path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"task registry not found: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def token_row_is_live(row: dict, now: str) -> bool:
    """A stored token row counts as live only when every field checks
    out: status live, unused, unexpired, well-formed hash. A malformed
    row is treated as absent, never as authorization."""
    if not isinstance(row, dict):
        return False
    if row.get("status") != "live" or row.get("used_at"):
        return False
    tool = row.get("tool_name")
    args_hash = row.get("args_hash")
    if not isinstance(tool, str) or not tool.strip():
        return False
    if not isinstance(args_hash, str) or not _GATE_HASH_RE.fullmatch(args_hash):
        return False
    expires_at = row.get("expires_at")
    if not isinstance(expires_at, str):
        return False
    try:
        return datetime.fromisoformat(expires_at) > datetime.fromisoformat(now)
    except ValueError:
        return False


def _log_gate_decision_conn(
    conn: sqlite3.Connection,
    decision: str,
    tool_name: Optional[str] = None,
    args_hash: Optional[str] = None,
    task_id: Optional[str] = None,
    token_id: Optional[str] = None,
    approved_by: Optional[str] = None,
    actor: str = "gate",
    session_id: Optional[str] = None,
    reason: Optional[str] = None,
    mode: str = "warn",
) -> None:
    conn.execute(
        """INSERT INTO gate_audit (at, decision, tool_name, args_hash, task_id,
               token_id, approved_by, actor, session_id, reason, mode)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now(timezone.utc).isoformat(), decision, tool_name, args_hash,
         task_id, token_id, approved_by, actor, session_id, reason, mode),
    )


def log_gate_decision(
    decision: str,
    tool_name: Optional[str] = None,
    args_hash: Optional[str] = None,
    task_id: Optional[str] = None,
    token_id: Optional[str] = None,
    approved_by: Optional[str] = None,
    actor: str = "gate",
    session_id: Optional[str] = None,
    reason: Optional[str] = None,
    mode: str = "warn",
    db_path: Optional[str] = None,
) -> None:
    """Append one gate_audit row. Never raises: audit logging must never
    break the call it observes. Argument values are never written here —
    only the hash."""
    try:
        with _connect(db_path) as conn:
            _log_gate_decision_conn(
                conn, decision, tool_name, args_hash, task_id, token_id,
                approved_by, actor, session_id, reason, mode,
            )
    except Exception:
        logger.exception("CEO Pack: gate audit write failed")


def _mint_token_conn(
    conn: sqlite3.Connection,
    task_id: str,
    tool_name: str,
    args_hash: str,
    approved_by: str,
    minted_via: str,
    ttl_minutes: int = TOKEN_TTL_MINUTES,
) -> Optional[str]:
    """Insert a token row (or return the live one) inside an open
    transaction. Returns the token_id, or None when the inputs are
    invalid. Idempotent per (task, tool, args): re-minting the same
    action while a live token exists returns the existing token id."""
    if not isinstance(tool_name, str) or not tool_name.strip():
        return None
    if not isinstance(args_hash, str) or not _GATE_HASH_RE.fullmatch(args_hash):
        return None
    if not isinstance(approved_by, str) or not approved_by.strip():
        return None
    now = datetime.now(timezone.utc).isoformat()
    row = conn.execute(
        """SELECT token_id FROM approval_tokens
           WHERE task_id = ? AND tool_name = ? AND args_hash = ?
           AND status = 'live' AND used_at IS NULL AND expires_at > ?""",
        (task_id, tool_name, args_hash, now),
    ).fetchone()
    if row:
        return row["token_id"]
    token_id = str(uuid.uuid4())
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat()
    conn.execute(
        """INSERT INTO approval_tokens (token_id, task_id, tool_name, args_hash,
               approved_by, approved_at, expires_at, used_at, status, minted_via)
           VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'live', ?)""",
        (token_id, task_id, tool_name, args_hash, approved_by, now, expires_at, minted_via),
    )
    _log_gate_decision_conn(
        conn, decision="token_minted", tool_name=tool_name, args_hash=args_hash,
        task_id=task_id, token_id=token_id, approved_by=approved_by,
        actor=approved_by, reason=None, mode="warn",
    )
    return token_id


def mint_approval_token(
    task_id: str,
    tool_name: str,
    arguments: dict,
    approved_by: str,
    minted_via: str,
    db_path: Optional[str] = None,
    ttl_minutes: int = TOKEN_TTL_MINUTES,
) -> Optional[str]:
    """Mint a one-time approval ticket for one exact action. Returns the
    token_id, or None when the inputs are invalid. Never raises."""
    args_hash = canonical_args_hash(arguments)
    if args_hash is None:
        return None
    try:
        with _connect(db_path) as conn:
            return _mint_token_conn(conn, task_id, tool_name, args_hash, approved_by, minted_via, ttl_minutes)
    except Exception:
        logger.exception("CEO Pack: token mint failed")
        return None


def get_live_token(
    task_id: str,
    tool_name: str,
    args_hash: str,
    db_path: Optional[str] = None,
) -> Optional[dict]:
    """The live token for one exact (task, tool, args), or None. Used by
    approve callers to report whether a ticket was minted. Never raises."""
    try:
        with _read_connect(db_path) as conn:
            row = conn.execute(
                """SELECT * FROM approval_tokens
                   WHERE task_id = ? AND tool_name = ? AND args_hash = ?
                   AND status = 'live' AND used_at IS NULL""",
                (task_id, tool_name, args_hash),
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    d = dict(row)
    if token_row_is_live(d, datetime.now(timezone.utc).isoformat()):
        return d
    return None


def find_live_token(
    tool_name: str,
    args_hash: str,
    db_path: Optional[str] = None,
    timeout: float = 1.0,
) -> Optional[dict]:
    """The live token matching one exact (tool, args) call, or None.

    Used by the gate hook. Raises sqlite3.Error / FileNotFoundError when
    the registry cannot be read, so the caller can tell "no ticket" apart
    from "registry unavailable". Malformed rows are skipped, never treated
    as authorization."""
    if not isinstance(tool_name, str) or not tool_name.strip():
        return None
    if not isinstance(args_hash, str) or not _GATE_HASH_RE.fullmatch(args_hash):
        return None
    with _read_connect(db_path, timeout=timeout) as conn:
        rows = conn.execute(
            """SELECT * FROM approval_tokens
               WHERE tool_name = ? AND args_hash = ?
               AND status = 'live' AND used_at IS NULL""",
            (tool_name, args_hash),
        ).fetchall()
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        d = dict(row)
        if token_row_is_live(d, now):
            return d
    return None


def list_tokens_for_tool(
    tool_name: str,
    db_path: Optional[str] = None,
    timeout: float = 1.0,
) -> list[dict]:
    """All token rows for one tool (any status/hash), newest first. Used
    by the gate hook to tell "ticket exists but the call changed" apart
    from "no ticket at all". Raises on registry errors, like
    find_live_token. Rows are returned as-is; the caller validates them."""
    if not isinstance(tool_name, str) or not tool_name.strip():
        return []
    with _read_connect(db_path, timeout=timeout) as conn:
        rows = conn.execute(
            "SELECT * FROM approval_tokens WHERE tool_name = ? ORDER BY approved_at DESC",
            (tool_name,),
        ).fetchall()
    return [dict(r) for r in rows]


def token_status_for(tool_name: str, arguments: Any, db_path: Optional[str] = None) -> str:
    """Advisory ticket state for one exact (tool, args): live | used |
    expired | none. Advisory only — never enforcement, never raises. A
    revoked ticket counts as none: it authorizes nothing and was not
    merely spent or timed out."""
    args_hash = canonical_args_hash(arguments)
    if args_hash is None or not isinstance(tool_name, str) or not tool_name.strip():
        return "none"
    try:
        rows = list_tokens_for_tool(tool_name, db_path)
    except Exception:
        return "none"
    now = datetime.now(timezone.utc).isoformat()
    seen_used = seen_expired = False
    for row in rows:
        if row.get("args_hash") != args_hash:
            continue
        if row.get("status") == "revoked":
            continue
        if token_row_is_live(row, now):
            return "live"
        if row.get("used_at"):
            seen_used = True
        else:
            seen_expired = True
    if seen_used:
        return "used"
    if seen_expired:
        return "expired"
    return "none"


def _revoke_tokens_for_task_conn(
    conn: sqlite3.Connection,
    task_id: str,
    actor: str,
) -> int:
    """Revoke a task's live tokens inside an open transaction. Returns
    the number revoked. Called when a task is rejected: a revoked ticket
    never authorizes. Audit rows record each revocation."""
    rows = conn.execute(
        """SELECT token_id, tool_name, args_hash, approved_by FROM approval_tokens
           WHERE task_id = ? AND status = 'live' AND used_at IS NULL""",
        (task_id,),
    ).fetchall()
    count = 0
    for row in rows:
        conn.execute(
            "UPDATE approval_tokens SET status = 'revoked' WHERE token_id = ?",
            (row["token_id"],),
        )
        _log_gate_decision_conn(
            conn, decision="token_revoked", tool_name=row["tool_name"],
            args_hash=row["args_hash"], task_id=task_id,
            token_id=row["token_id"], approved_by=row["approved_by"],
            actor=actor, reason="token_revoked", mode="warn",
        )
        count += 1
    return count


def revoke_tokens_for_task(task_id: str, actor: str, db_path: Optional[str] = None) -> int:
    """Revoke a task's live tokens. Returns the number revoked. Never raises."""
    try:
        with _connect(db_path) as conn:
            return _revoke_tokens_for_task_conn(conn, task_id, actor)
    except Exception:
        logger.exception("CEO Pack: token revoke failed")
        return 0


def get_gate_decisions(limit: int = 50, db_path: Optional[str] = None) -> list[dict]:
    """Newest gate_audit rows first. Raises when the registry cannot be
    read; the dashboard maps that to 503."""
    with _read_connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM gate_audit ORDER BY at DESC, id DESC LIMIT ?",
            (max(0, int(limit)),),
        ).fetchall()
    return [dict(r) for r in rows]