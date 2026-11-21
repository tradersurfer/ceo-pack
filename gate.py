"""Type 1 approval gate — Stage 1 (warn/observe only).

Warn mode is NOT enforcement. The pre_tool_call hook observes every tool
call, matches it against minted approval tickets, and writes what it
WOULD have stopped to the gate_audit table. It always returns None
(allow) and never consumes a ticket. Stage 2 will return the block
directive here, behind an explicit config switch; until then, nothing
in this module can stop a call, and warn mode is never described as
enforcement.

The hook must never raise: the host ignores a raising hook, so every
failure path here ends in None (Stage 2 will return block on internal
error instead — fail closed — but Stage 1 cannot block, so it logs
hook_error and allows).

Argument VALUES are never stored or logged here — only SHA-256 hashes
of the canonical arguments, computed in tasks.py.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    from . import tasks as _tasks
except ImportError:  # direct-module load (tests load files individually)
    import tasks as _tasks


def _audit(
    decision: str,
    tool_name: Optional[str],
    args_hash: Optional[str],
    session_id: Optional[str],
    db_path: Optional[str],
    task_id: Optional[str] = None,
    token_id: Optional[str] = None,
    approved_by: Optional[str] = None,
    reason: Optional[str] = None,
) -> None:
    """Best-effort audit write; never raises (log_gate_decision already
    swallows its own errors)."""
    _tasks.log_gate_decision(
        decision=decision, tool_name=tool_name, args_hash=args_hash,
        task_id=task_id, token_id=token_id, approved_by=approved_by,
        actor="gate", session_id=session_id, reason=reason, mode="warn",
        db_path=db_path,
    )


def _is_type1(tool_name: str, args: Any) -> bool:
    """Narrow net (Adrian's answer #2): the plugin's own approval /
    side-effect tools, or a risk cue in the call's own arguments.
    No host-tool effect-capable list in Stage 1. A classification
    failure raises and becomes a hook_error decision upstream."""
    try:
        from . import orchestration as _orch
    except ImportError:  # direct-module load (tests load files individually)
        import orchestration as _orch
    if tool_name in _orch.APPROVAL_TOOLS or tool_name in _orch.SIDE_EFFECT_TOOLS:
        return True
    return bool(_orch.infer_risk(args))


def _no_token_reason(tool_name: str, args_hash: Optional[str], db_path: Optional[str]) -> str:
    """Why a Type 1 call has no live ticket: the exact ticket was spent /
    expired / revoked, the call changed after approval, or there never
    was one. A read failure here degrades to no_token (the registry
    error case is handled before this runs)."""
    try:
        rows = _tasks.list_tokens_for_tool(tool_name, db_path)
    except Exception:
        return "no_token"
    now = datetime.now(timezone.utc).isoformat()
    live_other = False
    for row in rows:
        if row.get("args_hash") == args_hash:
            if row.get("used_at"):
                return "token_used"
            if row.get("status") == "revoked":
                return "token_revoked"
            if not _tasks.token_row_is_live(row, now):
                return "token_expired"
        elif row.get("status") == "live" and not row.get("used_at"):
            if _tasks.token_row_is_live(row, now):
                live_other = True
    if live_other:
        return "modified_arguments"
    return "no_token"


def _decide(tool_name: Any, args: Any, session_id: Optional[str], db_path: Optional[str]) -> dict:
    """The §4 decision order (warn mode). May raise on registry /
    classification errors; decide_pre_tool_call turns those into
    hook_error decisions."""
    tool_name = tool_name if isinstance(tool_name, str) else ""
    args_hash = _tasks.canonical_args_hash(args) if isinstance(args, dict) else None

    # Step 1: exact ticket match first — a live ticket for this exact
    # call would allow it. Stage 1 does NOT consume it.
    registry_error = False
    token = None
    if args_hash is not None and tool_name:
        try:
            token = _tasks.find_live_token(tool_name, args_hash, db_path=db_path)
        except Exception:
            registry_error = True
    if token is not None:
        _audit(decision="would_allow", tool_name=tool_name, args_hash=args_hash,
               task_id=token.get("task_id"), token_id=token.get("token_id"),
               approved_by=token.get("approved_by"), session_id=session_id,
               reason="token_match", db_path=db_path)
        return {"decision": "would_allow", "reason": "token_match",
                "token_id": token.get("token_id"), "task_id": token.get("task_id"),
                "mode": "warn"}

    # Step 2: Type 1 or not (narrow net).
    if not _is_type1(tool_name, args):
        return {"decision": "type2", "reason": None, "token_id": None,
                "task_id": None, "mode": "warn"}

    # Step 3: Type 1 with no ticket and an unreadable registry. No audit
    # row can be written either (the audit table lives in the registry),
    # so the decision itself carries the reason and the host log carries it.
    if registry_error:
        logger.warning(
            "CEO Pack gate: registry unreadable for %s; would block (warn mode).",
            tool_name,
        )
        return {"decision": "would_block", "reason": "registry_unavailable",
                "token_id": None, "task_id": None, "mode": "warn"}

    # Step 4: Type 1, registry readable, no live ticket — name the reason.
    reason = _no_token_reason(tool_name, args_hash, db_path)
    _audit(decision="would_block", tool_name=tool_name, args_hash=args_hash,
           session_id=session_id, reason=reason, db_path=db_path)
    return {"decision": "would_block", "reason": reason, "token_id": None,
            "task_id": None, "mode": "warn"}


def decide_pre_tool_call(
    tool_name: Any,
    args: Any,
    session_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict:
    """Decide what the gate WOULD do for one tool call (Stage 1).

    Returns {"decision": "would_allow" | "would_block" | "type2" |
    "hook_error", "reason": ..., "token_id": ..., "task_id": ...,
    "mode": "warn"} and writes the matching gate_audit row when the
    registry can be written. Never raises: any internal error becomes a
    hook_error decision (logged best-effort) and the call proceeds —
    warn mode cannot block.
    """
    try:
        return _decide(tool_name, args, session_id, db_path)
    except Exception:
        # The host ignores a raising hook, so the hook must never raise.
        # Stage 1: log hook_error (best effort) and allow. Stage 2 will
        # return {"action": "block", ...} here instead (fail closed).
        try:
            _audit(decision="hook_error",
                   tool_name=tool_name if isinstance(tool_name, str) else None,
                   args_hash=None, session_id=session_id,
                   reason="hook_error", db_path=db_path)
        except Exception:
            pass
        return {"decision": "hook_error", "reason": "hook_error",
                "token_id": None, "task_id": None, "mode": "warn"}


def _on_pre_tool_call(tool_name: str = "", args: Any = None, session_id: Optional[str] = None, **kwargs) -> None:
    """pre_tool_call hook, Stage 1 (warn/observe only).

    Runs the gate decision, writes the audit row, and ALWAYS returns
    None: warn mode observes, it never blocks and never consumes a
    ticket. The host ignores a raising hook, so every failure path
    here ends in None (Stage 2 will return the block directive here
    instead, including on internal error — see decide_pre_tool_call).
    """
    try:
        decide_pre_tool_call(tool_name, args, session_id=session_id)
    except Exception:
        # decide_pre_tool_call never raises by contract; this is belt
        # and braces. Never let the hook raise.
        logger.exception("CEO Pack gate: pre_tool_call failed for %s", tool_name)
    return None
