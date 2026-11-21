# Model Selection

Use `ceo_choose_model` before picking a brain for a job.

- CEO seat: largest orchestration model you can afford. Default is free: Nemotron 3 Ultra free (`nvidia/nemotron-3-ultra-550b-a55b:free` on OpenRouter, `nemotron-3-ultra-free` on Zen), 550B, 1M context.
- Fallback free: Nemotron 3.5 Lightning free, then `openrouter/free` as a random free with tool-calling filter. (The old GLM Flash free fallback is gone — no free GLM exists anymore, Nov 20, 2026.)
- Workers: fast, narrow frees — Nemotron 3.5 Lightning, Ling 3.2 Flash `:free` (bare ID is paid, never use it as free), or `openrouter/free` as a random free with tool-calling filter. Never put a small fast worker in the CEO seat.
- Paid override, high stakes only: Claude Opus 5.5. Paid value: Claude Sonnet 5.5 or GPT-6.1 Sol, same price band in the Nov 4 check.
- Cost mode constrains the choice — the model picks, the cost mode sets the ceiling. Free buys only the four free models; paid_value buys the value model plus the free ones; paid_flagship buys everything. Ask for a model your cost mode does not cover and the plugin refuses with the permitted list named — it never quietly swaps in a cheaper model, and an unknown model key is refused the same way. With no model asked for, the cost mode picks its default: free → Nemotron 3 Ultra free, paid_value → Claude Sonnet 5.5, paid_flagship → Claude Opus 5.5.
- Free limits to plan around: OpenRouter free is 20 requests/min, plus 50 requests/day until $10 of credits, then 1,000/day. A CEO task uses many requests — one per tool round-trip.
- Free lineups turn over fast. Verify the live provider list before a demo, and never hard-depend on one free slug forever.
