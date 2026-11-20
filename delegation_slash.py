"""``/ceo delegation``: show the CEO delegation mode and switch it (CLI, TUI, Desktop, messaging).

``/ceo delegation``               the saved mode and a picker to change it
``/ceo delegation <mode>``        save it as this profile's delegation.backend; takes effect next session (/new)
``/ceo delegation <mode> --now``  save it AND apply it to this chat now (rebuilds tools once)

Saving for next session is default because the mode is part of the chat's cached prompt.
--now is opt-in: core rebuilds this session's tool schema at its next turn (one cache break).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Optional

from . import delegation_store as store

_ALIASES = {
    "default": "default", "1": "default", "off": "default", "builtin": "default",
    "ceo_pack": "ceo_pack", "2": "ceo_pack", "unified": "ceo_pack", "ceo": "ceo_pack", "pack": "ceo_pack",
}
USAGE = "/ceo delegation [default | ceo_pack] [--now]"
AskChoice = Callable[[str, list], Optional[str]]
_WHEN = [
    {"value": "next", "label": "Next session (recommended)",
     "description": "Saved now; /new starts a chat that uses it. This chat keeps its prompt cache."},
    {"value": "now", "label": "Now (rebuilds this chat's tools)",
     "description": "Saved and applied to this chat: its tools are rebuilt at your next message (one prompt-cache break)."}
]


def make_handler(ctx) -> Callable[..., str]:
    """The registered handler: handle with the context's ask_choice and delegation_backends."""
    def delegation_command(raw_args: str = "", session_key: str = "", **_: Any) -> str:
        return handle(raw_args, session_key=session_key, ask_choice=getattr(ctx, "ask_choice", None),
                      api=getattr(ctx, "delegation_backends", None))
    return delegation_command


def _problem(mode: str, api) -> str:
    """Why backend mode cannot run here (empty when it can, or when core cannot tell)."""
    if api is None:
        return ""
    info = next((b for b in api.list_backends() if b.get("name") == mode), None)
    if info is None:
        return f"Delegation backend '{mode}' is not available: no enabled plugin provides it."
    return "" if info.get("available") else (info.get("reason") or f"Delegation backend '{mode}' is not available.")


def _ready(mode: str, section: Mapping[str, Any]) -> list[str]:
    """Names the mode would route to; empty for a mode with nothing configured (default: [""])."""
    if mode == "ceo_pack":
        return [f"{seat} ({cfg['type']})" for seat, cfg in store.configured_seats(section).items()]
    return [""]


def _mode_default(mode: str, section: Mapping[str, Any]) -> str:
    if mode == "ceo_pack":
        seats = store.configured_seats(section)
        return ", ".join(f"{s} ({c['type']})" for s, c in seats.items()) if seats else ""
    return ""


def _detail(mode: str, section: Mapping[str, Any]) -> str:
    if mode == "default":
        model = " ".join(str(section.get("model") or "").split()) or "the parent's model"
        return f"subagents use delegation.model: {model}"
    seats = store.configured_seats(section)
    if not seats:
        return "nothing set up yet (`hermes ceo delegation config`)"
    parts = []
    for s, c in seats.items():
        parts.append(f'{s} ({c["type"]})')
    return f'{len(seats)} seat{'s' * (len(seats) != 1)}: {', '.join(parts)}'


def _parse(raw: str) -> tuple[Optional[str], bool, str]:
    """(mode or None, now?, what was not understood) from the raw arguments."""
    words = raw.split()
    flags = [w for w in words if w.startswith("-")]
    unknown_flag = next((f for f in flags if f != "--now"), "")
    if unknown_flag:
        return None, False, unknown_flag
    now = "--now" in flags
    rest = " ".join(w for w in words if w not in flags).strip()
    if not rest:
        return None, now, ""
    key = re.sub(r"[\s-]+", "_", rest.lower())
    return _ALIASES.get(key), now, ("" if key in _ALIASES else rest)


def _listing(section: Mapping[str, Any], saved: str, api) -> str:
    lines = [f"CEO Delegation mode: {store.mode_label(saved)}"]
    problem = _problem(saved, api)
    if problem:
        lines.append(f"⚠ {problem} Switch to a mode below to fix delegation.")
    for i, mode in enumerate(store.MODES, 1):
        mark = "  ← current" if mode == saved else ""
        lines.append(f"  {i}. {store.MODES[mode]:<20} {_detail(mode, section)}{mark}")
    lines.append("Switch: /ceo delegation <mode> saves it for the next session (/new); "
                 "/ceo delegation <mode> --now also applies it to this chat (rebuilds its tools once).")
    return "\n".join(lines)


def _ask_mode(ask_choice: AskChoice, section: Mapping[str, Any], saved: str) -> Optional[str]:
    """The mode the user picked from the surface's own picker, or None (nothing can ask, cancelled)."""
    choices = [{"value": mode, "label": store.MODES[mode] + (" (current)" if mode == saved else ""),
                "description": _detail(mode, section)}
               for mode in store.MODES if _ready(mode, section)]
    return ask_choice(f"CEO Delegation mode: {store.mode_label(saved)}. Switch to:", choices)


def handle(raw_args: str = "", session_key: str = "", ask_choice: Optional[AskChoice] = None, api=None) -> str:
    """api: ctx.delegation_backends (None on core without it); ask_choice: ctx.ask_choice (None where surface cannot ask)."""
    section = store.delegation_section()
    saved = store.current_mode(section)
    mode, now, unknown = _parse(raw_args or "")
    if unknown.startswith("-"):
        return f"Unknown option '{unknown}'. Usage: {USAGE}"
    if unknown:
        return f"Unknown delegation mode '{unknown}'. Usage: {USAGE}"
    if mode is None:
        mode = _ask_mode(ask_choice, section, saved) if ask_choice else None
        if mode is None:
            return _listing(section, saved, api)
        if api is not None and not now:  # only core can apply a mode to the running chat
            when = ask_choice(f"Use {store.mode_label(mode)} from:", _WHEN)
            if when is None:
                return f"CEO Delegation mode unchanged: {store.mode_label(saved)}."
            now = when == "now"
    return _switch(mode, now, section, saved, api, session_key)


def _save(mode: str, now: bool, api, session_key: str) -> bool:
    """Save mode (and apply it to this chat when now); True when it was applied now."""
    if api is None:
        store.set_mode(mode)  # core without ctx.delegation_backends
        return False
    if not now:
        api.set_global_backend(mode, session_key=session_key)
        return False
    try:
        api.set_global_backend(mode, session_key=session_key, apply_now=True)
    except TypeError:  # core from before apply_now refuses the keyword before writing anything
        api.set_global_backend(mode, session_key=session_key)
        return False
    return True


def _switch(mode: str, now: bool, section: Mapping[str, Any], saved: str, api, session_key: str) -> str:
    if not _ready(mode, section):
        return (f"{store.mode_label(mode)} has nothing set up yet, so the agent would have nothing to pick. "
                f"Set it up with `hermes ceo delegation config`, then switch.")
    head = f"CEO Delegation mode: {store.mode_label(mode)}"
    if mode == saved and not now:
        return f"{head} is already saved. Add --now to apply it to this chat (rebuilds its tools once)."
    try:
        applied = _save(mode, now, api, session_key)
    except (ValueError, PermissionError, RuntimeError) as exc:
        return f"Not switched: {exc}"
    note = " delegation.model / delegation.provider apply again." if mode == "default" else (
        f" {_detail(mode, section).capitalize()}.")
    if applied:
        return (f"{head}, saved and applied to this chat: its tools are rebuilt for it at your next message "
                f"(one prompt-cache break).{note}")
    if now:
        return f"{head}, saved for the next session (/new). Applying it to this chat needs a newer Hermes.{note}"
    return (f"{head}, saved. It takes effect from the next session (/new); this chat's tools keep the previous "
            f"mode's choices until then (add --now to apply it here, which rebuilds this chat's tools once).{note}")