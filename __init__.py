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
    "ceo-pack-usage",
]

_GATE_OBSERVER = None  # gate._on_pre_tool_call, bound in register()


def _on_pre_tool_call(tool_name=None, args=None, **kwargs):
    """The plugin's one pre_tool_call hook.

    1. Owner-only approval guard: the model can never approve a Type 1 task.
       Approval is not a tool any more; if a stale schema or a hand-written
       call names an approval tool anyway, block it.
    2. Type 1 gate, Stage 1 (warn/observe only): log what the gate WOULD
       have stopped. The observer always returns None — it never blocks.
    """
    name = str(tool_name or "").lower()
    if name.startswith("ceo_") and "approve" in name:
        return {"action": "block",
                "message": "Type 1 approval is owner-only. Ask the owner to approve in the CEO dashboard or with /task-approve."}
    if _GATE_OBSERVER is not None:
        try:
            _GATE_OBSERVER(tool_name=tool_name, args=args, **kwargs)
        except Exception:
            logger.exception("CEO Pack gate observer failed for %s", tool_name)
    return None


def _task_approve_command(raw_args):
    """/task-approve <task_id> — typed by the user, handled here, never by the model."""
    task_id = (raw_args or "").strip().split()[0] if (raw_args or "").strip() else ""
    if not task_id:
        return "Usage: /task-approve <task_id>  (owner only; moves a Type 1 task from proposed to approved)"
    try:
        from . import tasks
    except ImportError:
        import tasks
    task = tasks.get_task(task_id)
    if not task:
        return f"No task with id {task_id}."
    if not task.get("approval_required"):
        return "That task does not need approval (it is not a Type 1 task)."
    if task.get("status") != "proposed":
        return f"That task is '{task.get('status')}', not 'proposed'. Only proposed tasks can be approved."
    if tasks.approve_task(task_id, "owner", note="Type 1 approval granted (slash command)", minted_via="slash"):
        stored = tasks.get_task(task_id) or {}
        tool, digest = stored.get("action_tool"), stored.get("action_args_hash")
        if tool and digest and tasks.get_live_token(task_id, tool, digest):
            ticket = f" One-time ticket minted for {tool} (30 min, single use; warn mode — nothing is enforced yet)."
        else:
            ticket = " No action was recorded, so no ticket was minted."
        return f"Approved: {task.get('title')}. It can now be assigned.{ticket}"
    return "Could not approve (the task may have just moved). Check its status."


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


# Every CEO Pack answer is ELI5: plain words a busy entrepreneur or
# business owner can follow — no jargon, short sentences, one idea at a
# time, and always what happens next.
_ELI5 = (
    "Answer in plain, simple words a busy entrepreneur or business "
    "owner can follow — no jargon, short sentences, one idea at a time. "
    "Say what happens next in plain words."
)


def _tool_prompt(tool, instruction, raw):
    """Analyze-first prompt for a one-tool slash command.

    The agent reads the user's words, works out the tool's inputs from
    them, and only then calls the tool — it never gets a bare pointer.
    """
    return (
        f"CEO Pack: {instruction} "
        f"Then call {tool} with what you found. "
        "If something required is missing, ask ONE short question for it, "
        "then run it — do not stall with a bare acknowledgement. "
        f"{_ELI5} "
        f"Input was: {raw}"
    )


def _ceo_command(raw_args):
    """Short state replies for /ceo — the host prints the command's return
    value as the visible reply, so a long instruction prompt shows up as a
    wall of text. The flow itself lives in the ceo-pack-usage skill; the
    command only flips the mode and points the agent at the skill."""
    raw = (raw_args or "").strip()
    if raw.lower() == "off":
        return (
            "CEO OFF. Back to normal chat. "
            "Use '/ceo on' to turn CEO mode back on."
        )
    if raw.lower() == "on":
        return (
            "CEO ON. Use '/ceo off' to turn it off. "
            "Follow the ceo-pack-usage skill for this session."
        )
    if not raw:
        return (
            "CEO ON. Follow the ceo-pack-usage skill. "
            "What business goal should we work on first? "
            "Use '/ceo off' to turn it off."
        )
    return (
        "CEO ON for this session. Read the user's words below, work out "
        "the goal, say it back in one plain sentence, then follow the "
        "ceo-pack-usage skill. Use '/ceo off' to turn it off. "
        f"Input was: {raw_args}"
    )


def _q4_command(raw_args):
    return (
        "CEO Pack Q4 closeout: first work out the year from the user's "
        "words (this year if they did not say), then use "
        "q4_closeout_checklist with that year, then assign CFO (books/tax "
        "prep), CLO (entity/filings), COO/Hermes (coordination), and CRO "
        "(potential-client pipeline). Checklist only, not tax or legal "
        "advice. "
        f"{_ELI5} "
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
        name="ceo_task_assign", toolset="ceo-pack",
        schema=schemas.CEO_TASK_ASSIGN, handler=tools.ceo_task_assign,
    )
    ctx.register_tool(
        name="ceo_task_complete", toolset="ceo-pack",
        schema=schemas.CEO_TASK_COMPLETE, handler=tools.ceo_task_complete,
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
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)

    # --- Type 1 gate, Stage 1 (warn/observe only) ---
    # pre_tool_call observes every tool call and logs what the gate WOULD
    # have stopped. It never blocks and never consumes a ticket in Stage 1.
    # Registration failure must not break the rest of the plugin.
    # It runs inside _on_pre_tool_call (one hook per event for this plugin).
    global _GATE_OBSERVER
    try:
        from . import gate as _gate
        _GATE_OBSERVER = _gate._on_pre_tool_call
        logger.info("CEO Pack Type 1 gate registered (Stage 1, warn/observe only)")
    except Exception:
        logger.exception("CEO Pack: Type 1 gate observer failed to load")

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
        lambda raw: _tool_prompt(
            "ceo_orchestrate",
            "first read the user's words below and work out the goal in one plain sentence",
            raw,
        ),
        description="CEO Pack orchestration plan for a goal",
    )    # --- v0.4.0: shortcuts to existing ceo_* tools ---
    # Each entry carries its own analyze-first instruction: the agent works
    # out the tool's inputs from the user's words instead of getting a bare
    # pointer. (name, tool, description, instruction)
    _shortcuts = [
        ("brief", "ceo_brief", "Start a CEO brief for a goal",
         "first read the user's words below and work out the goal in one plain sentence"),
        ("assign", "ceo_assign", "Assign one owner and one due date",
         "first read the user's words below and work out the task, which seat owns it (CEO, CFO, COO, CTO, CMO, CLO, Chief of Staff, or CRO), and one due date"),
        ("verify", "ceo_verify", "Verify a step before it counts done",
         "first read the user's words below and work out which tool ran, what it returned, and what done looks like for it"),
        ("log", "ceo_decision_log", "Log a decision",
         "first read the user's words below and work out the decision and which option was chosen"),
        ("decision", "ceo_decision_log", "Log a decision, decision-first",
         "first read the user's words below and work out the decision and which option was chosen"),
        ("route", "ceo_route", "Route a task to a seat",
         "first read the user's words below and work out the task, then which seat should own it"),
        ("models", "ceo_choose_model", "Pick or check the model",
         "first read the user's words below and work out what the user wants to know or change about the model"),
        ("weekly-pulse", "ceo_orchestrate", "Weekly pulse: orchestrate the week in one flow",
         "first read the user's words below and work out the goal for the week in one plain sentence"),
        ("closeout", "q4_closeout_checklist", "Year-end closeout checklist",
         "first read the user's words below and work out which year to close out (this year if they did not say)"),
        ("q1", "ceo_closeout_checklist", "Q1 closeout checklist",
         "first read the user's words below and work out which year and quarter to close out (this year, Q1, if they did not say)"),
        ("q2", "ceo_closeout_checklist", "Q2 closeout checklist",
         "first read the user's words below and work out which year and quarter to close out (this year, Q2, if they did not say)"),
        ("q3", "ceo_closeout_checklist", "Q3 closeout checklist",
         "first read the user's words below and work out which year and quarter to close out (this year, Q3, if they did not say)"),
        ("monthly-closeout", "ceo_closeout_checklist", "Monthly closeout checklist",
         "first read the user's words below and work out which year and month to close out (this year, this month, if they did not say)"),
        ("daily-checks", "ceo_daily_check", "Daily checks: shipped, earned, stuck, decisions",
         "first read the user's words below and work out which period to check (today if they did not say)"),
        ("eod-checks", "ceo_daily_check", "End-of-day checks and handoff",
         "first read the user's words below and work out the end-of-day period to check"),
        ("performance-checks", "ceo_performance_check", "Employee performance check",
         "first read the user's words below and work out whose performance to check"),
        ("status-check", "ceo_status_check", "Status of a briefing, assignment, task, or log",
         "first read the user's words below and work out what to check on — a briefing, assignment, task, or log entry — and its id or title if given"),
        ("roster", "ceo_roster", "C-Suite roster view (CEO Dashboard)",
         "first read the user's words below and work out what to show — the full C-Suite roster: every seat, who is bound to a real profile and who falls back to the main agent, and any capability gaps"),
    ]
    for _name, _tool, _desc, _instruction in _shortcuts:
        ctx.register_command(
            _name,
            (lambda tool, instruction: (lambda raw: _tool_prompt(tool, instruction, raw)))(_tool, _instruction),
            description=_desc,
        )

    # --- v0.5.0: Task persistence commands ---
    _task_commands = [
        ("task-create", "ceo_task_create", "Create a persistent task with full lifecycle",
         "first read the user's words below and work out the run to put it in (ask for one if there is none — a task needs a run), which seat owns it, and the task title"),
        ("task-status", "ceo_task_status", "Get task status and history",
         "first read the user's words below and work out which task, by its id or title"),
        ("task-list", "ceo_task_list", "List tasks by seat, status, or run",
         "first read the user's words below and work out how to filter the list — by seat, status, or run, if the user said any"),
        ("task-assign", "ceo_task_assign", "Assign a task to an owner/profile",
         "first read the user's words below and work out which task and who owns it now"),
        ("task-complete", "ceo_task_complete", "Mark a running task completed (completed, not verified)",
         "first read the user's words below and work out which task to mark complete — only a task that is running can complete"),
        ("run-create", "ceo_run_create", "Create an orchestration run",
         "first read the user's words below and work out the goal for the run in one plain sentence"),
        ("run-status", "ceo_run_status", "Get run status and progress",
         "first read the user's words below and work out which run, by its id or goal"),
    ]
    for _name, _tool, _desc, _instruction in _task_commands:
        ctx.register_command(
            _name,
            (lambda tool, instruction: (lambda raw: _tool_prompt(tool, instruction, raw)))(_tool, _instruction),
            description=_desc,
        )
    # Owner-only approval: a real handler the user types. It is NOT model-routed
    # (no tool prompt) and there is no model-callable approval tool.
    ctx.register_command(
        "task-approve", _task_approve_command,
        description="Approve a Type 1 task (owner only): /task-approve <task_id>",
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