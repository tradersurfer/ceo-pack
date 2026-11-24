"""CEO Pack dashboard plugin — backend API routes, mounted at /api/plugins/ceo-pack/.

Reads come from a real read of the persistent task registry (tasks.py) or
the roster (orchestration.get_roster_view, seeded from the roster snapshot
the agent process writes at registration). Nothing is invented: an empty
registry returns empty lists and zero counts with an explicit empty flag,
never fabricated activity.

Writes exist, but only as five POST routes that call the same tasks.py
functions the chat tools call (create_task, approve_task, try_assign_task,
complete_task, update_task_status) — the same lifecycle + permission
checks, the same SQLite registry, the same history audit. The host
authenticates every /api/ call, so the dashboard caller is an
authenticated Hermes user (the owner). A second task engine in the
frontend is explicitly out of scope.

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
from datetime import date, datetime, timezone
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

        def post(self, path: str):
            def deco(fn):
                self.routes.append(("POST", path, fn))
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


def _is_overdue(task: dict) -> bool:
    """Derived at read time, never stored: a non-terminal task whose due
    date is before today. Same rule the frontends used to compute locally
    (web isOverdue / Desktop isOverdue) — the backend flag is now the one
    source of truth, so the frontends read it instead of recomputing."""
    status = task.get("status")
    due = task.get("due_date")
    if not due or status in ("completed", "verified", "failed", "rejected"):
        return False
    try:
        return date.fromisoformat(str(due)) < date.today()
    except ValueError:
        return False


def _public_task(task: dict) -> dict:
    """Task row safe for the API: state only, no result/error payloads.

    ``execution`` is the validated delegated-execution record (Phase 3):
    the host-reported route / tokens / cost for the task's delegate run,
    or None when nothing was recorded. ``metadata`` itself stays stripped.
    ``overdue`` is derived at read time from due_date + status (never
    stored): a non-terminal task whose due date is before today.
    """
    out = {k: v for k, v in task.items() if k not in _TASK_STRIP}
    out["task_id"] = task.get("id")
    exec_rec = _tasks.execution_from_metadata(task.get("metadata")) if _tasks else {}
    out["execution"] = exec_rec or None
    out["overdue"] = _is_overdue(task)
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


def _seat_node(entry: dict, last_activity=None) -> dict:
    """One org-chart node. Children/members exist so departments and
    subordinate agents can plug in later; today every seat node is a leaf
    except through the head seat's reporting line (see _org_tree).

    ``last_activity`` is the seat's latest registry activity (ISO), or
    None when the seat has no recorded activity — the UI shows "unknown",
    never an invented timestamp."""
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
        "last_activity": last_activity,
        "children": [],
        "members": [],
    }


def _org_tree(roster: dict, last_activity: Optional[dict] = None) -> dict:
    """Org tree from the roster. The head seat is the root; the other seats
    report to it under the plugin's accountability model (the head seat
    holds priorities and the decision log; the CEO retains accountability
    for the integrated result). This is the plugin's model, not a claimed
    HR reporting line — departments and custom reporting lines plug into
    ``departments`` and node ``children`` when they exist."""
    seats = [_seat_node(s, (last_activity or {}).get(s.get("seat"))) for s in roster.get("seats", [])]
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
    # Last activity is best-effort: a registry that is unavailable must
    # never fail the roster — seats show "unknown" instead.
    try:
        activity_map = _tasks.get_seat_last_activity() if _tasks else {}
    except Exception:
        activity_map = {}
    nodes = [_seat_node(s, activity_map.get(s.get("seat"))) for s in view.get("seats", [])]
    return {
        "status": "success",
        "generated_at": _now_iso(),
        "head_seat": view.get("head_seat"),
        "seats": nodes,
        "org": _org_tree(view, activity_map),
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


@router.get("/gate-decisions")
def gate_decisions(limit: int = 50):
    """The Type 1 gate's audit log, newest first — what the gate WOULD
    have stopped (warn mode) and what it allowed on a ticket. Same shape
    as /activity's history rows. Only hashes are ever returned, never
    argument values."""
    _require_db()
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0 or limit > 100:
        raise HTTPException(status_code=400, detail="limit must be an integer from 0 to 100.")
    try:
        decisions = _tasks.get_gate_decisions(limit=limit)
    except (sqlite3.Error, FileNotFoundError, OSError):
        raise HTTPException(status_code=503, detail="task registry unavailable")
    return {
        "generated_at": _now_iso(),
        "decisions": decisions,
        "count": len(decisions),
    }


# ---------------------------------------------------------------------------
# Write routes (POST) — the dashboard acting for the owner.
#
# Each route calls the same tasks.py function the matching chat tool calls,
# with the same validation the chat handler runs, mapped to HTTP codes:
# unknown task/run -> 404 (run) or 400 (run_id in a create body — the chat
# tool takes any run_id, the dashboard only offers existing runs, so an
# unknown one is a bad request), wrong state / not Type 1 -> 409, bad
# input -> 400, registry locked/unavailable -> 503. Responses are the
# sanitized task object (same shape as the GET routes). The host
# authenticates every /api/ call, so the caller is an authenticated Hermes
# user; the history records the actor as the owner acting from the
# dashboard.
# ---------------------------------------------------------------------------

def _write_task_or_404(task_id: str) -> dict:
    """Fetch a task for a write route, or 404. DB errors become 503."""
    try:
        task = _tasks.get_task(task_id)
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail="task registry unavailable")
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


def _text_field(body: dict, name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail="%s is required text." % name)
    return value.strip()


def _optional_text(body: dict, name: str) -> Optional[str]:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="%s must be text." % name)
    return value


@router.post("/tasks")
def create_task_route(body: dict):
    _require_db()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object.")
    run_id = _text_field(body, "run_id")
    seat = _text_field(body, "seat")
    title = _text_field(body, "title")
    if _orch is None:
        raise HTTPException(status_code=503, detail="roster unavailable")
    canonical_seat = _orch.normalize_seat(seat)
    if canonical_seat is None:
        raise HTTPException(status_code=400, detail="Unknown seat. Valid: %s" % ", ".join(_orch.SEATS))
    due_date = _optional_text(body, "due_date")
    if due_date is not None:
        ok, note = _orch._check_due_date(due_date)
        if not ok:
            raise HTTPException(status_code=400, detail=note)
    description = _optional_text(body, "description") or ""
    task_key = _optional_text(body, "task_key")
    owner = _optional_text(body, "owner")
    model_route = _optional_text(body, "model_route")
    parent_task_id = _optional_text(body, "parent_task_id")
    risk_flags = body.get("risk_flags")
    if risk_flags is not None and not isinstance(risk_flags, list):
        raise HTTPException(status_code=400, detail="risk_flags must be a list.")
    metadata = body.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise HTTPException(status_code=400, detail="metadata must be a JSON object.")
    # Stage 1 gate: optional exact action this task will execute. Stored
    # as tool name + argument hash only; raw values are never stored.
    tool_name = _optional_text(body, "tool_name")
    arguments = body.get("arguments")
    if arguments is not None and not isinstance(arguments, dict):
        raise HTTPException(status_code=400, detail="arguments must be a JSON object.")
    try:
        if _tasks.get_task_run(run_id) is None:
            raise HTTPException(status_code=400, detail="unknown run_id")
        task_id = _tasks.create_task(
            run_id=run_id,
            seat=canonical_seat,
            title=title,
            description=description,
            task_key=task_key,
            due_date=due_date,
            owner=owner,
            model_route=model_route,
            approval_required=bool(body.get("approval_required", False)),
            risk_flags=risk_flags or [],
            parent_task_id=parent_task_id,
            metadata=metadata or {},
            tool_name=tool_name,
            arguments=arguments,
        )
        task = _tasks.get_task(task_id)
    except HTTPException:
        raise
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail="task registry unavailable")
    if task is None:
        raise HTTPException(status_code=503, detail="task registry unavailable")
    return _public_task(task)


@router.post("/tasks/{task_id}/approve")
def approve_task_route(task_id: str, body: Optional[dict] = None):
    _require_db()
    task = _write_task_or_404(task_id)
    if not task.get("approval_required"):
        raise HTTPException(status_code=409, detail="Task does not require approval (not a Type 1 task)")
    if task.get("status") != "proposed":
        raise HTTPException(status_code=409, detail="Task is in '%s' state, not 'proposed'. Can only approve proposed tasks." % task.get("status"))
    # Stage 1 gate: optional exact action (tool_name + arguments) in the
    # body. When given here it is recorded on the task and a one-time
    # ticket is minted for it; when the task already names an action,
    # that one is used instead. Raw values are never stored — only the hash.
    tool_name = None
    arguments = None
    if body is not None:
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be a JSON object.")
        tool_name = _optional_text(body, "tool_name")
        arguments = body.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            raise HTTPException(status_code=400, detail="arguments must be a JSON object.")
    try:
        ok = _tasks.approve_task(task_id, "owner", note="Type 1 approval granted (dashboard)",
                                 tool_name=tool_name, arguments=arguments, minted_via="dashboard")
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail="task registry unavailable")
    if not ok:
        raise HTTPException(status_code=409, detail="Task could not be approved (it may have moved already)")
    stored = _tasks.get_task(task_id)
    public = _public_task(stored)
    action_tool = stored.get("action_tool") if stored else None
    action_hash = stored.get("action_args_hash") if stored else None
    token = _tasks.get_live_token(task_id, action_tool, action_hash) if action_tool and action_hash else None
    public["token_minted"] = bool(token)
    public["token_id"] = token.get("token_id") if token else None
    return public


@router.post("/tasks/{task_id}/reject")
def reject_task_route(task_id: str, body: Optional[dict] = None):
    _require_db()
    task = _write_task_or_404(task_id)
    if task.get("status") != "proposed":
        raise HTTPException(status_code=409, detail="Task is in '%s' state, not 'proposed'. Can only reject proposed tasks." % task.get("status"))
    note = ""
    if body is not None:
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be a JSON object.")
        note = body.get("note") or ""
        if not isinstance(note, str):
            raise HTTPException(status_code=400, detail="note must be text.")
    try:
        ok = _tasks.update_task_status(task_id, "rejected", actor="owner (dashboard)", note=note or "Rejected from the dashboard")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid status")
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail="task registry unavailable")
    if not ok:
        raise HTTPException(status_code=409, detail="Task could not be rejected (it may have moved already)")
    return _public_task(_tasks.get_task(task_id))


@router.post("/tasks/{task_id}/complete")
def complete_task_route(task_id: str, body: Optional[dict] = None):
    _require_db()
    task = _write_task_or_404(task_id)
    status = task.get("status")
    if status != "running":
        if status in ("completed", "verified", "rejected"):
            detail = "Task is already '%s'. A %s task cannot be completed again." % (status, status)
        else:
            detail = "Task is '%s', not 'running'. Only a running task can be marked complete." % status
        raise HTTPException(status_code=409, detail=detail)
    try:
        ok = _tasks.complete_task(task_id, actor="owner (dashboard)", note="Marked complete from the dashboard")
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail="task registry unavailable")
    if not ok:
        raise HTTPException(status_code=409, detail="Task could not be completed (it may have moved already)")
    return _public_task(_tasks.get_task(task_id))


@router.post("/tasks/{task_id}/assign")
def assign_task_route(task_id: str, body: dict):
    _require_db()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object.")
    owner = _text_field(body, "owner")
    model_route = _optional_text(body, "model_route")
    task = _write_task_or_404(task_id)
    try:
        outcome, stored = _tasks.try_assign_task(task_id, owner, model_route, actor="owner (dashboard)")
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail="task registry unavailable")
    if outcome == "refused":
        raise HTTPException(status_code=409, detail="Task is in '%s' state. Can only assign approved/proposed tasks." % task.get("status"))
    if outcome == "awaiting_approval":
        raise HTTPException(status_code=409, detail="Type 1 task is waiting for owner approval; approve it before assigning.")
    if outcome == "conflict":
        raise HTTPException(status_code=409, detail="Task is already assigned to '%s'. Assigning it to '%s' would overwrite the owner — refused. The task keeps its current owner." % (stored.get("owner"), owner))
    # "assigned" and "already_assigned" both return the task; the
    # already-assigned case changed nothing (idempotent re-assign).
    return _public_task(_tasks.get_task(task_id))
