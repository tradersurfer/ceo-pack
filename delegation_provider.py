"""Unified CEO Delegation Provider (v0.5.0).

Combines model_map and profile_routes into a single provider where each seat
can be configured with either a model route or a Hermes profile route.
"""

import json
import logging
from typing import Any, Mapping, Optional

from agent.delegation_provider import DelegationProvider, DelegationRouteError

logger = logging.getLogger(__name__)

BACKEND = "ceo_pack"
SETTINGS_PATH = f"delegation.backends.{BACKEND}"
SEATS = ["CEO", "CFO", "COO", "CTO", "CMO", "CLO", "Chief of Staff", "CRO"]


_WARNED: set[str] = set()


def _warn_once(message: str, *args: Any) -> None:
    text = message % args
    if text not in _WARNED:
        _WARNED.add(text)
        logger.warning(text)


def _text(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _validate_seat_config(entry: Mapping[str, Any], seat: str) -> str:
    """Validate a single seat's delegation config. Returns empty string if OK, error message otherwise."""
    if not isinstance(entry, Mapping):
        return f"seat {seat!r} config is not a mapping"
    
    route_type = entry.get("type")
    if route_type not in ("model_route", "profile_route", "default"):
        return f"seat {seat!r} needs type: model_route | profile_route | default"
    
    if route_type == "model_route":
        # Model route: needs provider/base_url + model
        if not _text(entry.get("provider")) and not _text(entry.get("base_url")):
            return f"seat {seat!r} model_route needs provider or base_url"
        if not _text(entry.get("model")):
            return f"seat {seat!r} model_route needs model"
        # Validate optional fields
        if entry.get("reasoning_effort") not in (None, "", "low", "medium", "high"):
            return f"seat {seat!r} reasoning_effort must be low|medium|high"
        if entry.get("service_tier") not in (None, "", "normal", "fast", "ultrafast"):
            return f"seat {seat!r} service_tier must be normal|fast|ultrafast"
        if entry.get("daybreak") not in (None, True, False):
            return f"seat {seat!r} daybreak must be true|false"
        if entry.get("daybreak") is True and not _is_codex(_text(entry.get("provider"))):
            return f"seat {seat!r} daybreak needs provider: openai-codex"
    
    elif route_type == "profile_route":
        # Profile route: needs profile name
        if not _text(entry.get("profile")):
            return f"seat {seat!r} profile_route needs profile name"
    
    return ""


def _is_codex(provider: str) -> bool:
    if not provider:
        return False
    try:
        from hermes_cli.models import normalize_provider
    except ImportError:
        return False
    return normalize_provider(provider) == "openai-codex"


def _daybreak_route(seat: str, route: Mapping[str, Any]) -> dict[str, Any]:
    """Add Daybreak request field to route, or raise DelegationRouteError."""
    try:
        from agent.codex_service_tiers import DAYBREAK_PROGRAM, daybreak_offered
        from agent.session_source import DAYBREAK_SOURCES, session_source_for
        from gateway.session_context import get_session_env
    except ImportError as exc:
        raise DelegationRouteError(
            f"Seat {seat!r} needs Daybreak, which this Hermes does not support ({exc.name}). Update Hermes or pick another route."
        ) from exc
    
    source = session_source_for(get_session_env("HERMES_SESSION_PLATFORM", "") or None)
    if str(get_session_env("HERMES_CRON_SESSION", "") or "").strip().lower() in {"1", "true", "yes", "on"}:
        source = "cron"
    
    # Check if this is owner's chat (for gateway-hosted chats)
    try:
        from agent.session_source import turn_daybreak_surface
        verdict = turn_daybreak_surface()
    except ImportError:
        verdict = None
    
    if verdict is False or (verdict is None and source not in DAYBREAK_SOURCES) or source == "cron":
        where = "the gateway does not count this chat as the owner's" if verdict is False and source != "cron" \
            else f"this session is {source!r}"
        raise DelegationRouteError(
            f"Seat {seat!r} uses Daybreak, which only runs from the owner's own CLI, TUI or Desktop chats "
            f"({where}). Pick another route."
        )
    
    if not daybreak_offered(route.get("model"), provider=route.get("provider"), base_url=route.get("base_url")):
        raise DelegationRouteError(
            f"Seat {seat!r} uses Daybreak, but this account's Codex catalog does not list Daybreak for "
            f"{route.get('model')!r} (or the catalog could not be read). Pick another route."
        )
    
    overrides = dict(route.get("request_overrides") or {})
    extra_body = dict(overrides.get("extra_body") or {})
    extra_body["access_programs"] = {**dict(extra_body.get("access_programs") or {}), "cyber": DAYBREAK_PROGRAM}
    return {**route, "request_overrides": {**overrides, "extra_body": extra_body}}


def configured_seats(settings: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return {seat: config} from backend settings, skipping invalid entries."""
    raw = settings.get("seats")
    if raw is None:
        return {}
    if not isinstance(raw, list):
        _warn_once("%s.seats must be a list of seat configs; ignoring.", SETTINGS_PATH)
        return {}
    
    result = {}
    seen = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            _warn_once("%s.seats[%d] is not a mapping; skipped.", SETTINGS_PATH, i)
            continue
        seat = _text(entry.get("seat"))
        if not seat:
            _warn_once("%s.seats[%d] has no seat name; skipped.", SETTINGS_PATH, i)
            continue
        seat_upper = seat.upper()
        # Map display names to canonical
        seat_map = {
            "CEO": "CEO", "CFO": "CFO", "COO": "COO", "CTO": "CTO",
            "CMO": "CMO", "CLO": "CLO", "CRO": "CRO",
            "CHIEF OF STAFF": "Chief of Staff", "COS": "Chief of Staff",
            "HERMES": "COO"
        }
        canonical = seat_map.get(seat_upper)
        if not canonical:
            _warn_once("%s.seats[%d] unknown seat %r; skipped.", SETTINGS_PATH, i, seat)
            continue
        if canonical in seen:
            _warn_once("%s.seats[%d] repeats seat %r; skipped.", SETTINGS_PATH, i, canonical)
            continue
        
        problem = _validate_seat_config(entry, canonical)
        if problem:
            _warn_once("%s.seats[%d] %s; skipped.", SETTINGS_PATH, i, problem)
            continue
        
        seen.add(canonical)
        config = {"type": entry.get("type"), "description": _text(entry.get("description"))}
        if config["type"] == "model_route":
            config.update({
                "provider": _text(entry.get("provider")),
                "base_url": _text(entry.get("base_url")),
                "model": _text(entry.get("model")),
                "reasoning_effort": entry.get("reasoning_effort"),
                "service_tier": entry.get("service_tier"),
                "daybreak": entry.get("daybreak") is True,
                "fallback_providers": entry.get("fallback_providers") if isinstance(entry.get("fallback_providers"), list) else None,
            })
        elif config["type"] == "profile_route":
            config.update({
                "profile": _text(entry.get("profile")),
            })
        result[canonical] = config
    return result


class CEODelegationProvider(DelegationProvider):
    """Unified CEO delegation provider: each seat gets a model route OR a profile route."""

    @property
    def name(self) -> str:
        return BACKEND

    @property
    def display_name(self) -> str:
        return "CEO Pack (unified)"

    @property
    def summary(self) -> str:
        return "Each C-Suite seat runs on its configured model route or Hermes profile."

    @property
    def route_keys(self) -> tuple[str, ...]:
        return ("daybreak",)

    def task_parameters(self, config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        seats_config = configured_seats(self.settings(config))
        if not seats_config:
            return {}
        
        lines = [
            "Which C-Suite seat executes this subagent. The seat's configured route (model or profile) is used.",
            *(f"{seat}: {cfg['description'] or cfg['type']}" for seat, cfg in seats_config.items()),
        ]
        return {
            "seat": {
                "type": "string",
                "enum": list(seats_config.keys()),
                "description": "\n".join(lines),
            }
        }

    def description(self, config: Mapping[str, Any]) -> str:
        seats_config = configured_seats(self.settings(config))
        if not seats_config:
            return ""
        return "Each task may set `seat` to run that subagent on the seat's configured route."

    def route_task(self, task: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
        settings = self.settings(config)
        seats_config = configured_seats(settings)
        
        chosen_seat = task.get("seat")
        if not chosen_seat:
            # No seat specified: use default route from task_runs or parent's route
            return {}
        
        # Core already validated enum
        seat_config = seats_config.get(chosen_seat)
        if not seat_config:
            # Seat not configured: fall back to parent route
            return {}
        
        if seat_config["type"] == "model_route":
            route = {
                "provider": seat_config.get("provider"),
                "base_url": seat_config.get("base_url"),
                "model": seat_config.get("model"),
                "reasoning_effort": seat_config.get("reasoning_effort"),
                "service_tier": seat_config.get("service_tier"),
                "fallback_providers": seat_config.get("fallback_providers"),
            }
            # Remove None values
            route = {k: v for k, v in route.items() if v}
            
            if seat_config.get("daybreak"):
                route = _daybreak_route(chosen_seat, route)
            
            return {**route, "label": f"{chosen_seat} (model: {seat_config.get('model')})"}
        
        elif seat_config["type"] == "profile_route":
            return {
                "profile": seat_config.get("profile"),
                "label": f"{chosen_seat} (profile: {seat_config.get('profile')})"
            }
        
        return {}


def get_seat_config_summary(config: Mapping[str, Any]) -> str:
    """Human-readable summary of seat configurations for display."""
    settings = config.get("delegation", {}).get("backends", {}).get(BACKEND, {})
    seats = configured_seats(settings)
    if not seats:
        return "No seats configured (run `hermes ceo delegation config`)"
    
    lines = ["CEO Pack delegation: each seat has a route"]
    for seat in SEATS:
        cfg = seats.get(seat)
        if not cfg:
            lines.append(f"  {seat}: (uses parent route)")
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
            lines.append(f"  {seat}: {where} · {model}{extra_str}")
        elif cfg["type"] == "profile_route":
            lines.append(f"  {seat}: profile {cfg.get('profile')}")
    return "\n".join(lines)