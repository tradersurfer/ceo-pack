# CEO Pack Dashboard — API Contract (v0.5.0)

Base: `/api/plugins/ceo-pack/` — mounted by the Hermes dashboard host from
`dashboard/plugin_api.py` (`router`). **All routes are GET, read-only.**
There are no write routes by design: creating, approving, or assigning
work goes through the chat tools (`ceo_task_create`, `ceo_task_approve`,
`ceo_task_assign`), which run the lifecycle and permission checks. A
`POST`/`PUT`/`PATCH`/`DELETE` to any route returns 405 (no such route).

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
`provided`, `missing`, `children: []`, `members: []` (reserved for
departments / subordinate agents).

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
