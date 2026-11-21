# CEO Pack Dashboard — API Contract (v0.5.0)

Base: `/api/plugins/ceo-pack/` — mounted by the Hermes dashboard host from
`dashboard/plugin_api.py` (`router`). **Reads are GET; writes are four
POST routes** (below) that call the same `tasks.py` functions the chat
tools call. The host authenticates every `/api/` call, so the dashboard
caller is an authenticated Hermes user (the owner).

## Truth rules (apply to every endpoint)

- Every value is a read of the persistent task registry (`tasks.py`,
  SQLite) or the roster (`orchestration.get_roster_view`, seeded from the
  roster snapshot the agent process writes at registration). Nothing is
  computed from thin air; nothing is invented.
- A task's status is returned exactly as the registry holds it. Assigned
  is not "in progress"; completed is not "verified".
- Timestamps come from the registry (`created_at`, `updated_at`,
  `started_at`, `completed_at`, `approved_at`, history `at`). A missing
  timestamp is `null`; the UI renders "unknown".
- Zero counts are real counts of an empty registry. Responses carry
  `"empty": true` (summary) when the registry holds no tasks, and lists
  are `[]` — never fabricated rows.
- No secrets, no business content: task `result`/`error` payloads and
  free-form `metadata` are stripped from every task object. The API never
  reads customer lists, books, or any business data (there is no opt-in
  tool exposed in v0.5.0).
- Errors are visible, not silent: bad input → 400 with a `detail`
  string; unknown task/run → 404; registry unavailable → 503. No
  traceback is ever returned.
- Live model: the frontend polls every endpoint every 30s; there is no
  push channel (the plugin API is GET-only by design). "Live" therefore
  means "within the last poll". A seat whose `last_activity` is under 2
  minutes old renders a live dot; a task whose status changed, a seat
  whose activity moved, or a feed line that just arrived flashes once
  for 5s after the poll that sees it. The first load never flashes —
  everything is new then.
- `rejected` is a terminal lifecycle status, reachable only from
  `proposed` (owner declines a task from the dashboard). Nothing exits
  `rejected`; `completed_at` is set like the other terminal statuses.

## Write routes (POST) — the dashboard acting for the owner

Each route calls the same `tasks.py` function the matching chat tool
calls, with the same validation the chat handler runs. Responses are the
sanitized task object (same shape as the GET routes — no result/error
payloads, no metadata). Error mapping, everywhere: unknown task → 404;
wrong state / not Type 1 → 409 with a plain `detail` string; bad input →
400; registry locked or unavailable → 503 (retryable). No traceback is
ever returned. A `POST`/`PUT`/`PATCH`/`DELETE` to any other path returns
405 (no such route).

| Route | Backend | Rules |
|---|---|---|
| `POST /tasks` | `tasks.create_task` | Body `{run_id, seat, title, due_date?, description?, task_key?, owner?, model_route?, approval_required?, risk_flags?, parent_task_id?, metadata?}`. Missing/invalid required text → 400; unknown seat → 400; unknown `run_id` → 400 (the dashboard only offers existing runs); bad `due_date` → 400. `task_key` is idempotent per run: a repeat returns the existing task. Seat is canonicalized (`cfo` → `CFO`). |
| `POST /tasks/{id}/approve` | `tasks.approve_task` | Task must exist → 404. Must be Type 1 (`approval_required`) → 409. Must be `proposed` → 409. `approved_by` is `"owner"`; the history note records the dashboard origin. |
| `POST /tasks/{id}/reject` | `tasks.update_task_status(…, "rejected")` | Task must exist → 404. Must be `proposed` → 409. Optional body `{note?}`. History actor `"owner (dashboard)"`. Terminal — nothing exits `rejected`. |
| `POST /tasks/{id}/assign` | `tasks.assign_task` + `move_to_assigned` | Body `{owner, model_route?}`. Owner is required text → 400 (mirrors the chat tool, which takes any owner text; the UI offers seats). Task must exist → 404. Status must be `approved`/`proposed` → 409. History actor `"owner (dashboard)"`. |

Concurrency: the agent process and the web-server process both write the
registry now. `_connect` sets `PRAGMA journal_mode=WAL` + a 10s busy
timeout; a still-contended write surfaces as 503, never a hang or a 500.

## Endpoints

### `GET /` — health
| | |
|---|---|
| Output | `{"plugin": "ceo-pack", "version": "0.5.0", "status": "ok"}` |
| Source | Static. |

### `GET /summary` — headline counts
| Field | Source |
|---|---|
| `generated_at` | Server time (UTC ISO) at read. |
| `empty` | `true` when the registry holds zero tasks. |
| `tasks_total` | `sum(tasks.get_status_counts().values())`. |
| `tasks_by_status` | `tasks.get_status_counts()` — `{status: count}`. |
| `total_runs` | `tasks.get_run_count()`. |
| `pending_approvals` | `len(tasks.get_pending_approvals())`. |
| `running` | `len(tasks.get_running_tasks())`. |
| `stuck` | `len(tasks.get_stuck_tasks())` (blocked/failed, retries exhausted). |

### `GET /roster` — org chart data
| Field | Source |
|---|---|
| `head_seat` | `orchestration.get_roster_view()["head_seat"]`. |
| `seats[]` | One node per seat from `get_roster_view()["seats"]`. |
| `org` | Tree built from the seats: root = head seat, `children` = the other seats (plugin accountability model — see note). `departments: []` (none exist yet). |
| `departments` | `[]` — reserved for future departments. |
| `snapshot_loaded` | `true` when a roster snapshot file existed and applied; `false` on a fresh install (default roster shown). |
| `display_text_is_untrusted` | Always `true` — names/avatars/soul are owner-typed display text. |
| `note` | Explains the accountability model + that departments plug in later. |

Seat node fields: `kind` (`"seat"`), `seat` (canonical), `display_name`,
`display` (`"Alias (Canonical)"` or canonical), `avatar_url` (the
configured avatar **only** when it is an `https://` URL, else `null`),
`initials` (derived from the display name), `soul` (owner-typed text or
`null`), `profile` (bound Hermes profile or `null`), `bound`, `binding`
(`"full"` / `"name-only"` / `"reference"`), `capabilities`,
`provided`, `missing`, `last_activity` (ISO timestamp of the seat's
latest registry activity — newest of the seat's task `updated_at` values
and history events on its tasks — or `null` when the seat has no recorded
activity; the UI shows "unknown"), `children: []`, `members: []`
(reserved for departments / subordinate agents).

### `GET /tasks` — task board list
Query params: `status`, `seat`, `run_id` (text filters), `limit`
(integer 0–200, default 50). Non-text filters or an out-of-range limit
→ 400.

| Field | Source |
|---|---|
| `tasks[]` | `tasks.list_tasks(...)`, newest first, sanitized (see truth rules). |
| `count` | `len(tasks)`. |
| `filters` | Echo of the applied filters. |

Task object fields: `task_id` (= `id`), `run_id`, `task_key`, `seat`,
`title`, `description`, `status`, `priority`, `owner`, `model_route`,
`due_date`, `created_at`, `updated_at`, `started_at`, `completed_at`,
`approved_at`, `approved_by`, `approval_required`, `risk_flags`,
`retry_count`, `max_retries`, `parent_task_id`, `execution` (see below).

`execution` — the validated delegated-execution record for the task's
delegate run (Phase 3), or `null` when nothing was recorded. Source:
`tasks.get_execution` / `tasks.execution_from_metadata`; the raw
`metadata` column stays stripped. Fields are exactly what the Hermes
host reports on a delegate result entry — nothing is estimated or
invented:

| Field | Meaning |
|---|---|
| `route` | Ran-on route: `backend`, `label`, `provider`, `model`, `reasoning_effort`, `service_tier`, `fallback_activated`, `refused`. `refused: true` means the host refused the task. Compare with `model_route`, the *requested* route. |
| `tokens.input` / `tokens.output` | Host-reported token counts; absent when the host did not supply them. |
| `cost_usd` | Host-reported cost; absent when unknown. Never estimated. |
| `cost_status` | `"unknown"` when the host could not compute a cost; otherwise the host's status value. |
| `api_calls` / `duration_seconds` | Host-reported call count and run duration. |
| `exit_reason` / `failure_reason` / `status` | Host-reported outcome strings. |
| `recorded_at` | When the record was written to the registry. |

### `GET /tasks/{task_id}` — task detail
404 when the id is unknown. Returns the sanitized task object plus:
`history[]` (`tasks.get_task_history` — `from_status`, `to_status`,
`actor`, `note`, `at`), `dependencies[]`, `dependents[]`,
`dependencies_met` (`tasks.are_dependencies_met`).

### `GET /runs` — orchestration runs
Query param: `limit` (integer 0–100, default 20). Returns recent runs
with per-run `task_count` and `status_counts` (computed from
`tasks.get_tasks_by_run`).

### `GET /runs/{run_id}` — run detail
404 when the id is unknown. Run row plus its tasks (sanitized) and
`status_counts`.

### `GET /activity` — live activity center
| Field | Source |
|---|---|
| `status_counts` | `tasks.get_status_counts()`. |
| `running[]` | `tasks.get_running_tasks()` (sanitized). |
| `stuck[]` | `tasks.get_stuck_tasks()` (sanitized). |
| `recent_history[]` | `tasks.get_recent_history(50)` — history events with task title + seat. |

### `GET /approvals` — approval center
| Field | Source |
|---|---|
| `pending[]` / `pending_count` | `tasks.get_pending_approvals()` (Type 1 tasks in `proposed`). |
| `history[]` | `tasks.get_approval_history(50)` — history events where a task moved to `approved` (actor + timestamp). |
| `decisions_note` | States plainly that free-form decisions logged in chat with `ceo_decision_log` are **not** stored by the plugin in v0.5.0; approval history here comes from the task registry. |

Pending tasks carry `action_tool` / `action_args_hash` when the task
names the exact action it will execute (hash only, never argument
values). The approve route accepts optional `tool_name` + `arguments`
in the body and returns `token_minted` / `token_id` for the ticket it
minted (Stage 1 gate, warn mode — see `GET /gate-decisions`).

### `GET /gate-decisions` — Type 1 gate audit log (Stage 1, warn/observe only)
Query param: `limit` (integer 0–100, default 50). Returns the newest
`gate_audit` rows first: `at`, `decision` (`would_allow` | `would_block` |
`token_minted` | `token_revoked` | `hook_error`), `tool_name`,
`args_hash` (SHA-256 of the canonical arguments — never the values),
`task_id`, `token_id`, `approved_by`, `actor`, `session_id`, `reason`
(`token_match` | `no_token` | `token_expired` | `token_used` |
`token_revoked` | `modified_arguments` | `registry_unavailable` |
`hook_error` | `uncertain_classification`), `mode` (`"warn"` in Stage 1).
Warn mode is NOT enforcement: `would_block` means the gate would have
stopped the call under Stage 2 rules; nothing was actually blocked and
no ticket was consumed.

## Known limitations

- **Roster snapshot freshness:** the snapshot is written when the plugin
  registers (install/enable/reload). A config change applies to the
  dashboard on the next plugin load, not instantly.
- **Profile avatars:** the dashboard shows a seat's configured avatar
  only when it is an `https://` URL; otherwise initials. Hermes profile
  assets (`profile_dir/assets/avatar.png`) are host-internal and not
  reachable from the plugin API — resolving them is a host question, not
  something to fake.
- **Decision log:** free-form `ceo_decision_log` entries are not
  persisted (stateless tool). The approval center shows pending approvals
  and approval transitions from the registry only.
- **Profiles share one registry:** the task DB path is not
  profile-aware (pre-existing v0.5.0 behavior) — the dashboard shows the
  same registry the tools write, across profiles.
