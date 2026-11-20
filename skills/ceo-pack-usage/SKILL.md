# CEO Pack Usage

CEO Pack is your AI C-suite inside Hermes. You talk like a business owner, it turns your words into a plan, gives each job to the right seat, and checks the work before calling it done.

## How to use it

- Say `/ceo` once to turn CEO mode on for the whole chat. It stays on until you say `/ceo off`. You do not need to type `/ceo` on every message.
- Or type `/ceo` with your question in one go, like `/ceo how do I set up my business with you?` The pack reads your question, works out the goal, and runs the flow.
- Or open the CEO Pack tab in the dashboard — no command needed, just click and work.
- When CEO mode is on, the pack reads your words first, works out what you are trying to achieve, and says the goal back in one plain sentence before it runs the flow. If your words have no clear goal yet, it asks ONE short question, then runs it — it never stalls with a bare acknowledgement.

## The flow, in plain words

1. **Brief** — say the goal in one plain sentence. (`ceo_brief`)
2. **Route** — the pack picks the seat for each job: money (CFO), legal (CLO), marketing (CMO), tech (CTO), running the business day to day (COO / Hermes), or the chief of staff. (`ceo_route`)
3. **Assign** — one owner and one due date per job. (`ceo_assign`)
4. **Do the work** — the tool runs, with the risk flags on top, never buried inside. (`ceo_tool_call`)
5. **Verify** — the pack checks the work is really done before it counts. (`ceo_verify`)
6. **Log it** — the decision goes in the log. (`ceo_decision_log`)

## The seats are roles, not people

You do not need seven agents. A seat is a job title. If you made a real Hermes profile for a seat, the work goes there. If you did not, the same main agent does the work wearing that seat's hat — it thinks like a CFO for a money job and like a CLO for a legal job. The roster shows which seats are tied to a real profile and which are falling back to the main agent.

## Rules the pack follows

- Anything big, priced, or hard to undo stops and asks you first. You stay the boss.
- Risk flags go at the top level of the tool call, never buried inside the arguments.
- One owner and one due date per assignment. The pack does not track duplicates — you track (seat, due date) yourself.
- Tasks need a run first — start a run from chat, then create tasks in it. A task has no due date of its own; the due date lives on the assignment.
- Never invent a price, a fact, or client work. If a number is unknown, say so and ask the owner.
- Every answer is in plain words a business owner can follow — no jargon, short sentences, what happens next.

## Known gaps (v0.5.0)

- No `ceo_task_complete` tool yet — finishing a task goes through the task's status, not a single tool call.
- `ceo_assign` does not track duplicates.
- `ceo_task_create` has no due date field — use `ceo_assign` for dated work.
- Fixed in v0.5.0: signing a document now flags outside-authority risk (the NDA case), and "fire the shift lead" now routes to the Chief of Staff, not the CRO.
