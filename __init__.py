"""CEO Pack — Hermes plugin registration.

Registers 14 tools, 6 bundled skills, and 21+ slash commands. (v0.5.0)
Orchestration first: brief -> route -> assign -> tool call -> verify
-> decision log. No network calls, no credential reads, no file writes
outside the plugin's own state. All external actions are flagged for
owner approval by the tools themselves; this plugin does not
auto-approve anything.

v0.5.0 adds:
- Persistent task registry with full lifecycle (proposed -> approved -> assigned -> running -> blocked/completed -> verified)
- Unified CEO delegation provider: each C-Suite seat gets a model route OR a Hermes profile route
- Interactive delegation config: `hermes ceo delegation config`
- In-chat delegation switching: `/ceo delegation`
- Task execution history, dependencies, retry/recovery

Seat naming, answered from the v0.3.0 handoff: ceo_route returns
"COO (Hermes)" for display, and normalize_seat in orchestration.py
accepts "COO", "COO (Hermes)", and "Hermes" and canonicalizes to "COO".
"""

import json
import logging
from pathlib import Path

from . import schemas, tools

logger = logging.getLogger(__name__)

_SKILLS = [
    "ceo-brief",
    "ceo-decision-log",
    "ceo-assign",
    "q4-closeout",
    "orchestration",
    "model-selection",
]

def _on_post_tool_call(tool_name, args, result, task_id, **kwargs):
    """Wire verification into the post_tool_call hook (v0.3.0 step 5).

    For every ceo_* result, run verify_result and log whether it is
    verified. The hook never raises and never blocks the host — it
    closes the advisory gap by making verification visible on every
    call, while ceo_verify remains the tool a caller uses explicitly.
    """
    logger.debug("CEO Pack: tool called: %s (session %s)", tool_name, task_id)
    if not tool_name.startswith("ceo_") and tool_name != "q4_closeout_checklist":
        return
    try:
        from .orchestration import META_TOOLS, verify_result

        if tool_name in META_TOOLS:
            # An envelope or a verdict is not a completed business action, so there is nothing to verify.
            logger.info("CEO Pack verify: %s is a meta tool (validation/verdict), not a completion.", tool_name)
            return

        parsed = result
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except ValueError:
                parsed = None
        verdict = verify_result(tool_name, parsed)
        logger.info(
            "CEO Pack verify: %s status=%s verified=%s next=%s",
            tool_name,
            verdict.get("status"),
            verdict.get("verified"),
            verdict.get("next", {}).get("action"),
        )
    except Exception:
        logger.exception("CEO Pack: post_tool_call verification failed for %s", tool_name)


def _ceo_command(raw_args):
    return (
        "CEO Pack: use ceo_brief for a goal, ceo_route to pick a seat, "
        "ceo_assign for one owner + one due date, and ceo_decision_log "
        "to record the decision. External actions need owner approval. "
        f"Input was: {raw_args}"
    )


def _q4_command(raw_args):
    return (
        "CEO Pack Q4 closeout: use q4_closeout_checklist with the year, "
        "then assign CFO (books/tax prep), CLO (entity/filings), "
        "COO/Hermes (coordination), and CRO (potential-client pipeline). "
        "Checklist only, not tax or legal advice. "
        f"Input was: {raw_args}"
    )
def _read_install_config(ctx):
    """Read declared settings through the real Hermes API, ctx.get_config(key).

    Older hosts that expose a plain dict (config / settings / plugin_config) are still honoured.
    """
    try:
        from . import orchestration
    except ImportError:
        import orchestration
    cfg = {}
    getter = getattr(ctx, "get_config", None)
    if callable(getter):
        for key in orchestration.CONFIG_KEYS + ("seat_names",):
            try:
                value = getter(key, None)
            except Exception:
                value = None
            if value is not None:
                cfg[key] = value
    if not cfg:
        for attr in ("config", "settings", "plugin_config"):
            legacy = getattr(ctx, attr, None)
            if isinstance(legacy, dict) and legacy:
                cfg = legacy
                break
    return cfg


def _apply_install_config(ctx):
    """Apply every declared install setting; log what was applied and what was refused."""
    try:
        from . import orchestration
    except ImportError:
        import orchestration
    applied, notes = orchestration.apply_install_config(_read_install_config(ctx))
    if applied:
        logger.info("CEO Pack install config applied: %s", ", ".join(applied))
    for note in notes:
        logger.warning("CEO Pack install config: %s", note)
def register(ctx):
    _apply_install_config(ctx)

    # --- Persist the applied roster for the dashboard tab ---
    # The dashboard API runs in the web-server process, which never shares
    # this process's in-memory roster (aliases, display names, bindings).
    # Best-effort snapshot next to the task DB; a failure must never break
    # registration. Resolves through the same path as the task DB, so the
    # CEO_PACK_TASK_DB override moves both together.
    try:
        from . import orchestration as _orch_snapshot
        from .tasks import roster_snapshot_path
        _orch_snapshot.write_roster_snapshot(roster_snapshot_path())
    except Exception:
        logger.debug("CEO Pack: roster snapshot not written", exc_info=True)

    # --- Initialize task database ---
    try:
        from .tasks import init_db
        init_db()
        logger.info("CEO Pack task database initialized")
    except Exception:
        logger.exception("CEO Pack: task database init failed")

    # --- Core tools (14) ---
    ctx.register_tool(
        name="ceo_brief", toolset="ceo-pack",
        schema=schemas.CEO_BRIEF, handler=tools.ceo_brief,
    )
    ctx.register_tool(
        name="ceo_assign", toolset="ceo-pack",
        schema=schemas.CEO_ASSIGN, handler=tools.ceo_assign,
    )
    ctx.register_tool(
        name="ceo_decision_log", toolset="ceo-pack",
        schema=schemas.CEO_DECISION_LOG, handler=tools.ceo_decision_log,
    )
    ctx.register_tool(
        name="ceo_route", toolset="ceo-pack",
        schema=schemas.CEO_ROUTE, handler=tools.ceo_route,
    )
    ctx.register_tool(
        name="q4_closeout_checklist", toolset="ceo-pack",
        schema=schemas.Q4_CLOSEOUT_CHECKLIST, handler=tools.q4_closeout_checklist,
    )
    ctx.register_tool(
        name="ceo_orchestrate", toolset="ceo-pack",
        schema=schemas.CEO_ORCHESTRATE, handler=tools.ceo_orchestrate,
    )
    ctx.register_tool(
        name="ceo_choose_model", toolset="ceo-pack",
        schema=schemas.CEO_CHOOSE_MODEL, handler=tools.ceo_choose_model,
    )
    ctx.register_tool(
        name="ceo_tool_call", toolset="ceo-pack",
        schema=schemas.CEO_TOOL_CALL, handler=tools.ceo_tool_call,
    )
    ctx.register_tool(
        name="ceo_verify", toolset="ceo-pack",
        schema=schemas.CEO_VERIFY, handler=tools.ceo_verify,
    )
    ctx.register_tool(
        name="ceo_closeout_checklist", toolset="ceo-pack",
        schema=schemas.CEO_CLOSEOUT_CHECKLIST, handler=tools.ceo_closeout_checklist,
    )
    ctx.register_tool(
        name="ceo_daily_check", toolset="ceo-pack",
        schema=schemas.CEO_DAILY_CHECK, handler=tools.ceo_daily_check,
    )
    ctx.register_tool(
        name="ceo_performance_check", toolset="ceo-pack",
        schema=schemas.CEO_PERFORMANCE_CHECK, handler=tools.ceo_performance_check,
    )
    ctx.register_tool(
        name="ceo_status_check", toolset="ceo-pack",
        schema=schemas.CEO_STATUS_CHECK, handler=tools.ceo_status_check,
    )
    ctx.register_tool(
        name="ceo_roster", toolset="ceo-pack",
        schema=schemas.CEO_ROSTER, handler=tools.ceo_roster,
    )    # --- v0.5.0: Task persistence tools (new) ---
    ctx.register_tool(
        name="ceo_task_create", toolset="ceo-pack",
        schema=schemas.CEO_TASK_CREATE, handler=tools.ceo_task_create,
    )
    ctx.register_tool(
        name="ceo_task_status", toolset="ceo-pack",
        schema=schemas.CEO_TASK_STATUS, handler=tools.ceo_task_status,
    )
    ctx.register_tool(
        name="ceo_task_list", toolset="ceo-pack",
        schema=schemas.CEO_TASK_LIST, handler=tools.ceo_task_list,
    )
    ctx.register_tool(
        name="ceo_task_approve", toolset="ceo-pack",
        schema=schemas.CEO_TASK_APPROVE, handler=tools.ceo_task_approve,
    )
    ctx.register_tool(
        name="ceo_task_assign", toolset="ceo-pack",
        schema=schemas.CEO_TASK_ASSIGN, handler=tools.ceo_task_assign,
    )
    ctx.register_tool(
        name="ceo_run_create", toolset="ceo-pack",
        schema=schemas.CEO_RUN_CREATE, handler=tools.ceo_run_create,
    )
    ctx.register_tool(
        name="ceo_run_status", toolset="ceo-pack",
        schema=schemas.CEO_RUN_STATUS, handler=tools.ceo_run_status,
    )

    ctx.register_hook("post_tool_call", _on_post_tool_call)

    # --- Skills ---
    skills_dir = Path(__file__).parent / "skills"
    for name in _SKILLS:
        skill_md = skills_dir / name / "SKILL.md"
        if skill_md.exists():
            ctx.register_skill(name, skill_md)

    # --- Core commands ---
    ctx.register_command("ceo", _ceo_command, description="CEO Pack brief and assignment help")
    ctx.register_command("q4", _q4_command, description="CEO Pack year-end closeout checklist")
    ctx.register_command(
        "orchestrate",
        lambda raw: "CEO Pack orchestration: use ceo_orchestrate with a goal. "
        f"Input was: {raw}",
        description="CEO Pack orchestration plan for a goal",
    )    # --- v0.4.0: shortcuts to existing ceo_* tools ---
    _shortcuts = [
        ("brief", "ceo_brief", "Start a CEO brief for a goal"),
        ("assign", "ceo_assign", "Assign one owner and one due date"),
        ("verify", "ceo_verify", "Verify a step before it counts done"),
        ("log", "ceo_decision_log", "Log a decision"),
        ("decision", "ceo_decision_log", "Log a decision, decision-first"),
        ("route", "ceo_route", "Route a task to a seat"),
        ("models", "ceo_choose_model", "Pick or check the model"),
        ("weekly-pulse", "ceo_orchestrate", "Weekly pulse: orchestrate the week in one flow"),
        ("closeout", "q4_closeout_checklist", "Year-end closeout checklist"),
        ("q1", "ceo_closeout_checklist", "Q1 closeout checklist"),
        ("q2", "ceo_closeout_checklist", "Q2 closeout checklist"),
        ("q3", "ceo_closeout_checklist", "Q3 closeout checklist"),
        ("monthly-closeout", "ceo_closeout_checklist", "Monthly closeout checklist"),
        ("daily-checks", "ceo_daily_check", "Daily checks: shipped, earned, stuck, decisions"),
        ("eod-checks", "ceo_daily_check", "End-of-day checks and handoff"),
        ("performance-checks", "ceo_performance_check", "Employee performance check"),
        ("status-check", "ceo_status_check", "Status of a briefing, assignment, task, or log"),
        ("roster", "ceo_roster", "C-Suite roster view (CEO Dashboard)"),
    ]
    for _name, _tool, _desc in _shortcuts:
        ctx.register_command(
            _name,
            (lambda tool: (lambda raw: f"CEO Pack: use {tool}. Input was: {raw}"))(_tool),
            description=_desc,
        )

    # --- v0.5.0: Task persistence commands ---
    _task_commands = [
        ("task-create", "ceo_task_create", "Create a persistent task with full lifecycle"),
        ("task-status", "ceo_task_status", "Get task status and history"),
        ("task-list", "ceo_task_list", "List tasks by seat, status, or run"),
        ("task-approve", "ceo_task_approve", "Approving a Type 1 task (owner only)"),
        ("task-assign", "ceo_task_assign", "Assign a task to an owner/profile"),
        ("run-create", "ceo_run_create", "Create an orchestration run"),
        ("run-status", "ceo_run_status", "Get run status and progress"),
    ]
    for _name, _tool, _desc in _task_commands:
        ctx.register_command(
            _name,
            (lambda tool: (lambda raw: f"CEO Pack: use {tool}. Input was: {raw}"))(_tool),
            description=_desc,
        )
    # --- v0.5.0: Unified CEO Delegation Provider ---
    try:
        from .delegation_provider import CEODelegationProvider
        ctx.register_delegation_provider(CEODelegationProvider())
        logger.info("CEO Pack delegation provider registered")
    except ModuleNotFoundError as exc:
        # exc.name is "agent" (not "agent.delegation_provider") when the
        # top-level host package is missing; only re-raise for other modules.
        if (exc.name or "").split(".")[0] != "agent":
            raise
        logger.warning(
            "CEO Pack needs a Hermes core with pluggable delegation providers "
            "(agent.delegation_provider); update Hermes. The provider is inactive until then."
        )
    except Exception:
        logger.exception("CEO Pack: delegation provider registration failed")

    # --- v0.5.0: CLI command for delegation config ---
    try:
        from . import delegation_cli
        ctx.register_cli_command(
            name="ceo delegation",
            help="CEO delegation modes: default, ceo_pack (unified seat routes)",
            setup_fn=delegation_cli.setup_parser,
            handler_fn=delegation_cli.handle,
            description="Pick how CEO Pack routes subagents (Default or CEO Pack unified) "
                        "and edit per-seat routes. Changes apply to new sessions.",
        )
    except Exception:
        logger.exception("CEO Pack: delegation CLI registration failed")

    # --- v0.5.0: Slash command for in-chat delegation switching ---
    try:
        from . import delegation_slash
        ctx.register_command(
            "ceo delegation",
            delegation_slash.make_handler(ctx),
            description="Show or switch the CEO delegation mode (next session; --now for this chat)",
            args_hint="[default | ceo_pack] [--now]",
        )
    except Exception:
        logger.exception("CEO Pack: delegation slash command registration failed")