# Model choices — free default, top-ranked options

Default: **Nemotron 3 Ultra free** — `nvidia/nemotron-3-ultra-550b-a55b:free` (OpenRouter), `nemotron-3-ultra-free` (Zen). 550B, 1M context, picked in the Nov 4, 2026 check for CEO orchestration: long sessions, handoffs, tool history, delegation.

| Choice | ID / provider | Use for |
|---|---|---|
| free_default | nvidia/nemotron-3-ultra-550b-a55b:free | CEO, default |
| free_worker | nvidia/nemotron-3.5-lightning:free | Narrow worker jobs |
| free_worker_2 | inclusionai/ling-3.2-flash:free | Narrow worker jobs — re-verify live; bare ling-3.2-flash is PAID, only :free is free |
| free_random | openrouter/free | Fallback, random free with tool-calling filter |
| paid_value | anthropic/claude-sonnet-5.5 | Everyday frontier, when free limits bite |
| paid_flagship | anthropic/claude-opus-5.5 | High stakes only, override not default |

## Cost mode constrains the choice (Chat #5, Nov 21, 2026)

The model picks, the cost mode sets the ceiling — an explicit model
choice never overrides the cost boundary. Ask for a model your cost mode
does not cover and the plugin refuses with the permitted list named,
instead of quietly swapping in a cheaper model. An unknown model key is
refused the same way, never silently defaulted.

| Cost mode | Permitted keys |
|---|---|
| `free` | `free_default`, `free_worker`, `free_worker_2`, `free_random` |
| `paid_value` | `paid_value` + all four free keys |
| `paid_flagship` | `paid_flagship`, `paid_value` + all four free keys |

Neither supplied (no model asked for): the cost mode picks its default —
free → `free_default`, paid_value → `paid_value`, paid_flagship →
`paid_flagship`. The rule is enforced in two places: at install time
(`apply_install_config` refuses an incompatible model + cost combination
and keeps the previous value) and at plan time (`_pick_model` returns a
clean error and `plan_tool_calls` returns an error envelope, not a plan
on a substituted model). Lowering `cost_mode` below the current model is
refused the same way — change the model first.

Caveats, from that same check, that users must see:
- Live re-check Nov 5, 2026: Nemotron 3 Ultra free slug confirmed still $0 / 1M / tool-calling and answering test calls Nov 3; GLM-5.3-Flash Free still listed on Zen (zero retention, 200K context in OpenCode, free for a limited time); Lightning slug corrected to `nvidia/nemotron-3.5-lightning:free`; `inclusionai/ling-3.0-flash` is paid, not free — worker 2 is now `inclusionai/ling-3.1-flash:free` (v0.4.0 fix: bare ID is paid, re-verify live).
- Live re-check Nov 20, 2026 (OpenRouter models list): `inclusionai/ling-3.2-flash:free` verified $0 / 1M context / tool-calling — worker 2 moved from 3.1 to 3.2, because `inclusionai/ling-3.1-flash:free` is GONE from the live list (bare 3.1 is paid). The GLM free fallback is DROPPED: `z-ai/glm-5.3-flash` is paid on OpenRouter ($0.15 in / $0.47 out per 1M), paid-only on Zen per the Nov 18 check, and no free GLM variant exists — swapping it would have put a paid model in the free slot. Both Nemotron free slugs re-confirmed $0 / 1M / tool-calling the same day.
- NVIDIA free hosts are listed 'May train' / prompts logged — do not send client books through a free NVIDIA route.
- OpenRouter free: 20 req/min, 50 req/day until $10 of credits, then 1,000/day. A CEO task is many requests.
- Free lineups turn over fast — models free in July were not free by October, and Nous Portal rotates monthly. Verify live before a demo.
- Retention/training policies differ by provider and by free tier. Verify before sending sensitive books to a free model.
- Prices in `models.py` are Nov 4 guidance, not quotes.
