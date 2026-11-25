# Delegation Reconciliation — ceo-pack v0.5.0 Phase 3

**Date:** 2026-11-18. **Sources read in full:**
- `NousResearch/hermes-official-plugins` @ `e726eb7d5f6e7f4b2f3d149e482fcfbc7304b275` — `advanced-delegation/` (`__init__.py`, `model_map.py`, `profile_routes.py`, `store.py`, `cli.py`, `slash.py`, `plugin.yaml`, `README.md`)
- `NousResearch/hermes-agent` local checkout @ `d33d4fd9e7b57e16316ca953523bba596e4f6d5e` — `agent/delegation_provider.py` (host ABC), `agent/delegation_registry.py`, `tools/delegate_tool.py`, `tools/delegate_tool_routes.py`, `tools/delegate_tool_child_run.py`, `agent/turn_usage.py`
- ceo-pack work copy `~/workspace/ceo-pack-v050` — `delegation_provider.py`, `delegation_store.py`, `delegation_cli.py`, `delegation_slash.py`, `__init__.py` (register), `tasks.py`, `tools.py` (`ceo_task_assign`), `dashboard/plugin_api.py`

**Verdict in one line:** the ceo-pack provider is a faithful port of the two real providers merged into one seat-keyed backend, with two real divergences to fix (reasoning-effort values, execution metadata not recorded), one deliberate hardening (no keys ever carried), and one gap that needs the owner's call (Type 1 assign gate vs a pinned test). It does **not** introduce a second delegation architecture: core owns the lifecycle; this provider only answers "which route does this seat take".

## 0. The host contract (what core owns — never reimplement)

From `agent/delegation_provider.py` (host ABC docstring + code):

- **Core owns the whole lifecycle**: spawn, isolation, roles, depth, concurrency, background units, stop fan-out, results, `output_schema`, progress. "A provider only answers 'which route does this task take'."
- **Route vocabulary** (`ROUTE_KEYS` — anything else in a provider's answer is ignored): `model`, `provider`, `base_url`, `api_key`, `api_mode`, `request_overrides`, `reasoning_effort`, `service_tier`, `fallback_providers`, `profile`, `label`.
- **Reserved task parameters** (`RESERVED_TASK_PARAMETERS`): `goal`, `context`, `index`, `output_schema`, `images`, `group`, `role`, `toolsets`, `acp_command`, `acp_args`, `provider`, `model`, `base_url`, `api_key`, `api_mode`. A provider-declared parameter with one of these names is refused at schema-build time; raw route fields never become model-facing parameters.
- **Settings live under `delegation.backends.<backend name>`** (`DelegationProvider.settings()`); never beside the core `delegation` keys.
- **`is_available()` false fails delegation closed**: a `delegate_task` call under that backend is a tool error naming it, never a run on another route.
- **Per-task parameters**: `task_parameters()` is called at session build (deterministic for a given config — the schema is part of the cached prompt prefix). An `enum` here is enforced by core *before* `route_task` runs; a task carrying an undeclared parameter is refused before it reaches the provider.
- **Refusal**: raise `DelegationRouteError` to refuse a task; the message becomes the `delegate_task` tool error the model reads.
- **Backend selection**: `agent/delegation_registry.py::get_active_provider` — `delegation.backend` selects; unset = `default`. An unregistered or unavailable backend raises `DelegationRouteError` naming the backend and the fix ("No subagent was started"). Unknown backend = fail closed, never a silent fallback.
- **Per-task refusal is isolated** (`tools/delegate_tool_routes.py::TaskRoute.refused_entry`): a task its backend cannot route is refused alone (its entry carries the error); siblings still run.
- **Prompt-cache rule**: the mode is part of the chat's cached prompt; switching modes applies next session by default, `--now` rebuilds tools once (one cache break). Both slash implementations honor this.

## 1. Execution metadata — what the host actually reports per delegated task

From `tools/delegate_tool_child_run.py` (`_route_report`, result-entry assembly) and `agent/turn_usage.py`:

A routed task's result entry carries:
- `route`: `{backend, label?, provider, model, reasoning_effort, service_tier, fallback_activated?}` — read off the child **at result time**, so a fallback provider that took over shows. `None` for the default backend's unlabelled route. Refused tasks carry `route: {backend, label?, refused: true}`.
- `tokens`: `{input, output}` — the child's `session_prompt_tokens` / `session_completion_tokens`.
- `cost_usd` (rounded, model-visible) + `cost_status` (`"unknown"` when the host cannot compute it; otherwise the status string from `session_cost_status`).
- `api_calls`, `duration_seconds`, `exit_reason`, `truncated` (budget-exhausted flag), `failure_reason` (classified code, e.g. `rate_limit`, `billing`), `tool_trace`.
- `_child_cost_usd` is folded into the parent's session cost by the aggregator and stripped before serializing to the model; `cost_usd` is the model-visible per-delegation spend.

**Consequences for ceo-pack:** cost is genuinely available when the host supplies it and honestly `"unknown"` when it doesn't — there is nothing to estimate and nothing to invent. The recordable set is exactly the allowlisted fields above. `summary`/`error` prose is never stored (it can echo caller text).

## 2. Piece-by-piece reconciliation

Verdicts: **REUSE** = take as-is from the real source. **ADAPT** = same shape, ceo-pack-specific change, reason given. **NATIVE** = host owns it; ceo-pack must not reimplement. **DIVERGENCE** = differs from the real source; fix or documented decision.

### 2.1 Provider registration & base class

| Piece | Real source | ceo-pack | Verdict |
|---|---|---|---|
| Register via `ctx.register_delegation_provider()` | `advanced-delegation/__init__.py` registers both providers; on missing `agent.delegation_provider` logs one warning, registers nothing | `__init__.py` registers `CEODelegationProvider()`; missing-host guard checks `exc.name` top-level `"agent"` (task-2 fix) | **REUSE** (shape identical; guard already fixed) |
| Base class | `agent.delegation_provider.DelegationProvider` (+ `ProviderBase`) | try/except import with a stand-in replicating `settings()` verbatim | **ADAPT** — stand-in is test-only; inside Hermes the real class is used. Keep. |
| `requires_hermes >=0.22.5` | `advanced-delegation/plugin.yaml` declares it; README: first release with pluggable providers | ceo-pack `plugin.yaml` already declares `>=0.22.5` | **REUSE** |
| `settings()` | Host ABC implementation | Stand-in replicates it verbatim (verified in task 2) | **REUSE** |

### 2.2 Per-task parameter: `seat` vs `option` / `profile`

| Piece | Real source | ceo-pack | Verdict |
|---|---|---|---|
| Parameter name | `option` (model_map), `profile` (profile_routes) | `seat` | **ADAPT** — `seat` is not in `RESERVED_TASK_PARAMETERS`, so it is legal. It is the ceo-pack product decision: the model picks a *seat*, and the seat's config decides model vs profile. Enum = configured seats; core enforces it before `route_task`. |
| `task_parameters()` | Returns `{param: {type, enum, description}}`, `{}` when nothing configured | Same shape; `{}` when no seats configured | **REUSE** |
| `description()` | One line, `""` when unconfigured | Same | **REUSE** |
| `route_keys` | model_map declares `("daybreak",)` so settings editors may write it | ceo-pack declares `("daybreak",)` | **REUSE** |

### 2.3 `route_task` — model route path

| Piece | Real source | ceo-pack | Verdict |
|---|---|---|---|
| Route fields copied | `ROUTE_FIELDS = (provider, model, base_url, api_key, api_mode, reasoning_effort, service_tier)` + `fallback_providers` list | `provider, base_url, model, reasoning_effort, service_tier, fallback_providers` (falsy values dropped) | **ADAPT, deliberate hardening** — `api_key` / `api_mode` are never copied out of seat config (test `test_extra_keys_never_survive_config` pins this). The host credential resolver supplies provider credentials; a seat config must never carry keys. Stricter than real, keep. |
| `label` | model_map: option name; profile_routes: profile name | `"CFO (model: anthropic/claude-sonnet-5.5)"` / `"CFO (profile: builder)"` | **ADAPT** — more informative; host accepts any short label. Keep. |
| Unknown reasoning effort | Core refuses the task ("An unknown reasoning_effort refuses the task") | Same — host enforces; provider passes the value through | **NATIVE** |
| Unknown service tier | Core checks like `/fast`; un-offered tier refuses the task | Same — host enforces at spawn; store pre-checks with `offered_tier` | **NATIVE** |

### 2.4 `route_task` — profile route path

| Piece | Real source | ceo-pack | Verdict |
|---|---|---|---|
| Return shape | `{"profile": name, "label": name}` | `{"profile": name, "label": "Seat (profile: name)"}` | **REUSE** (label wording only) |
| Profile existence check | None at route time — core binds the profile at spawn and fails loudly if missing | None (same) | **NATIVE** — do not add an existence check; core is the enforcement point. |
| Profile route ignores other route keys | "A profile route ignores the other route keys; the profile's own config decides them" | Same (only `profile` is returned) | **REUSE** |

### 2.5 `route_task` — no seat / unknown seat / default

| Piece | Real source | ceo-pack | Verdict |
|---|---|---|---|
| No choice given | Runs on the mode's `default` when set, else the parent's route (`{}`) | Returns `{}` (parent route) | **ADAPT, deliberate omission** — ceo-pack has no `default` seat key. Observable behavior with no default set is identical (parent route). Adding a `default` would be a new feature, not a reconciliation fix. Out of scope. |
| `default` value not in options | Warn once; tasks without a choice use the parent route | N/A (no default key) | **NATIVE / N/A** |
| Chosen value outside the enum | Impossible — core enforces the enum before `route_task` | Same guarantee (`seat` enum); the `seats_config.get()` miss path only triggers on mid-session config change | **DIVERGENCE, documented decision** — ceo-pack returns `{}` (parent route) for an unconfigured seat; the real model_map would `KeyError` on `options[chosen]`. `{}` matches the host's own "empty route = inherit parent" semantic and is strictly safer than the real provider's crash. The existing test `test_no_seat_and_unknown_seat_fall_back_to_parent` pins this behavior. Keep; do not change. |

### 2.6 Daybreak

| Piece | Real source | ceo-pack | Verdict |
|---|---|---|---|
| `_daybreak_route` | `model_map.py`: imports `DAYBREAK_PROGRAM, daybreak_offered`, `DAYBREAK_SOURCES, session_source_for`, `get_session_env`; cron forced to `"cron"`; gateway verdict via `_gateway_owner_chat()`; raises `DelegationRouteError` with owner-surface guidance; sets `request_overrides.extra_body.access_programs.cyber = DAYBREAK_PROGRAM` | Same logic, `_gateway_owner_chat` inlined (calls `turn_daybreak_surface()` directly, same `verdict is False / None / "cron"` branches) | **REUSE** — verified line-by-line equivalent. |
| Daybreak requires `openai-codex` | `_entry_problem` + `_daybreak_problem` | `_validate_seat_config` + `_daybreak_problem` (same check) | **REUSE** |
| Daybreak offered check | `daybreak_offered(model, provider, base_url)`; `False` when the catalog cannot be read | Same via `offered_daybreak` | **REUSE** |

### 2.7 Validation — provider (`_validate_seat_config`) vs real `_entry_problem`

| Check | Real `_entry_problem` | ceo-pack | Verdict |
|---|---|---|---|
| Entry is a mapping | yes | yes | **REUSE** |
| Name + description required | yes ("needs a name and a description") | Name: seat must canonicalize to a known seat. Description: **not required** (falls back to the type in `task_parameters`) | **DIVERGENCE, documented decision** — the real provider *skips* description-less entries entirely. ceo-pack tolerates them. Requiring it would break pinned tests (`test_valid_seats_parsed` uses description-less profile/default seats). The editor path (CLI) still requires a description. Keep the tolerant read; do not change. |
| Model required | yes | yes | **REUSE** |
| Provider or base_url required | yes | yes | **REUSE** |
| `reasoning_effort` values | Not checked in `_entry_problem`; checked in `store.validate_route` via `parse_reasoning_effort` against `VALID_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")`, plus `"none"`/false = disabled | **Hardcoded `("low", "medium", "high")`** in both provider and store | **DIVERGENCE — FIX IN STEP 2.** ceo-pack wrongly rejects `minimal`, `xhigh`, `max`, `ultra`, and `"none"`. Fix: validate with host `parse_reasoning_effort` when importable, else the full `VALID_REASONING_EFFORTS` tuple (+ `"none"`/false as disabled → normalize to absent). |
| `service_tier` values | Not in `_entry_problem`; store checks via `parse_exact_service_tier` + offered check | Provider checks membership in `(normal, fast, ultrafast)`; store checks offered | **REUSE** (same split as real) |
| `daybreak` must be bool + codex | yes | yes | **REUSE** |
| Unknown keys stripped | Route keeps only `ROUTE_FIELDS` + description + daybreak flag | `configured_seats` copies only the allowlisted keys | **REUSE** |

### 2.8 Validation — store (`validate_seat_config`) vs real `validate_route`

| Check | Real `validate_route` | ceo-pack | Verdict |
|---|---|---|---|
| Name regex + uniqueness | `_NAME_RE`, unique among others | Seat must be in the fixed 8-seat list; `save_seat` replaces by seat so duplicates cannot persist | **ADAPT** — seat names are a closed set by product design; equivalent protection. |
| Description required | yes | no (see 2.7) | **DIVERGENCE, documented** (same reason) |
| Model / provider-or-base_url required | yes | yes | **REUSE** |
| Provider known | `_provider_known` via `resolve_provider_full` | Same | **REUSE** |
| Reasoning effort | `parse_reasoning_effort` vs `VALID_REASONING_EFFORTS` | hardcoded `(low, medium, high)` | **DIVERGENCE — FIX IN STEP 2** (same fix as 2.7) |
| Tier offered | `_tier_problem` → `offered_tier` via `resolve_fast_mode_overrides` | Same | **REUSE** |
| Daybreak offered | `_daybreak_problem` → `offered_daybreak` | Same | **REUSE** |
| Writes via `atomic_config_write`, managed-install refusal, "blank default never removed" | yes | Same (`_write`, `is_managed`, `require_readable_config_before_write`) | **REUSE** |

### 2.9 CLI (`hermes ceo delegation config`) vs real `hermes delegation config`

| Piece | Real source | ceo-pack | Verdict |
|---|---|---|---|
| Structure | Mode picker → route editor (model routes) / profile multi-select; `--show` prints setup; no-TTY refuses interactive edit; writes via `store` | Same structure, seat-based; `--show`; no-TTY refusal | **REUSE** |
| Provider/model picker | Reuses `hermes model` curses pieces (`build_aux_picker_rows`, model picker, pricing) | Same imports (`build_aux_picker_rows`, `prompt_model_selection`, `get_pricing_for_provider`, `_prompt_reasoning_effort_selection`) | **REUSE** |
| Validation before save | `store.validate_route` | `store.validate_seat_config` | **REUSE** (modulo the effort-values fix in 2.8) |
| Tier picker | Offers only tiers the route offers; drops a tier the route does not offer | Same (`_pick_tier` via `store.offered_tier`) | **REUSE** |
| Daybreak picker | Only offered when `offered_daybreak`; drops otherwise | Same (`_pick_daybreak`) | **REUSE** |
| Unknown-key preservation on edit | Real editor writes only `ROUTE_KEYS` (`save_routes`/`save_profiles` write the edited list) | **ceo-pack `_edit_seat_route` re-saves unknown keys** (`for key, value in existing.items(): if key not in route and key not in store.ROUTE_KEYS and (same_endpoint or key == "fallback_providers")`) — an `api_key` smuggled into a seat config survives CLI edits | **DIVERGENCE — FIX IN STEP 2.** Drop unknown keys on save; only `seat, type, description, profile` + `ROUTE_KEYS` may persist. The provider already strips them at route time, so this is defense-in-depth, not a live leak — but the editor must not preserve what the provider refuses to carry. |
| Seat lookup in `_edit_seat_route` | N/A (routes are picked from a list by index) | Fragile identity search (`{**others, **{"": existing}}` + `cfg is existing`) to recover the seat name | **DIVERGENCE — FIX IN STEP 2.** Pass the seat name explicitly into `_edit_seat_route` instead of reverse-looking it up by object identity. |
| `_finish_ceo_pack` refuses empty setup | Real CLI has no equivalent gate (a mode with no routes is switchable but useless) | Refuses to switch with zero seats configured | **ADAPT** — stricter, matches the slash `_ready` gate. Keep. |

### 2.10 Slash (`/ceo delegation`) vs real `/delegation`

| Piece | Real source | ceo-pack | Verdict |
|---|---|---|---|
| Aliases | `default`/`1`/`off`/`builtin`; `model_map`/`2`/`models`/…; `profile_routes`/`3`/`profiles`/… | `default`/`1`/`off`/`builtin`; `ceo_pack`/`2`/`unified`/`ceo`/`pack` | **REUSE** (mode-appropriate aliases) |
| `_parse` (unknown mode/flag) | Same structure | Same | **REUSE** |
| `_ready` (refuse switch to unconfigured mode) | Same — `[]` refuses with "nothing set up yet" | Same | **REUSE** |
| `_problem` (backend unavailable via `ctx.delegation_backends`) | Same | Same | **REUSE** |
| `_save` / `apply_now` with `TypeError` fallback for pre-`apply_now` cores | Same | Same | **REUSE** |
| Next-session default, `--now` = one cache break | Same, with the prompt-cache rationale in the docstring | Same | **REUSE** |
| `_detail` / `_mode_default` | Shows the mode's default route | ceo-pack has no `default` key, so `_mode_default` lists seats instead | **ADAPT** (follows from 2.5) |

### 2.11 Execution metadata recording — the gap

| Piece | Real source (host) | ceo-pack today | Verdict |
|---|---|---|---|
| Result entry carries `route` / `tokens` / `cost_usd` / `cost_status` / `api_calls` / `duration_seconds` / `exit_reason` / `failure_reason` | `tools/delegate_tool_child_run.py` | **Nothing records it.** `tasks.py` has `model_route` (the *requested* route, set at assign time) and a free-form `metadata` JSON column, but no function records a delegate result. The dashboard strips `metadata` from every API response (`_TASK_STRIP`), so even hand-written execution data would never reach the UI. | **GAP — FIX IN STEP 2.** Add `tasks.record_execution(task_id, entry)`: accept only the host allowlisted fields (§1), coerce/validate types, store under `metadata["execution"]`, log a history note. Add a `tasks.get_execution(task_id)` reader. Surface it in the dashboard task-detail response as an explicit `execution` object (validated shape); keep `metadata` itself stripped. Dashboard shows route/provider/model/tokens/cost/duration, `"unknown"` for cost when `cost_status` is `"unknown"` or absent. Never estimate a cost. |
| `model_route` column | N/A (host has no task registry) | Set at assign time from the `model_route` arg | **ADAPT** — keep as the *requested* route evidence; `execution.route` is the *actual* route evidence. The two must never be conflated in the UI: label them "requested" vs "ran on". |

### 2.12 Type 1 gating — the gap that needs the owner's call

- **Provider layer: correct by design.** A delegation provider cannot gate — it only answers "which route". The host enforces nothing about Type 1; `ceo_assign`/`make_tool_call` flag Type 1 and require owner approval in the tools layer (verified live: L2-1–L2-5, and `test_type1_stays_gated_whatever_the_route` asserts the route does not change the gate).
- **Assign layer: NOT gated, and the suite pins it.** `tools.ceo_task_assign` accepts `proposed` **or** `approved` tasks with no check on `approval_required`. A Type 1 task can therefore be assigned — and then run — without ever being approved. `tests/test_v050_handlers.py::test_all_seven_success_results_verify` creates an `approval_required=True` task and assigns it while still proposed, expecting `status == "success"`. Adding a handler gate (`approval_required` + not approved → refuse assign) would break that pinned test.
- **Execution layer: no hook exists yet.** `delegate_task` itself runs outside ceo-pack tools. The only mechanism that could refuse a delegated execution of an unapproved Type 1 task is the `pre_tool_call` gate — which is **design-only** by the owner's decision (warn mode first, then block with owner-approval token).
- **Decision for Step 2: do not add the handler gate; do not change the test.** Documented here for the owner: closing this needs either (a) a handler gate in `ceo_task_assign` (breaks the pinned test — needs the owner's explicit yes to change the test), or (b) the `pre_tool_call` gate (needs the owner's build decision). Until one lands, Type 1 gating rests on the orchestration workflow (assign only after approval) and is **not** enforced by code at the delegation boundary. Say so plainly in the report; do not claim otherwise.

### 2.13 `pre_tool_call` gate

**Out of scope (design-only).** Per the plan and the owner's decision: warn mode first, then block with an owner-approval token — only when the owner says build. Nothing in Step 2 implements blocking. This doc records the dependency (§2.12) so the gate's design, when it comes, starts from the real contract: the host's `pre_tool_call` hook can veto any tool call by returning `{"action": "block", "message": ...}` (verified in the handoff from `hermes_cli/plugins.py` `VALID_HOOKS`).

## 3. Step 2 fix list (what changes, in order)

1. **Reasoning-effort values** (`delegation_provider.py::_validate_seat_config`, `delegation_store.py::validate_seat_config`): accept the host's full `VALID_REASONING_EFFORTS` (`minimal, low, medium, high, xhigh, max, ultra`) plus `"none"`/false as disabled. Prefer host `parse_reasoning_effort` when importable; fall back to the tuple. Route passthrough unchanged (falsy values already dropped).
2. **Execution recording** (`tasks.py`): new `record_execution(task_id, entry, db_path=None)` + `get_execution(task_id, db_path=None)`. Allowlist only the §1 fields; coerce numbers (`cost_usd` float ≥ 0, `tokens.input/output` ints ≥ 0, `api_calls` int ≥ 0, `duration_seconds` float ≥ 0); `cost_status` kept as the host's string or `"unknown"`; `route` kept only with allowlisted sub-keys; `failure_reason`/`exit_reason` kept as short strings; drop everything else (including `summary`, `error` prose, `_child_*`, `tool_trace`, any `api_key`-shaped key). Never raise on a malformed entry — return `False`. Missing task → `False`. Store under `metadata["execution"]` (no schema change; existing DBs unaffected). Log a history note (`"execution recorded: <provider>/<model>"` or `"execution recorded: route refused"`).
3. **Dashboard surfacing** (`dashboard/plugin_api.py`, `dashboard/dist/index.js`, `dashboard/API_CONTRACT.md`): task-detail response gains an explicit `execution` object (from `get_execution`, validated shape) while `metadata` stays stripped. UI renders requested route (`model_route`) vs ran-on route (`execution.route`), tokens, cost + cost_status, duration, api_calls, exit/failure reason; `"unknown"` wherever the host supplied nothing. No cost estimation anywhere.
4. **CLI hardening** (`delegation_cli.py`): (a) drop unknown keys on save — only `seat, type, description, profile` + `store.ROUTE_KEYS` persist; (b) pass the seat name explicitly into `_edit_seat_route` (remove the identity-search lookup).
5. **No change**: provider `seat` parameter, `{}` fallback for unknown seat, tolerant description read, no `default` key, no `api_key`/`api_mode` carriage, no handler Type 1 gate, no `pre_tool_call` block.

## 4. What stays native Hermes (never reimplement in ceo-pack)

- Subagent lifecycle: spawn, isolation, roles, depth, concurrency, background units, stop fan-out, results, `output_schema`, progress (`delegate_tool.py`, `delegate_tool_routes.py`).
- Credential resolution for routes (the same resolver `delegation.*` uses); profile binding/scope at spawn (`child_profile_scope`).
- Cost estimation (`agent/usage_pricing.py::estimate_usage_cost`, `session_estimated_cost_usd`); ceo-pack only *records* the host's reported `cost_usd`/`cost_status`.
- Prompt-cache handling for mode switches (`--now` semantics; `ctx.delegation_backends`).
- Mode availability: unknown/unavailable backend fails closed in core (`get_active_provider`).
- Speed-tier and Daybreak enforcement at spawn (core re-checks; ceo-pack only pre-checks in the editor so users are not surprised).

## 5. Open questions for Claude (wiring review)

1. `is_available()`: ceo-pack does not override it (host default `True`). Should it return `False` when the backend has zero seats configured, so `delegate_task` under `ceo_pack` fails closed with a named error instead of routing everything to the parent route? Real providers do not override it either — but their empty-config state still routes (to parent/default). Flag, don't decide here.
2. Profile existence at route time: real provider does not check; core fails at spawn. Confirm that is the intended ceo-pack behavior (no check added).
3. `record_execution` storage: `metadata["execution"]` (no schema change) vs dedicated columns. Chosen: metadata JSON, for zero-migration compatibility with existing DBs. Confirm.
4. The `default`-key omission (§2.5): confirm parent-route fallback is the intended semantic for tasks that set no seat.
5. CLI unknown-key drop (§3.4): confirm no legitimate editor flow depends on preserving extra keys.

## 6. Implementation status (Phase 3, Step 2 — 2026-11-18)

All of §3 implemented and verified; nothing in §3 was skipped. Files changed:

- `delegation_provider.py` — new `effort_problem()` helper: prefers the host's `parse_reasoning_effort` when importable, else the full `VALID_REASONING_EFFORTS` tuple (minimal, low, medium, high, xhigh, max, ultra) + `"none"`/`False` = disabled. `_validate_seat_config` uses it; the old hardcoded `(low, medium, high)` check is gone.
- `delegation_store.py` — `validate_seat_config` uses the same `effort_problem()` (imported from the provider); no second effort list exists anywhere.
- `tasks.py` — new `record_execution(task_id, entry)` / `get_execution(task_id)` / `execution_from_metadata(metadata)` / `_clean_execution(entry)`. Allowlisted fields only (route, tokens, cost_usd, cost_status, api_calls, duration_seconds, exit_reason, failure_reason, status, task_index, truncated); `cost_status` defaults to `"unknown"`; stored under `metadata["execution"]`, no schema change; never raises; does not touch task status or history.
- `dashboard/plugin_api.py` — `_public_task` gains `execution` (validated record or `null`); `metadata` stays stripped.
- `dashboard/dist/index.js` — TaskDetail gains a "Delegated execution" section: requested route (`model_route`) vs ran-on route, tokens, cost + cost_status, duration, api_calls, exit/failure reason, recorded time; `"unknown"` wherever the host supplied nothing. No CSS or other component changed.
- `dashboard/API_CONTRACT.md` — `execution` object documented (field table + cost honesty rule).
- `delegation_cli.py` — new `_preserved_extra_keys()`: only `fallback_providers` survives an edit; `_edit_seat_route` now takes the seat name explicitly (old object-identity lookup removed). The old key-preservation loop (which carried unknown keys on same-endpoint edits) is gone.
- Tests: `tests/test_v050_delegation.py` +20 (5 effort-parity, 8 execution-recording, 3 CLI key-preservation) and `tests/test_v050_dashboard.py` +4 (execution visible in detail/list, unknown cost honest, null when none). **286/286 pass, 20x identical, demo rc=0, validator 15/15.**

Deliberately NOT done (still awaiting the owner's call, per §2.8 / §3.5): the Type 1 handler gate in `ceo_task_assign` (pinned test `test_all_seven_success_results_verify` still assigns an `approval_required=True` task while proposed and expects success — changing it needs his yes); the `pre_tool_call` blocking gate (design-only). The open questions in §5 are unchanged and still await Claude's wiring review.

## 7. Catalog rule 9 — private core imports (Phase 4, 2026-11-18)

Validator before: 15/15 checks pass, 1 warning (10 named private imports + "10 more"). Each import was checked against the host's `PluginContext` surface (`hermes_cli/plugins.py`) for a documented seam. Result: **one import replaced, the rest kept with the missing seam documented** — no working code was broken to chase the warning.

**Replaced — `delegation_cli.py::_pick_effort`:** the host's underscore-private `_prompt_reasoning_effort_selection` (`hermes_cli.main_provider_setup`) is now gone. The picker is built on this CLI's own `_radio` menu with the level list from the public `hermes_constants.VALID_REASONING_EFFORTS`. Semantics verified against the host source and preserved exactly: cancel/Skip keeps the current value, "Inherit parent" leaves the key unset, "Disable reasoning" stores `"none"`, a level stores that level. 286/286 still pass.

**Kept — no documented seam exists (verified in `hermes_cli/plugins.py`):**

| Import | Used for | Missing seam |
|---|---|---|
| `agent.delegation_provider` (base class) | `CEODelegationProvider` must subclass the host's provider ABC to register via `register_delegation_provider` | There is no ctx-provided base class; subclassing the host ABC *is* the integration point. Already guarded with a stand-in fallback for plain-python runs. |
| `hermes_cli.config` (`load_config_readonly`, `atomic_config_write`, `get_config_path`, `is_managed`, `require_readable_config_before_write`, `get_compatible_custom_providers`) | Read/write the profile config's `delegation:` section (`delegation.backend`, `delegation.backends.ceo_pack.seats`) | `ctx.get_config`/`set_config` address only `plugins.entries.<plugin_id>.settings` (the plugin's install settings) — a different location. There is no ctx method for the profile's `delegation:` section. |
| `hermes_cli.providers.resolve_provider_full` | Provider-registry lookup in `_provider_known` | No ctx provider-registry API. |
| `hermes_cli.models.normalize_provider`, `resolve_fast_mode_overrides` | Provider-name normalization, speed-tier offering checks | No ctx model-catalog API. A local normalization table would drift from host semantics. |
| `agent.fast_mode.parse_exact_service_tier` | Speed-tier parsing in `_tier_problem` | No ctx tier API. |
| `agent.codex_service_tiers` (`DAYBREAK_PROGRAM`, `daybreak_offered`), `agent.session_source` (`DAYBREAK_SOURCES`, `session_source_for`, `turn_daybreak_surface`), `gateway.session_context.get_session_env` | Daybreak gating, copied from the host's real provider | No ctx Daybreak API. |
| `hermes_cli.curses_ui` (`curses_radiolist`, `curses_checklist`), `hermes_cli.cli_output.prompt`, `hermes_cli.inventory`, `hermes_cli.auth_model_picker`, `hermes_cli.models_pricing`, `hermes_cli.profiles` | The `hermes ceo delegation config` interactive CLI (menus, provider/model pickers with pricing, profile list) | The CLI runs in the local CLI process via `register_cli_command` — there is no `PluginContext` there at all, so `ctx.ask_choice` / `ctx.profile_name()` cannot apply. `ctx.profile_name()` also returns only the active profile, not the list the picker needs. |

Validator after: 15/15 checks pass; the rule-9 warning remains (fewer named imports — `_prompt_reasoning_effort_selection` is gone) because the kept imports above are still flagged. If Nous ever documents a seam for the profile `delegation:` section, the provider registry, or the model catalog, the store/provider should move to it; until then these imports are load-bearing, not cosmetic.
