"""CEO Pack dashboard plugin — backend API routes, mounted at /api/plugins/ceo-pack/.

Read-only. Every value comes from a real read of the persistent task
registry (tasks.py) or the roster (orchestration.get_roster_view, seeded
from the roster snapshot the agent process writes at registration).
Nothing is invented: an empty registry returns empty lists and zero
counts with an explicit empty flag, never fabricated activity.

No write routes exist by design. Any action (create/approve/assign) goes
through the chat tools, which run the lifecycle + permission checks.
A second task engine in the frontend is explicitly out of scope.

Import note: the host loads this file as a top-level module via
importlib.spec_from_file_location (see hermes_cli.web_server_dashboard),
so there is no package context and no sys.path help. The plugin root is
added to sys.path only for the three imports below, then removed again.
``tasks`` / ``orchestration`` / ``models`` are plain top-level names in
this process afterwards — verified against the Hermes tree: no host code
imports those bare names (the host's are package-qualified, e.g.
``hermes_cli.models``), so nothing is shadowed.

FastAPI is guaranteed by the host, but the module also loads without it
(a minimal stub router) so the dashboard tests can run under plain
unittest with stdlib only. In production the real APIRouter is used.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent

try:
    from fastapi import APIRouter, HTTPException
except ImportError:  # host-independent test runs (stdlib unittest)
    class HTTPException(Exception):  # type: ignore[no-redef]
        """Minimal stand-in: same (status_code, detail) shape as FastAPI's."""

        def __init__(self, status_code: int, detail: Any = None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class APIRouter:  # type: ignore[no-redef]
        """Records routes; handlers stay plain callables for tests."""

        def __init__(self):
            self.routes = []

        def get(self, path: str):
            def deco(fn):
                self.routes.append(("GET", path, fn))
                return fn
            return deco

try:
    sys.path.insert(0, str(_PLUGIN_ROOT))
    try:
        import models as _models  # noqa: F401  (orchestration's fallback import needs it)
        import orchestration as _orch
        import tasks as _tasks
    finally:
        sys.path.remove(str(_PLUGIN_ROOT))
except Exception:
    log.exception("CEO Pack dashboard: failed to load plugin modules")
    _orch = None  # type: ignore[assignment]
    _tasks = None  # type: ignore[assignment]

router = APIRouter()

_DB_READY = False

# Fields that must never leave the API, even though the registry holds them:
# tool results / errors can echo caller-supplied text, and metadata is
# free-form caller data. The dashboard shows state, not payloads.
_TASK_STRIP = {"result", "result_json", "error", "error_json", "metadata"}

# Free-form decisions logged in chat are not persisted by the plugin in
# v0.5.0 (ceo_decision_log returns JSON; it writes nothing). Say so plainly
# instead of inventing a decision history.
DECISIONS_NOTE = (
    "Decisions logged in chat with ceo_decision_log are not stored by the "
    "plugin in v0.5.0. Approval history below comes from the task registry "
    "(tasks that moved to approved), which is the persisted record."
)


def _ensure_db() -> bool:
    """Make sure the registry schema exists (idempotent)."""
    global _DB_READY
    if _DB_READY:
        return True
    if _tasks is None:
        return False
    try:
        _tasks.init_db()
        _DB_READY = True
        return True
    except Exception:
        log.exception("CEO Pack dashboard: task database init failed")
        return False


def _require_db():
    if not _ensure_db():
        raise HTTPException(status_code=503, detail="task registry unavailable")


def _seed_roster() -> bool:
    """Apply the roster snapshot (if one exists) so the org chart shows
    the roster the agent actually sees. Returns True when a snapshot was
    applied. A missing snapshot is normal on a fresh install — the
    dashboard then shows the default roster, never invented bindings."""
    if _orch is None or _tasks is None:
        return False
    try:
        snap = _orch.read_roster_snapshot(_tasks.roster_snapshot_path())
        if snap is None:
            return False
        _orch.apply_roster_snapshot(snap)
        return True
    except Exception:
        log.debug("CEO Pack dashboard: roster snapshot not applied", exc_info=True)
        return False


def _public_task(task: dict) -> dict:
    """Task row safe for the API: state only, no result/error payloads.

    ``execution`` is the validated delegated-execution record (Phase 3):
    the host-reported route / tokens / cost for the task's delegate run,
    or None when nothing was recorded. ``metadata`` itself stays stripped.
    """
    out = {k: v for k, v in task.items() if k not in _TASK_STRIP}
    out["task_id"] = task.get("id")
    exec_rec = _tasks.execution_from_metadata(task.get("metadata")) if _tasks else {}
    out["execution"] = exec_rec or None
    return out


def _public_history(event: dict) -> dict:
    return {
        "id": event.get("id"),
        "task_id": event.get("task_id"),
        "title": event.get("title"),
        "seat": event.get("seat"),
        "from_status": event.get("from_status"),
        "to_status": event.get("to_status"),
        "actor": event.get("actor"),
        "note": event.get("note"),
        "at": event.get("at"),
    }


def _seat_node(entry: dict) -> dict:
    """One org-chart node. Children/members exist so departments and
    subordinate agents can plug in later; today every seat node is a leaf
    except through the head seat's reporting line (see _org_tree)."""
    avatar = entry.get("avatar")
    avatar_url = avatar if isinstance(avatar, str) and avatar.startswith("https://") else None
    name = entry.get("display_name") or entry.get("seat") or "?"
    initials = "".join(w[0] for w in name.replace("(", " ").split() if w)[:2].upper() or "?"
    return {
        "kind": "seat",
        "seat": entry.get("seat"),
        "display_name": entry.get("display_name"),
        "display": entry.get("display"),
        "avatar_url": avatar_url,
        "initials": initials,
        "soul": entry.get("soul"),
        "profile": entry.get("profile"),
        "bound": bool(entry.get("bound")),
        "binding": entry.get("binding"),
        "capabilities": entry.get("capabilities") or [],
        "provided": entry.get("provided") or [],
        "missing": entry.get("missing") or [],
        "children": [],
        "members": [],
    }


def _org_tree(roster: dict) -> dict:
    """Org tree from the roster. The head seat is the root; the other seats
    report to it under the plugin's accountability model (the head seat
    holds priorities and the decision log; the CEO retains accountability
    for the integrated result). This is the plugin's model, not a claimed
    HR reporting line — departments and custom reporting lines plug into
    ``departments`` and node ``children`` when they exist."""
    seats = [_seat_node(s) for s in roster.get("seats", [])]
    head = roster.get("head_seat") or "CEO"
    root = next((n for n in seats if n["seat"] == head), seats[0] if seats else None)
    if root is None:
        return {"kind": "org", "root": None, "departments": []}
    root["children"] = [n for n in seats if n is not root]
    return {"kind": "org", "root": root, "departments": []}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Routes (all GET, read-only)
# ---------------------------------------------------------------------------

@router.get("/")
def health():
    return {"plugin": "ceo-pack", "version": "0.5.0", "status": "ok"}


@router.get("/summary")
def summary():
    _require_db()
    try:
        counts = _tasks.get_status_counts()
        total = sum(counts.values())
        pending = _tasks.get_pending_approvals()
        running = _tasks.get_running_tasks()
        stuck = _tasks.get_stuck_tasks()
        total_runs = _tasks.get_run_count()
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail="task registry unavailable")
    return {
        "generated_at": _now_iso(),
        "empty": total == 0,
        "tasks_total": total,
        "tasks_by_status": counts,
        "total_runs": total_runs,
        "pending_approvals": len(pending),
        "running": len(running),
        "stuck": len(stuck),
    }


@router.get("/roster")
def roster():
    if _orch is None:
        raise HTTPException(status_code=503, detail="roster unavailable")
    snapshot_loaded = _seed_roster()
    view = _orch.get_roster_view()
    nodes = [_seat_node(s) for s in view.get("seats", [])]
    return {
        "status": "success",
        "generated_at": _now_iso(),
        "head_seat": view.get("head_seat"),
        "seats": nodes,
        "org": _org_tree(view),
        "departments": [],
        "snapshot_loaded": snapshot_loaded,
        "display_text_is_untrusted": True,
        "note": ("Org chart from the roster. Reporting shown by the plugin's "
                 "accountability model (head seat holds priorities). "
                 "Departments and custom reporting lines plug in here when "
                 "they exist; none are shown until they do."),
    }


@router.get("/tasks")
def list_tasks(status: Optional[str] = None, seat: Optional[str] = None,
               run_id: Optional[str] = None, limit: int = 50):
    _require_db()
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be an integer 0-200.")
    for name, value in (("status", status), ("seat", seat), ("run_id", run_id)):
        if value is not None and not isinstance(value, str):
            raise HTTPException(status_code=400, detail="%s must be text." % name)
    try:
        rows = _tasks.list_tasks(limit=limit, status=status, seat=seat, run_id=run_id)
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail="task registry unavailable")
    return {
        "generated_at": _now_iso(),
        "tasks": [_public_task(t) for t in rows],
        "count": len(rows),
        "filters": {"status": status, "seat": seat, "run_id": run_id, "limit": limit},
    }


@router.get("/tasks/{task_id}")
def task_detail(task_id: str):
    _require_db()
    try:
        task = _tasks.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        history = _tasks.get_task_history(task_id)
        deps = _tasks.get_task_dependencies(task_id)
        dependents = _tasks.get_dependent_tasks(task_id)
        deps_met = _tasks.are_dependencies_met(task_id)
    except HTTPException:
        raise
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail="task registry unavailable")
    out = _public_task(task)
    out["history"] = [_public_history(h) for h in history]
    out["dependencies"] = deps
    out["dependents"] = dependents
    out["dependencies_met"] = deps_met
    return out


@router.get("/runs")
def list_runs(limit: int = 20):
    _require_db()
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0 or limit > 100:
        raise HTTPException(status_code=400, detail="limit must be an integer 0-100.")
    try:
        runs = _tasks.get_task_runs(limit=limit)
        out = []
        for run in runs:
            run_tasks = _tasks.get_tasks_by_run(run["run_id"])
            counts: dict = {}
            for t in run_tasks:
                s = t.get("status", "unknown")
                counts[s] = counts.get(s, 0) + 1
            out.append({
                "run_id": run["run_id"],
                "goal": run.get("goal"),
                "head_seat": run.get("head_seat"),
                "model_key": run.get("model_key"),
                "run_status": run.get("status"),
                "created_at": run.get("created_at"),
                "updated_at": run.get("updated_at"),
                "completed_at": run.get("completed_at"),
                "task_count": len(run_tasks),
                "status_counts": counts,
            })
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail="task registry unavailable")
    return {"generated_at": _now_iso(), "runs": out, "count": len(out)}


@router.get("/runs/{run_id}")
def run_detail(run_id: str):
    _require_db()
    try:
        run = _tasks.get_task_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        run_tasks = _tasks.get_tasks_by_run(run_id)
    except HTTPException:
        raise
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail="task registry unavailable")
    counts: dict = {}
    for t in run_tasks:
        s = t.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1
    return {
        "run_id": run["run_id"],
        "goal": run.get("goal"),
        "head_seat": run.get("head_seat"),
        "model_key": run.get("model_key"),
        "run_status": run.get("status"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "completed_at": run.get("completed_at"),
        "task_count": len(run_tasks),
        "status_counts": counts,
        "tasks": [_public_task(t) for t in run_tasks],
    }


@router.get("/activity")
def activity():
    _require_db()
    try:
        counts = _tasks.get_status_counts()
        running = _tasks.get_running_tasks()
        stuck = _tasks.get_stuck_tasks()
        history = _tasks.get_recent_history(limit=50)
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail="task registry unavailable")
    return {
        "generated_at": _now_iso(),
        "status_counts": counts,
        "running": [_public_task(t) for t in running],
        "stuck": [_public_task(t) for t in stuck],
        "recent_history": [_public_history(h) for h in history],
    }


@router.get("/approvals")
def approvals():
    _require_db()
    try:
        pending = _tasks.get_pending_approvals()
        history = _tasks.get_approval_history(limit=50)
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail="task registry unavailable")
    return {
        "generated_at": _now_iso(),
        "pending": [_public_task(t) for t in pending],
        "pending_count": len(pending),
        "history": [_public_history(h) for h in history],
        "decisions_note": DECISIONS_NOTE,
    }
