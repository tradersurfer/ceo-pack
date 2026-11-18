"""``hermes ceo delegation config``: pick the CEO delegation mode and set up seat routes.

Modes:
* **default**: uses delegation.model / delegation.provider (core default)
* **ceo_pack**: each C-Suite seat has its own model route or profile route

Interactive editor with curses menus (reuses core's model picker components).
"""

from __future__ import annotations

import sys
from typing import Any, Mapping, Optional

from . import delegation_store as store

_NO_TTY = (
    "`hermes ceo delegation config` needs an interactive terminal to edit routes.\n"
    "Print the current setup with `hermes ceo delegation config --show`, or set keys directly with\n"
    "`hermes config set delegation.backend default|ceo_pack`."
)

try:
    from hermes_cli.curses_ui import curses_radiolist, curses_checklist, curses_single_select
except ImportError:
    curses_radiolist = curses_checklist = curses_single_select = None  # type: ignore


def _has_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _text(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def setup_parser(parser) -> None:
    sub = parser.add_subparsers(dest="ceo_delegation_command", metavar="<command>")
    config = sub.add_parser(
        "config",
        help="Pick the CEO delegation mode and edit seat routes",
        description="Pick how CEO Pack routes subagents (Default or CEO Pack unified) "
                    "and edit per-seat routes. Changes apply to new sessions."
    )
    config.add_argument("--show", action="store_true", help="Print the current mode and routes, then exit")
    parser.set_defaults(func=handle)


def handle(args) -> int:
    if getattr(args, "ceo_delegation_command", None) != "config":
        print("usage: hermes ceo delegation config [--show]")
        return 0
    if getattr(args, "show", False):
        print(store.describe(store.delegation_section()))
        return 0
    if not _has_terminal():
        print(_NO_TTY, file=sys.stderr)
        return 1
    if not curses_radiolist:
        print("Curses UI not available (hermes_cli.curses_ui missing).", file=sys.stderr)
        return 1
    try:
        run()
    except RuntimeError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


# --- menus ------------------------------------------------------------------------------

def _radio(title: str, rows: list, selected: int = 0, description: Optional[str] = None, searchable: bool = False) -> int:
    return curses_radiolist(title, rows, selected=selected, cancel_returns=-1, description=description, searchable=searchable)


def _ask(question: str, default: str = "") -> str:
    from hermes_cli.cli_output import prompt
    return prompt(question, default or None).strip()


def run() -> None:
    section = store.delegation_section()
    mode = store.current_mode(section)
    modes = list(store.MODES)
    rows = [[(f"{store.MODES[m]:<20}", None), (store.MODE_BLURBS[m], "dim"),
             *([("  ← current", "yellow")] if m == mode else [])] for m in modes]
    idx = _radio("CEO Delegation mode: how C-Suite seats route their subagents", rows,
                 selected=modes.index(mode) if mode in modes else 0,
                 description=_mode_context(section))
    if idx < 0:
        print("No change.")
        return
    {"default": _use_default, "ceo_pack": _ceo_pack_routes}[modes[idx]]()


def _mode_context(section: Mapping[str, Any]) -> str:
    mode = store.current_mode(section)
    detail = ""
    if mode == "ceo_pack":
        n = len(store.configured_seats(section))
        detail = f" · {n} seat{'s' * (n != 1)} configured"
    return f"  Current mode: {store.mode_label(mode)}{detail}\n"


def _use_default() -> None:
    store.set_mode("default")
    section = store.delegation_section()
    model = _text(section.get("model")) or "the parent's model"
    where = _text(section.get("provider")) or _text(section.get("base_url"))
    print(f"Delegation mode: Default. delegation.model / delegation.provider apply again: subagents use "
          f"{model}{f' on {where}' if where else ''}. New sessions pick this up.")


# --- CEO Pack routes --------------------------------------------------------------------

def _ceo_pack_routes() -> None:
    while True:
        section = store.delegation_section()
        seats = store.configured_seats(section)
        from .delegation_provider import SEATS
        
        rows = []
        for seat in SEATS:
            cfg = seats.get(seat)
            if not cfg:
                rows.append([(f"{seat:<18}", None), ("(uses parent route)", "dim")])
            elif cfg["type"] == "model_route":
                where = cfg.get("provider") or cfg.get("base_url")
                model = cfg.get("model")
                extras = []
                if cfg.get("reasoning_effort"):
                    extras.append(f"reasoning {cfg['reasoning_effort']}")
                if cfg.get("service_tier") and cfg["service_tier"] != "normal":
                    extras.append(cfg["service_tier"])
                if cfg.get("daybreak"):
                    extras.append("daybreak")
                extra_str = f" [{', '.join(extras)}]" if extras else ""
                rows.append([(f"{seat:<18}", None), (f"{where} · {model}{extra_str}", None), (cfg.get("description") or "", "dim")])
            elif cfg["type"] == "profile_route":
                rows.append([(f"{seat:<18}", None), (f"profile: {cfg.get('profile')}", None), (cfg.get("description") or "", "dim")])
        
        rows.append(["+ Add/Edit seat", "Done: use CEO Pack"])
        idx = _radio("CEO Pack seat routes: each C-Suite seat gets a model route or profile route", rows,
                     description="  Enter on a seat to add/edit its route. "
                                 "Choose model route (provider+model) or profile route (Hermes profile).\n"
                                 "Seats without config use the parent agent's route.")
        if idx < 0:
            print("Route changes are saved; the delegation mode is unchanged.")
            return
        if idx == len(SEATS) + 1:  # Done
            if _finish_ceo_pack(seats):
                return
            continue
        if idx == len(SEATS):  # Add/Edit
            _edit_seat_route({}, seats)
            continue
        # Edit existing seat
        seat = SEATS[idx]
        _edit_seat_route(seats.get(seat, {}), {k: v for k, v in seats.items() if k != seat})


def _edit_seat_route(existing: Mapping[str, Any], others: Mapping[str, Mapping[str, Any]]) -> None:
    """Interactive editor for a single seat's route."""
    from .delegation_provider import SEATS
    
    # Pick seat if new
    if not existing:
        seat_names = [s for s in SEATS if s not in others]
        if not seat_names:
            print("All seats already configured.")
            return
        rows = [[(s, None)] for s in seat_names] + [["Back"]]
        idx = _radio("Which seat to configure?", rows)
        if idx < 0 or idx == len(seat_names):
            return
        seat = seat_names[idx]
    else:
        # Find seat name from existing config
        seat = next((s for s, cfg in {**others, **{"": existing}}.items() if cfg is existing), "")
        if not seat:
            for s in SEATS:
                if s not in others:
                    seat = s
                    break
    
    # Pick route type
    type_rows = [
        [("model_route", None), ("Provider + model (OpenRouter, Anthropic, local, etc.)", "dim")],
        [("profile_route", None), ("Hermes profile (uses its model, skills, credentials, SOUL)", "dim")],
        [("default (parent route)", None), ("No override; uses delegation.model/delegation.provider", "dim")],
    ]
    current_type = existing.get("type", "default") if existing else "model_route"
    type_idx = _radio(f"Route type for {seat}:", type_rows,
                      selected=["model_route", "profile_route", "default"].index(current_type))
    if type_idx < 0:
        return
    
    route_type = ["model_route", "profile_route", "default"][type_idx]
    
    if route_type == "default":
        # Remove seat config
        store.remove_seat(seat)
        print(f"Seat {seat} removed (will use parent route).")
        return
    
    description = _ask("One-line description (what the agent reads to choose)", _text(existing.get("description")))
    if not description:
        print("Route not saved.")
        return
    
    if route_type == "model_route":
        where = _pick_where(existing)
        if where is None:
            print("Route not saved.")
            return
        route = {"seat": seat, "type": "model_route", "description": description, **where}
        # Keys that survive an edit of the same endpoint
        same_endpoint = (where.get("provider", "") == _text(existing.get("provider")) and
                         where.get("base_url", "") == _text(existing.get("base_url")))
        for key, value in existing.items():
            if key not in route and key not in store.ROUTE_KEYS and (same_endpoint or key == "fallback_providers"):
                route[key] = value
        _pick_effort(route, _text(existing.get("reasoning_effort")))
        _pick_tier(route, _text(existing.get("service_tier")))
        _pick_daybreak(route, existing.get("daybreak") is True)
        problems = store.validate_seat_config(route, others)
        if problems:
            for p in problems:
                print(f"  ✗ {p}")
            print("Route not saved.")
            return
        store.save_seat(route)
        print(f"Saved seat {seat} as model route.")
    
    elif route_type == "profile_route":
        profile = _pick_profile(existing.get("profile") if existing else None)
        if not profile:
            print("Route not saved.")
            return
        route = {"seat": seat, "type": "profile_route", "description": description, "profile": profile}
        problems = store.validate_seat_config(route, others)
        if problems:
            for p in problems:
                print(f"  ✗ {p}")
            print("Route not saved.")
            return
        store.save_seat(route)
        print(f"Saved seat {seat} as profile route.")


def _pick_where(existing: Mapping[str, Any]) -> Optional[dict[str, str]]:
    """Provider + model picker (reuses hermes model's curses components)."""
    from hermes_cli.inventory import build_aux_picker_rows, format_aux_picker_entries
    current_provider, current_url = _text(existing.get("provider")), _text(existing.get("base_url"))
    current_model = _text(existing.get("model"))
    try:
        rows = build_aux_picker_rows(current_provider=current_provider, current_model=current_model,
                                     current_base_url=current_url)
    except Exception as exc:
        print(f"Could not list your providers: {exc}")
        rows = []
    entries = format_aux_picker_entries(rows, current_provider=current_provider, current_base_url=current_url)
    entries.append(("__custom__", "Custom endpoint (direct URL)" + ("  ← current" if current_url else ""), []))
    slugs = [slug for slug, _, _ in entries]
    selected = slugs.index(current_provider) if current_provider in slugs else (
        len(entries) - 1 if current_url else 0)
    idx = _radio("Provider for this seat:", [label for _, label, _ in entries], selected=selected, searchable=True,
                 description="  Providers with credentials on this profile. Set up another with `hermes model`.\n")
    if idx < 0:
        return None
    slug, _, models = entries[idx]
    if slug == "__custom__":
        url = _ask("Base URL (OpenAI-compatible, e.g. http://localhost:8080/v1)", current_url)
        model = _ask("Model", current_model) if url else ""
        return {"base_url": url, "model": model} if url and model else None
    model = _pick_model(slug, models, current_model if slug == current_provider else "")
    return {"provider": slug, "model": model} if model else None


def _pick_model(provider: str, models: list[str], current: str) -> str:
    if not models:
        return _ask(f"Model on {provider}", current)
    from hermes_cli.auth_model_picker import prompt_model_selection
    from hermes_cli.models_pricing import get_pricing_for_provider
    try:
        pricing = get_pricing_for_provider(provider) or {}
    except Exception:
        pricing = {}
    return prompt_model_selection(list(models), current_model=current, pricing=pricing,
                                  confirm_provider=provider, title=f"Select the model for this seat:") or ""


def _pick_effort(route: dict[str, Any], current: str) -> None:
    from hermes_cli.main_provider_setup import _prompt_reasoning_effort_selection
    from hermes_constants import VALID_REASONING_EFFORTS
    effort = _prompt_reasoning_effort_selection(list(VALID_REASONING_EFFORTS), current_effort=current,
                                                default_label="Inherit parent")
    effort = current if effort is None else effort
    if effort:
        route["reasoning_effort"] = effort


def _pick_tier(route: dict[str, Any], current: str) -> None:
    _TIERS = (("fast", "priority"), ("ultrafast", "ultrafast"))
    offered = [word for word, tier in _TIERS if store.offered_tier(route, tier)]
    if not offered:
        if current and current != "normal":
            print(f"  This route offers no speed tier; dropped service_tier '{current}'.")
        return
    rows = ["Normal (no speed tier)", *(word.capitalize() for word in offered)]
    idx = _radio("Speed tier for this route:", rows,
                 selected=1 + offered.index(current) if current in offered else 0)
    word = current if idx < 0 else ("" if idx == 0 else offered[idx - 1])
    if word in offered:
        route["service_tier"] = word


def _pick_daybreak(route: dict[str, Any], current: bool) -> None:
    if not store.offered_daybreak(route):
        if current:
            print("  Daybreak is not offered for this route's model; dropped daybreak.")
        return
    idx = _radio("Daybreak for this route (OpenAI trusted access, reduced guardrails):", ["Off", "On"],
                 selected=1 if current else 0)
    if (current if idx < 0 else idx == 1):
        route["daybreak"] = True


def _pick_profile(current: Optional[str] = None) -> Optional[str]:
    from hermes_cli.profiles import get_active_profile_name, list_profile_names
    me = get_active_profile_name()
    names = [n for n in list_profile_names() if n != me]
    if not names:
        print("There are no other profiles to route to yet. Create one with `hermes profile create <name>`, "
              "give it a model, then run this again.")
        return None
    items = [n for n in names]
    chosen = curses_checklist("Which profile should this seat run as?", items,
                              {i for i, n in enumerate(names) if n == current}, cancel_returns={-1})
    if chosen == {-1} or not chosen:
        return None
    return names[list(chosen)[0]]


def _finish_ceo_pack(seats: Mapping[str, Mapping[str, Any]]) -> bool:
    """Switch to CEO Pack mode; False sends user back to seats list."""
    if not seats:
        print("No seats configured yet, so CEO Pack would give the agent nothing to pick. The mode is unchanged.")
        return True
    store.set_mode("ceo_pack")
    shown = ", ".join(f"{s} ({cfg['type']})" for s, cfg in seats.items())
    print(f"Delegation mode: CEO Pack ({shown}). delegation.model / delegation.provider are inactive in this mode. "
          f"New sessions pick this up; switch modes in a chat with /ceo delegation.")
    return True