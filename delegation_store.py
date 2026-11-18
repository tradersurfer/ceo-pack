"""Read and write CEO Pack delegation settings.

Owns: delegation.backend (default | ceo_pack) and delegation.backends.ceo_pack.seats
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Sequence

from .delegation_provider import SEATS

MODES: dict[str, str] = {"default": "Default", "ceo_pack": "CEO Pack (unified)"}
MODE_BLURBS: dict[str, str] = {
    "default": "every subagent uses delegation.model / delegation.provider (else your main model)",
    "ceo_pack": "each C-Suite seat runs on its configured model route or Hermes profile",
}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ROUTE_KEYS = ("provider", "base_url", "model", "reasoning_effort", "service_tier", "daybreak", "fallback_providers")


def delegation_section(config: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    """The delegation section of the active profile's effective config (read-only)."""
    if config is None:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly() or {}
    section = config.get("delegation") if isinstance(config, Mapping) else None
    return section if isinstance(section, Mapping) else {}


def current_mode(section: Mapping[str, Any]) -> str:
    raw = section.get("backend")
    return raw.strip().lower() if isinstance(raw, str) and raw.strip() else "default"


def _block(section: Mapping[str, Any], mode: str) -> Mapping[str, Any]:
    backends = section.get("backends")
    block = backends.get(mode) if isinstance(backends, Mapping) else None
    return block if isinstance(block, Mapping) else {}


def _entries(block: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    raw = block.get(key)
    return [dict(e) for e in raw if isinstance(e, Mapping)] if isinstance(raw, list) else []


def _text(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def configured_seats(section: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return {seat: config} from the ceo_pack backend's seats list."""
    block = _block(section, "ceo_pack")
    raw = block.get("seats")
    if not isinstance(raw, list):
        return {}
    result = {}
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        seat = _text(entry.get("seat"))
        if seat:
            result[seat] = dict(entry)
    return result


def _write(mutate: Callable[[dict[str, Any]], None]) -> None:
    """Raises RuntimeError when the config must not be written."""
    from hermes_cli.config import (atomic_config_write, format_managed_message, get_config_path, is_managed,
                                   require_readable_config_before_write)
    if is_managed():
        raise RuntimeError(format_managed_message("change delegation settings"))
    path = get_config_path()
    data = require_readable_config_before_write(path)
    section = data.get("delegation")
    if not isinstance(section, dict):
        section = data["delegation"] = {}
    mutate(section)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_config_write(path, data)


def _backend_block(section: dict[str, Any], mode: str) -> dict[str, Any]:
    backends = section.get("backends")
    if not isinstance(backends, dict):
        backends = section["backends"] = {}
    block = backends.get(mode)
    if not isinstance(block, dict):
        block = backends[mode] = {}
    return block


def set_mode(mode: str) -> None:
    """Persist delegation.backend (default | ceo_pack)."""
    if mode not in MODES:
        raise ValueError(f"Unknown delegation mode {mode!r}; use one of: {', '.join(MODES)}.")
    _write(lambda section: section.__setitem__("backend", mode))


def save_seat(seat_config: Mapping[str, Any]) -> None:
    """Add or update a single seat's config in ceo_pack.seats."""
    seat = _text(seat_config.get("seat"))
    if not seat:
        raise ValueError("seat config must include 'seat'")
    def mutate(section: dict[str, Any]) -> None:
        block = _backend_block(section, "ceo_pack")
        seats = block.get("seats")
        if not isinstance(seats, list):
            seats = block["seats"] = []
        # Remove existing entry for this seat
        seats[:] = [s for s in seats if isinstance(s, Mapping) and _text(s.get("seat")) != seat]
        seats.append(dict(seat_config))
    _write(mutate)


def remove_seat(seat: str) -> None:
    """Remove a seat's config from ceo_pack.seats."""
    def mutate(section: dict[str, Any]) -> None:
        block = _backend_block(section, "ceo_pack")
        seats = block.get("seats")
        if isinstance(seats, list):
            seats[:] = [s for s in seats if isinstance(s, Mapping) and _text(s.get("seat")) != seat]
    _write(mutate)


# --- validation -----------------------------------------------------------------------------

def _provider_known(provider: str) -> bool:
    from hermes_cli.config import get_compatible_custom_providers, load_config_readonly
    from hermes_cli.providers import resolve_provider_full
    config = load_config_readonly() or {}
    return resolve_provider_full(provider, config.get("providers"), get_compatible_custom_providers(config)) is not None


def _tier_problem(route: Mapping[str, Any]) -> str:
    word = _text(route.get("service_tier"))
    if not word:
        return ""
    from agent.fast_mode import parse_exact_service_tier
    try:
        tier = parse_exact_service_tier(word)
    except ValueError:
        return f"Unknown speed tier {word!r}; use normal, fast or ultrafast."
    if not tier:
        return ""
    if not offered_tier(route, tier):
        return (f"Speed tier {word!r} is not offered for {route.get('model')} on "
                f"{route.get('provider') or route.get('base_url')}; every spawn on this route would be refused.")
    return ""


def offered_tier(route: Mapping[str, Any], tier: str) -> bool:
    """Whether the route's model/provider offers tier (priority or ultrafast), by the same gate as /fast."""
    from hermes_cli.models import resolve_fast_mode_overrides
    return bool(resolve_fast_mode_overrides(
        _text(route.get("model")) or None, provider=_text(route.get("provider")) or None,
        base_url=_text(route.get("base_url")) or None, tier=tier))


def offered_daybreak(route: Mapping[str, Any]) -> bool:
    """Whether Daybreak (OpenAI's daybreak_blue) is offered for the route's model."""
    try:
        from agent.codex_service_tiers import daybreak_offered
    except ImportError:
        return False
    return daybreak_offered(_text(route.get("model")) or None, provider=_text(route.get("provider")) or None,
                            base_url=_text(route.get("base_url")) or None)


def _daybreak_problem(route: Mapping[str, Any]) -> str:
    daybreak = route.get("daybreak")
    if daybreak in (None, False):
        return ""
    if daybreak is not True:
        return f"daybreak must be true or false, not {daybreak!r}."
    provider = _text(route.get("provider"))
    from hermes_cli.models import normalize_provider
    if not provider or normalize_provider(provider) != "openai-codex":
        return "Daybreak needs provider openai-codex (the ChatGPT/Codex subscription)."
    if not offered_daybreak(route):
        return (f"Daybreak is not listed for {route.get('model')} in this account's Codex catalog (or it could not "
                f"be read); use the base model (e.g. gpt-6-sol), not a *-daybreak-* alias.")
    return ""


def validate_seat_config(seat_config: Mapping[str, Any], others: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Problems that would make core skip or refuse this seat's route (empty list = savable)."""
    problems: list[str] = []
    seat = _text(seat_config.get("seat"))
    if seat not in SEATS:
        problems.append(f"Unknown seat {seat!r}. Valid: {', '.join(SEATS)}")
    route_type = seat_config.get("type")
    if route_type not in ("model_route", "profile_route", "default"):
        problems.append(f"Seat {seat!r} needs type: model_route | profile_route | default")
    if route_type == "model_route":
        if not _text(seat_config.get("provider")) and not _text(seat_config.get("base_url")):
            problems.append(f"Seat {seat!r} model_route needs provider or base_url")
        if not _text(seat_config.get("model")):
            problems.append(f"Seat {seat!r} model_route needs model")
        provider = _text(seat_config.get("provider"))
        if provider and not _provider_known(provider):
            problems.append(f"Unknown provider {provider!r} (see `hermes model` for the providers Hermes knows).")
        effort = seat_config.get("reasoning_effort")
        if effort not in (None, "", "low", "medium", "high"):
            problems.append(f"Unknown reasoning effort {effort!r}; use low, medium, or high.")
        tier = _tier_problem(seat_config) if not problems else ""
        if tier:
            problems.append(tier)
        daybreak = _daybreak_problem(seat_config) if not problems else ""
        if daybreak:
            problems.append(daybreak)
    elif route_type == "profile_route":
        if not _text(seat_config.get("profile")):
            problems.append(f"Seat {seat!r} profile_route needs profile name")
    return problems


# --- display ----------------------------------------------------------------------------------

def mode_label(mode: str) -> str:
    return MODES.get(mode, mode)


def describe(section: Mapping[str, Any]) -> str:
    """The --show text: the mode and what it routes."""
    mode = current_mode(section)
    lines = [f"CEO Delegation mode: {mode_label(mode)} (delegation.backend: {mode})"]
    if mode == "ceo_pack":
        seats = configured_seats(section)
        lines.append(f"  Seat routes ({len(seats)}):")
        for seat in SEATS:
            cfg = seats.get(seat)
            if not cfg:
                lines.append(f"    {seat}: (uses parent route)")
            elif cfg["type"] == "model_route":
                where = _text(cfg.get("provider")) or _text(cfg.get("base_url"))
                model = _text(cfg.get("model"))
                extras = []
                if cfg.get("reasoning_effort"):
                    extras.append(f"reasoning {cfg['reasoning_effort']}")
                if cfg.get("service_tier") and cfg["service_tier"] != "normal":
                    extras.append(cfg["service_tier"])
                if cfg.get("daybreak"):
                    extras.append("daybreak")
                extra_str = f" [{', '.join(extras)}]" if extras else ""
                lines.append(f"    {seat}: {where} · {model}{extra_str}")
                if cfg.get("description"):
                    lines.append(f"      → {cfg['description']}")
            elif cfg["type"] == "profile_route":
                lines.append(f"    {seat}: profile {cfg.get('profile')}")
                if cfg.get("description"):
                    lines.append(f"      → {cfg['description']}")
        if not seats:
            lines.append("    (none yet: run `hermes ceo delegation config`)")
    elif mode == "default":
        model = _text(section.get("model")) or "your main model"
        provider = _text(section.get("provider")) or _text(section.get("base_url"))
        lines.append(f"  Subagents use delegation.model: {model}{f' on {provider}' if provider else ''}")
    else:
        lines.append("  delegation.model / delegation.provider are inactive in this mode.")
    return "\n".join(lines)