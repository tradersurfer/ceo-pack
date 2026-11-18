"""Tool handlers — pure, stdlib-only, no network, no file writes. (v0.4.0)

Handlers always return a JSON string, even on error, and never raise.
Every handler refuses credential-like input (paths only, values never echoed)
and returns an explicit status field the verify hook can read.
"""

import json
import re

SEATS = {
    "ceo": "CEO",
    "cfo": "CFO",
    "coo": "COO (Hermes)",
    "cto": "CTO",
    "cmo": "CMO",
    "clo": "CLO",
    "chief of staff": "Chief of Staff",
    "cro": "CRO",
}

DOMAIN_TO_SEAT = {
    "finance": "CFO", "accounting": "CFO", "tax": "CFO", "bookkeeping": "CFO",
    "reporting": "CFO", "forecasting": "CFO",
    "operations": "COO (Hermes)", "ops": "COO (Hermes)",
    "documents": "COO (Hermes)", "monitoring": "COO (Hermes)",
    "inbox": "COO (Hermes)", "scheduling": "COO (Hermes)",
    "technology": "CTO", "engineering": "CTO",
    "coding": "CTO", "debugging": "CTO",
    "marketing": "CMO", "brand": "CMO", "content": "CMO", "social": "CMO",
    "legal": "CLO", "compliance": "CLO", "contracts": "CLO",
    "people": "Chief of Staff", "hiring": "Chief of Staff",
    "writing": "Chief of Staff", "email": "Chief of Staff", "meetings": "Chief of Staff",
    "revenue": "CRO", "sales": "CRO",
    "support": "CRO", "deals": "CRO", "proposals": "CRO",
    "strategy": "CEO", "research": "CEO",
    "fundraising": "CEO", "investors": "CEO",
}

# Content routing: whole-word matches only ("ops" must not fire inside "laptops").
# Expanded v0.4.0 (research round): top-20 business tasks AI agent users actually
# ask for, with multi-term scoring so "close the sales deal" beats a bare
# "contract" mention. Ties go to the safer seat in _ROUTE_PRIORITY order.
_ROUTE_TERMS = {
    "CLO": r"legal|law|lawsuit|compliance|contracts?|nda|licen[sc]e[sd]?|trademark|liability|terms|regulat\w+|privacy|agreements?|clauses?|signatures?|copyright|patents?",
    "CFO": r"finance|financial|accounting|tax(?:es|ation)?|bookkeeping|books?|budgets?|invoices?|payroll|cash ?flow|expenses?|audit|reconcil\w+|forecast\w*|quarters?|runway|burn rate|dashboards?|metrics|spreadsheets?|kpis?|receipts?|bank|statements?|board|q[1-4]",
    "CTO": r"technology|tech|engineering|software|security|infrastructure|database|code|api|deploy(?:ment)?|bugs?|websites?|checkout|debugg\w*|refactor\w*|errors?|crash\w*|servers?|hosting|domains?",
    "CRO": r"revenue|sales|pipeline|leads?|prospects?|prices?|pricing|discounts?|quotes?|outreach|prospect\w*|follow[- ]ups?|proposals?|deals?|close|crm|negotiat\w*|support|tickets?|complaints?|churn",
    "CMO": r"marketing|brand(?:ing)?|content|campaigns?|social media|seo|launch|newsletter|subscribers?|announce\w*|promotions?|posts?|captions?|blogs?|ads|advertising|funnel|conversions?|landing page",
    "Chief of Staff": r"people|hiring|hire|recruit\w*|onboarding|culture|hr|team|summar\w*|briefings?|draft|write|rewrite|proofread|email|meeting notes|action items|interviews?|candidates?|resumes?|screening|job offer",
    "COO (Hermes)": r"operations?|ops|process(?:es)?|logistics|vendors?|schedule|cadence|workflows?|inbox|calendar|extract\w*|documents?|paperwork|monitor\w*|alerts?|sops?|projects?|timeline|deadline|checklists?|suppliers?",
    "CEO": r"strategy|research|vision|priorities|brainstorm\w*|roadmap|goals?|fundrais\w*|investors?|pitch|term sheet|cap table|funding|venture|mission",
}
_ROUTE_PRIORITY = ["CLO", "CFO", "CTO", "CRO", "CMO", "Chief of Staff", "COO (Hermes)", "CEO"]
_ROUTE_RE = {seat: re.compile(r"\b(?:%s)\b" % terms, re.I)
             for seat, terms in _ROUTE_TERMS.items()}


def _ok(payload, ignored=None):
    payload = dict(payload)
    payload.setdefault("status", "success")
    if ignored:
        payload["ignored_fields"] = ignored
        payload["warning"] = ("Ignored unknown field(s): %s. They had no effect, including any risk or "
                              "approval flags. Use ceo_tool_call to pass risk flags." % ", ".join(ignored))
    return json.dumps(payload)


def _ignored(tool_name, args):
    """Names of supplied fields the tool's schema does not declare (they are otherwise dropped silently)."""
    try:
        try:
            from . import schemas
        except ImportError:
            import schemas
        for value in vars(schemas).values():
            if isinstance(value, dict) and value.get("name") == tool_name:
                props = (value.get("parameters") or value.get("input_schema") or {}).get("properties")
                if isinstance(props, dict):
                    return sorted(k for k in args if k not in props)
    except Exception:
        pass
    return []


def _with_data_note(payload, args):
    note = _orch().data_access_note(args)
    if note:
        payload["data_access_note"] = note
    return payload


def _err(message):
    return json.dumps({"status": "error", "error": message})


def _orch():
    try:
        from . import orchestration
    except ImportError:
        import orchestration
    return orchestration


def _prep(args, scan=True):
    """Coerce args to a dict and refuse credential-like content. Returns (args, error_json).

    scan=False is for ceo_tool_call, where make_tool_call does the scan and
    returns the envelope shape (valid: false) instead of an error dict.
    """
    if args is None:
        args = {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None, _err("Arguments string is not valid JSON.")
    if not isinstance(args, dict):
        return None, _err("Arguments must be a JSON object.")
    leaked = _orch().find_secret_paths(args) if scan else []
    if leaked:
        return None, _err(f"Refused: credential-like content at {leaked}. "
                          "Pass secrets through Hermes, never in tool arguments. Values are not echoed.")
    return args, None


def _text(args, key, required=True):
    """Return (stripped string, error_json). Non-string values are errors, not crashes."""
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return ("", _err(f"Need {key}")) if required else ("", None)
    if not isinstance(value, str):
        return "", _err(f"{key} must be text, got {type(value).__name__}.")
    return value.strip(), None


def ceo_brief(args, **kwargs):
    args, bad = _prep(args)
    if bad:
        return bad
    goal, e = _text(args, "goal")
    if e:
        raw = args.get("goal")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return _err("No goal provided")
        return e
    business, e = _text(args, "business_name", required=False)
    if e:
        return e
    inferred = _orch().infer_risk({"goal": goal})
    install = _orch().INSTALL
    return _ok(
        _with_data_note({
            "goal": goal,
            "business_name": business or install["business_name"] or None,
            "owner": install["owner_name"] or None,
            "decision_needed": "Owner to confirm the goal and the budget before work is assigned.",
            "workstreams": [
                {"seat": "CEO", "job": "Hold priorities and the decision log."},
                {"seat": "CFO", "job": "Numbers, costs, and financial impact."},
                {"seat": "COO (Hermes)", "job": "Execution, coordination, and operating cadence."},
                {"seat": "CTO", "job": "Technology, data, and security."},
                {"seat": "CMO", "job": "Market, brand, and customer message."},
                {"seat": "CLO", "job": "Legal, contracts, and compliance."},
            ],
            "inferred_risk": inferred,
            "approval_required": bool(inferred),   # planning is Type 2; only risky wording gates
            "next_step": "Assign one owner and one due date per workstream with ceo_assign.",
        }, args), ignored=_ignored("ceo_brief", args)
    )


def ceo_assign(args, **kwargs):
    """Assign with the same validation as the orchestration envelope.

    Required fields, a real ISO YYYY-MM-DD due date, and a known seat.
    Hermes and COO (Hermes) both map to COO, displayed as COO (Hermes)
    to match ceo_route. A past due date warns, it does not fail.
    approval_required is True only when the task wording reads as Type 1;
    assigning work is Type 2, and the envelope agrees.
    """
    args, bad = _prep(args)
    if bad:
        return bad
    orch = _orch()
    task, e = _text(args, "task")
    seat_raw, e2 = _text(args, "seat")
    if e or e2:
        for err in (e, e2):
            if err and "must be text" in err:
                return err
        return _err("Need task, seat, and due_date")
    due = args.get("due_date")
    ok, note = orch._check_due_date(due)
    if not ok:
        return _err(note)
    seat = orch.normalize_seat(seat_raw)
    if seat is None:
        return _err("Unknown seat. Valid seats: CEO, CFO, COO, CTO, CMO, CLO, Chief of Staff, CRO (Hermes = COO)")
    inferred = orch.infer_risk({"task": task})
    payload = {
        "task": task,
        "owner": "COO (Hermes)" if seat == "COO" else seat,
        "due_date": due,
        "state": "assigned",
        "inferred_risk": inferred,
        "approval_required": bool(inferred),
        "owner_is_assignee": seat == "CEO",
        "note": "Assignment only. Execution and any external action still need owner approval where flagged.",
    }
    if note:
        payload["warning"] = note
    return _ok(_with_data_note(payload, args), ignored=_ignored("ceo_assign", args))


def ceo_decision_log(args, **kwargs):
    """Record a decision. Wording that reads as Type 1 forces approval; a caller cannot lower it."""
    args, bad = _prep(args)
    if bad:
        return bad
    decision, e = _text(args, "decision")
    chosen, e2 = _text(args, "chosen")
    if e or e2:
        for err in (e, e2):
            if err and "must be text" in err:
                return err
        return _err("Need decision and chosen")
    reason, e3 = _text(args, "reason", required=False)
    if e3:
        return e3
    options = args.get("options") or []
    if not isinstance(options, list) or not all(isinstance(x, str) for x in options):
        return _err("options must be a list of text options.")
    needs = args.get("needs_approval")
    if needs is None:
        needs = True
    needs = bool(needs)
    inferred = _orch().infer_risk({"decision": decision, "chosen": chosen, "reason": reason, "options": options})
    forced = bool(inferred) and not needs
    if inferred:
        needs = True
    payload = {
        "decision": decision,
        "options": options,
        "chosen": chosen,
        "reason": reason,
        "needs_approval": needs,
        "inferred_risk": inferred,
        "state": "awaiting_owner_approval" if needs else "recorded",
        "status": "needs_approval" if needs else "success",
    }
    if forced:
        payload["gate_note"] = "needs_approval was set to false but the wording reads as Type 1, so approval is required."
    return _ok(_with_data_note(payload, args), ignored=_ignored("ceo_decision_log", args))


def ceo_route(args, **kwargs):
    """Route to the seat whose domain terms match best. Whole words only; ties go to the safer seat."""
    args, bad = _prep(args)
    if bad:
        return bad
    task, e = _text(args, "task")
    if e:
        raw = args.get("task")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return _err("No task provided")
        return e
    domain_raw = args.get("domain")
    if domain_raw is not None and not isinstance(domain_raw, str):
        return _err(f"domain must be text, got {type(domain_raw).__name__}.")
    domain = (domain_raw or "").strip().lower()
    inferred = _orch().infer_risk({"task": task})
    seat = DOMAIN_TO_SEAT.get(domain)
    matched, consult, confidence = [], [], "high"
    if seat:
        why = f"Explicit domain '{domain}'. CEO retains accountability for the integrated result."
    else:
        scores = {}
        for name, rx in _ROUTE_RE.items():
            hits = sorted({m.group(0).lower() for m in rx.finditer(task)})
            if hits:
                scores[name] = hits
        if scores:
            top = max(len(v) for v in scores.values())
            leaders = [n for n in _ROUTE_PRIORITY if n in scores and len(scores[n]) == top]
            seat = leaders[0]
            matched = scores[seat]
            consult = [n for n in _ROUTE_PRIORITY if n in scores and n != seat]
            # one generic word is a weak signal: "high" needs a single leader AND at least two matched terms
            confidence = "high" if (len(leaders) == 1 and len(matched) >= 2) else "medium"
            why = "Best whole-word match in the task. CEO retains accountability for the integrated result."
        else:
            seat, confidence = "CEO", "low"
            why = "No domain terms matched, so the CEO holds it. Pass domain to route explicitly."
    return _ok(
        _with_data_note({
            "task": task,
            "domain": domain or None,
            "routed_to": seat,
            "matched_terms": matched,
            "also_consult": consult,
            "confidence": confidence,
            "why": why,
            "inferred_risk": inferred,
            "approval_required": bool(inferred),
        }, args), ignored=_ignored("ceo_route", args)
    )


_CLOSEOUT_QUARTERS = ("Q1", "Q2", "Q3", "Q4", "monthly")

_CLOSEOUT_ITEMS = {
    "Q1": [
        "Reconcile books and close Q1 in your accounting system.",
        "Review prior-year closeout items still open, if any.",
        "Set quarterly tax estimates and confirm deadlines.",
        "Review licences and renewals due this quarter.",
        "Prepare quarterly reporting: what shipped, what earned, what is stuck, what needs a decision.",
        "Confirm no external filing, payment, or publication goes out without owner approval.",
    ],
    "Q2": [
        "Reconcile books and close Q2 in your accounting system.",
        "Mid-year review: budget vs actual, pipeline vs target.",
        "Review estimated taxes and payroll items for the half year.",
        "Prepare quarterly reporting: what shipped, what earned, what is stuck, what needs a decision.",
        "Confirm no external filing, payment, or publication goes out without owner approval.",
    ],
    "Q3": [
        "Reconcile books and close Q3 in your accounting system.",
        "Start year-end planning: Q4 closeout prep and tax items.",
        "Review estimated taxes and any entity changes needed before year-end.",
        "Prepare quarterly reporting: what shipped, what earned, what is stuck, what needs a decision.",
        "Confirm no external filing, payment, or publication goes out without owner approval.",
    ],
    "Q4": [
        "Reconcile books and close the year in your accounting system.",
        "Review individual, income, corporate, sales & use, payroll, and excise tax items that apply to you.",
        "Prepare year-end annual filings and confirm deadlines and jurisdictions.",
        "Review entity structure; list restructuring or cleanup items, if any.",
        "Prepare annual reporting: what shipped, what earned, what is stuck, what needs a decision.",
        "Confirm no external filing, payment, or publication goes out without owner approval.",
    ],
    "monthly": [
        "Reconcile books for the month in your accounting system.",
        "Review invoices, expenses, and cash flow for the month.",
        "Confirm payroll ran and payroll taxes are current.",
        "List deadlines coming up next month.",
        "Prepare monthly reporting: what shipped, what earned, what is stuck, what needs a decision.",
        "Confirm no external filing, payment, or publication goes out without owner approval.",
    ],
}


def _closeout_payload(quarter, year, entity):
    label = "Year-end closeout" if quarter == "Q4" else ("Monthly closeout" if quarter == "monthly" else f"{quarter} closeout")
    return {
        "quarter": quarter,
        "year": year,
        "entity_type": entity or None,
        "label": label,
        "disclaimer": "Checklist only, not tax or legal advice. Confirm with a qualified professional.",
        "checklist": _CLOSEOUT_ITEMS[quarter],
        "owners": {
            "CFO": "Books, numbers, and tax preparation.",
            "CLO": "Entity, filings, and compliance.",
            "COO (Hermes)": "Coordination and operating cadence.",
            "CRO": "Revenue pipeline for potential clients, if applicable.",
        },
    }


def _valid_year(year):
    if isinstance(year, str) and year.strip().isdigit():
        year = int(year.strip())
    if isinstance(year, bool) or not isinstance(year, int) or not (2000 <= year <= 2100):
        return None
    return year


def q4_closeout_checklist(args, **kwargs):
    """Year-end closeout. Kept for backwards compatibility; delegates to the generalized closeout."""
    args, bad = _prep(args)
    if bad:
        return bad
    year = _valid_year(args.get("year"))
    if year is None:
        return _err("Need year as a four-digit number between 2000 and 2100.")
    entity, e = _text(args, "entity_type", required=False)
    if e:
        return e
    return _ok(_closeout_payload("Q4", year, entity))


def ceo_closeout_checklist(args, **kwargs):
    """Closeout checklist for any quarter (Q1-Q4) or monthly, for bookkeeping and accounting."""
    args, bad = _prep(args)
    if bad:
        return bad
    quarter = args.get("quarter", "Q4")
    if not isinstance(quarter, str):
        return _err("quarter must be text: Q1, Q2, Q3, Q4, or monthly.")
    _qmap = {"q1": "Q1", "q2": "Q2", "q3": "Q3", "q4": "Q4", "monthly": "monthly"}
    quarter = _qmap.get(quarter.strip().lower())
    if quarter is None:
        return _err("Unknown quarter. Use Q1, Q2, Q3, Q4, or monthly.")
    year = _valid_year(args.get("year"))
    if year is None:
        return _err("Need year as a four-digit number between 2000 and 2100.")
    entity, e = _text(args, "entity_type", required=False)
    if e:
        return e
    return _ok(_closeout_payload(quarter, year, entity))


def ceo_daily_check(args, **kwargs):
    """Daily check or end-of-day check: what shipped, earned, stuck, decided."""
    args, bad = _prep(args)
    if bad:
        return bad
    period = args.get("period", "daily")
    if not isinstance(period, str):
        return _err("period must be text: daily or eod.")
    period = period.strip().lower().replace("_", "-").replace("end-of-day", "eod")
    if period not in ("daily", "eod"):
        return _err(f"Unknown period {period!r}. Use daily or eod.")
    day, e = _text(args, "date", required=False)
    if e:
        return e
    if day:
        import datetime as _dt
        try:
            _dt.date.fromisoformat(day)
        except ValueError:
            return _err("date must be ISO format YYYY-MM-DD.")
    items = (["What shipped today.", "What earned today.", "What is stuck.",
              "What needs a decision.", "Tomorrow's top 3."]
             if period == "daily" else
             ["What shipped today.", "What earned today.", "What is still stuck.",
              "Blockers cleared or carried over.", "Handoff for tomorrow."])
    return _ok({
        "period": period,
        "date": day or None,
        "checklist": items,
        "inferred_risk": _orch().infer_risk({"task": "daily check"}),
        "approval_required": False,
        "note": "A check is a report, not an action. Anything external, priced, or shipped still needs owner approval.",
    })


def ceo_performance_check(args, **kwargs):
    """Employee performance check: goals, wins, gaps, next steps. People = Type 1, owner approval required.

    Template only: the plugin holds no employee data. The owner supplies the
    subject and any metrics; the check structures the review.
    """
    args, bad = _prep(args)
    if bad:
        return bad
    subject, e = _text(args, "subject")
    if e:
        raw = args.get("subject")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return _err("Need subject: the person or seat being reviewed.")
        return e
    period, e2 = _text(args, "period", required=False)
    if e2:
        return e2
    metrics = args.get("metrics") or []
    if not isinstance(metrics, list) or not all(isinstance(m, str) for m in metrics):
        return _err("metrics must be a list of text items.")
    return _ok({
        "subject": subject,
        "period": period or "quarter",
        "metrics": metrics,
        "review": [
            "Goals for the period: what was agreed, and what actually happened.",
            "Wins: what went well, with examples.",
            "Gaps: where results or behaviour fell short, with examples.",
            "Next steps: one owner, one due date per improvement.",
            "Decision: keep, coach, change scope, or escalate to the owner.",
        ],
        "inferred_risk": ["people"],
        "approval_required": True,
        "gate_note": "Performance decisions are people decisions (Type 1). The owner approves; this check only structures the review.",
    })


def ceo_status_check(args, **kwargs):
    """Status of a briefing, assignment, task, log, or plan.

    The plugin keeps no state, so status comes from the item supplied:
    pass the item's result and, when verifying, its tool_name. Without an
    item, it returns what is needed to check one. It never invents a status.
    """
    args, bad = _prep(args)
    if bad:
        return bad
    item_type = args.get("item_type", "")
    if not isinstance(item_type, str) or not item_type.strip():
        return _err("Need item_type: brief, assignment, task, log, plan, or run.")
    item_type = item_type.strip().lower()
    if item_type not in ("brief", "assignment", "task", "log", "plan", "run", "briefing"):
        return _err(f"Unknown item_type {item_type!r}. Use brief, assignment, task, log, plan, or run.")
    reference, e = _text(args, "reference", required=False)
    if e:
        return e
    result = args.get("result")
    tool_name = args.get("tool_name")
    if result is None:
        return _ok({
            "item_type": item_type,
            "reference": reference or None,
            "status": "unknown",
            "note": "No item supplied, so there is nothing to check. Pass the item's result (and tool_name to verify it) and I will report its real status.",
        })
    if tool_name is not None and not isinstance(tool_name, str):
        return _err("tool_name must be text.")
    verdict = None
    if tool_name:
        verdict = _orch().verify_result(tool_name, result)
        status = verdict["status"]
    elif isinstance(result, dict):
        status = result.get("status") or result.get("state") or "unknown"
    else:
        status = "unknown"
    return _ok({
        "item_type": item_type,
        "reference": reference or None,
        "status": status,
        "verified": verdict["verified"] if verdict else None,
        "missing_fields": verdict["missing_fields"] if verdict else [],
        "next": verdict["next"] if verdict else None,
    })


def ceo_roster(args, **kwargs):
    """C-Suite roster view: seat, display name, bound profile, capabilities, gaps.

    The data for the install flow and the pinned CEO Dashboard.
    """
    args, bad = _prep(args)
    if bad:
        return bad
    return _ok(_orch().get_roster_view())


def ceo_orchestrate(args, **kwargs):
    args, bad = _prep(args)
    if bad:
        return bad
    goal, e = _text(args, "goal")
    if e:
        raw = args.get("goal")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return _err("No goal provided")
        return e
    model_key = args.get("model_key", _orch().INSTALL["model_choice"])
    if not isinstance(model_key, str):
        return _err("model_key must be text.")
    try:
        plan = _orch().plan_tool_calls(goal, model_key)
    except Exception as exc:  # pragma: no cover - fallback for direct import
        return _err(f"Orchestration unavailable: {exc}")
    return _ok(_with_data_note(plan, args), ignored=_ignored("ceo_orchestrate", args))


def ceo_choose_model(args, **kwargs):
    args, bad = _prep(args)
    if bad:
        return bad
    task_type = args.get("task_type", "ceo")
    budget = args.get("budget", _orch().INSTALL["cost_mode"])
    provider = args.get("provider", _orch().INSTALL["model_provider"])
    if not all(isinstance(v, str) for v in (task_type, budget, provider)):
        return _err("task_type, budget, and provider must be text.")
    try:
        try:
            from .models import choose_model
        except ImportError:
            from models import choose_model

        result = choose_model(task_type=task_type, budget=budget)
    except Exception as exc:
        return _err(f"Model choice unavailable: {exc}")
    result["provider_requested"] = provider
    return _ok(result)


def ceo_tool_call(args, **kwargs):
    args, bad = _prep(args, scan=False)
    if bad:
        return bad
    tool_name = args.get("tool_name", "")
    if not isinstance(tool_name, str):
        return _err("Need tool_name and arguments")
    tool_name = tool_name.strip()
    arguments = args.get("arguments")
    seat = args.get("seat", "CEO")
    risk = args.get("risk")
    if not tool_name or arguments is None:
        return _err("Need tool_name and arguments")
    try:
        # risk is passed through as-is: a malformed risk object is an error, never silently dropped.
        return json.dumps(_orch().make_tool_call(tool_name, arguments, seat, risk=risk))
    except Exception as exc:
        return _err(f"Tool-call validation unavailable: {exc}")


def ceo_verify(args, **kwargs):
    """Verify a tool result before anyone reports the step as done."""
    args, bad = _prep(args)
    if bad:
        return bad
    tool_name = args.get("tool_name", "")
    if not isinstance(tool_name, str):
        return _err("Need tool_name and result")
    tool_name = tool_name.strip()
    result = args.get("result")
    if not tool_name or result is None:
        return _err("Need tool_name and result")
    try:
        verdict = _orch().verify_result(tool_name, result, expect=args.get("expect"),
                                        retries_used=args.get("retries_used", 0))
        # The verified tool's status stays in "status"; this call itself is metadata, not a business action.
        return json.dumps(verdict)
    except Exception as exc:
        return _err(f"Verification unavailable: {exc}")
def ceo_task_create(args, **kwargs):
    """Create a persistent task with full lifecycle tracking."""
    args, bad = _prep(args)
    if bad:
        return bad
    try:
        from . import tasks
    except ImportError:
        import tasks
    
    run_id, e = _text(args, "run_id")
    if e:
        return _err("Need run_id")
    seat, e = _text(args, "seat")
    if e:
        return _err("Need seat")
    title, e = _text(args, "title")
    if e:
        return _err("Need title")
    
    # Validate seat
    orch = _orch()
    canonical_seat = orch.normalize_seat(seat)
    if canonical_seat is None:
        return _err(f"Unknown seat. Valid: {', '.join(orch.SEATS)}")
    
    due_date = args.get("due_date")
    if due_date:
        ok, note = orch._check_due_date(due_date)
        if not ok:
            return _err(note)
    
    task_key = args.get("task_key")
    owner = args.get("owner")
    model_route = args.get("model_route")
    approval_required = bool(args.get("approval_required", False))
    risk_flags = args.get("risk_flags") or []
    parent_task_id = args.get("parent_task_id")
    metadata = args.get("metadata") or {}
    description = args.get("description", "")
    
    task_id = tasks.create_task(
        run_id=run_id,
        seat=canonical_seat,
        title=title,
        description=description,
        task_key=task_key,
        due_date=due_date,
        owner=owner,
        model_route=model_route,
        approval_required=approval_required,
        risk_flags=risk_flags,
        parent_task_id=parent_task_id,
        metadata=metadata,
    )
    
    return _ok({
        "task_id": task_id,
        "run_id": run_id,
        "seat": canonical_seat,
        "title": title,
        "task_status": "proposed",
        "approval_required": approval_required,
        "note": "Task created with status 'proposed'. Type 1 tasks require owner approval via ceo_task_approve."
    }, ignored=_ignored("ceo_task_create", args))


def ceo_task_status(args, **kwargs):
    """Get task status, history, and result."""
    args, bad = _prep(args)
    if bad:
        return bad
    try:
        from . import tasks
    except ImportError:
        import tasks
    
    task_id, e = _text(args, "task_id")
    if e:
        return _err("Need task_id")
    
    task = tasks.get_task(task_id)
    if not task:
        return _err(f"Task {task_id} not found")
    
    history = tasks.get_task_history(task_id)
    dependencies = tasks.get_task_dependencies(task_id)
    dependents = tasks.get_dependent_tasks(task_id)
    
    payload = dict(task)
    # The envelope's "status" is the call outcome ("success"); the task's
    # lifecycle state rides under "task_status" so _status_of/verify read
    # the envelope, not the domain value.
    payload["task_status"] = payload.pop("status")
    payload["task_id"] = task["id"]
    payload["history"] = history
    payload["dependencies"] = dependencies
    payload["dependents"] = dependents
    payload["dependencies_met"] = tasks.are_dependencies_met(task_id)
    
    return _ok(payload, ignored=_ignored("ceo_task_status", args))


def ceo_task_list(args, **kwargs):
    """List tasks by seat, status, run, or filter."""
    args, bad = _prep(args)
    if bad:
        return bad
    try:
        from . import tasks
    except ImportError:
        import tasks
    
    # Validate filters with the same _text checks the other handlers use:
    # non-text run_id/seat/status must be an error envelope, never a
    # sqlite binding error.
    run_id, e = _text(args, "run_id", required=False)
    if e:
        return e
    seat, e = _text(args, "seat", required=False)
    if e:
        return e
    status, e = _text(args, "status", required=False)
    if e:
        return e
    limit = args.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        return _err("limit must be a non-negative integer.")
    
    if run_id:
        task_list = tasks.get_tasks_by_run(run_id)
    elif seat:
        task_list = tasks.get_tasks_by_seat(seat, status)
    else:
        # No filter - get all would be too much, require a filter
        return _err("Provide at least one filter: run_id, seat, or status")
    
    # Apply status filter if run_id provided but seat not
    if run_id and not seat and status:
        task_list = [t for t in task_list if t.get("status") == status]
    
    # Limit results
    task_list = task_list[:limit]
    
    # Simplify for list view
    summaries = []
    for t in task_list:
        summaries.append({
            "task_id": t.get("id"),
            "run_id": t.get("run_id"),
            "seat": t.get("seat"),
            "title": t.get("title"),
            "status": t.get("status"),
            "owner": t.get("owner"),
            "due_date": t.get("due_date"),
            "approval_required": t.get("approval_required"),
            "retry_count": t.get("retry_count"),
        })
    
    return _ok({
        "tasks": summaries,
        "count": len(summaries),
        "filters": {"run_id": run_id, "seat": seat, "status": status, "limit": limit}
    }, ignored=_ignored("ceo_task_list", args))


def ceo_task_approve(args, **kwargs):
    """Approve a Type 1 task (owner only)."""
    args, bad = _prep(args)
    if bad:
        return bad
    try:
        from . import tasks
    except ImportError:
        import tasks
    
    task_id, e = _text(args, "task_id")
    if e:
        return _err("Need task_id")
    approved_by, e = _text(args, "approved_by")
    if e:
        return _err("Need approved_by")
    
    task = tasks.get_task(task_id)
    if not task:
        return _err(f"Task {task_id} not found")
    
    if not task.get("approval_required"):
        return _err("Task does not require approval (not a Type 1 task)")
    
    if task.get("status") != "proposed":
        return _err(f"Task is in '{task.get('status')}' state, not 'proposed'. Can only approve proposed tasks.")
    
    ok = tasks.approve_task(task_id, approved_by)
    if not ok:
        return _err("Failed to approve task")
    
    return _ok({
        "task_id": task_id,
        "task_status": "approved",
        "approved_by": approved_by,
        "note": "Task approved. Assign owner/profile via ceo_task_assign, then it can be executed."
    }, ignored=_ignored("ceo_task_approve", args))


def ceo_task_assign(args, **kwargs):
    """Assign a task to an owner/profile and optionally a model route."""
    args, bad = _prep(args)
    if bad:
        return bad
    try:
        from . import tasks
    except ImportError:
        import tasks
    
    task_id, e = _text(args, "task_id")
    if e:
        return _err("Need task_id")
    owner, e = _text(args, "owner")
    if e:
        return _err("Need owner")
    model_route = args.get("model_route")
    
    task = tasks.get_task(task_id)
    if not task:
        return _err(f"Task {task_id} not found")
    
    if task.get("status") not in ("approved", "proposed"):
        return _err(f"Task is in '{task.get('status')}' state. Can only assign approved/proposed tasks.")
    
    ok = tasks.assign_task(task_id, owner, model_route)
    if not ok:
        return _err("Failed to assign task")

    # Assign binds an owner and activates the task: proposed/approved -> assigned.
    # (The raw update_task_status() table refuses proposed -> assigned, so the
    # assign operation has its own transition in tasks.move_to_assigned.)
    tasks.move_to_assigned(task_id, actor=owner, note=f"Assigned to {owner}")

    return _ok({
        "task_id": task_id,
        "owner": owner,
        "model_route": model_route,
        "task_status": "assigned",
        "note": "Task assigned. Ready for execution."
    }, ignored=_ignored("ceo_task_assign", args))


def ceo_run_create(args, **kwargs):
    """Create an orchestration run for a goal."""
    args, bad = _prep(args)
    if bad:
        return bad
    try:
        from . import tasks
    except ImportError:
        import tasks
    
    goal, e = _text(args, "goal")
    if e:
        return _err("Need goal")
    head_seat = args.get("head_seat", "CEO")
    model_key = args.get("model_key", "free_default")
    
    # Validate head_seat
    orch = _orch()
    canonical_seat = orch.normalize_seat(head_seat)
    if canonical_seat is None:
        return _err(f"Unknown head_seat. Valid: {', '.join(orch.SEATS)}")
    
    run_id = tasks.create_task_run(goal, canonical_seat, model_key)
    
    return _ok({
        "run_id": run_id,
        "goal": goal,
        "head_seat": canonical_seat,
        "model_key": model_key,
        "run_status": "planned",
        "note": "Run created. Use ceo_orchestrate or ceo_task_create to add tasks."
    }, ignored=_ignored("ceo_run_create", args))


def ceo_run_status(args, **kwargs):
    """Get orchestration run status, progress, and all associated tasks."""
    args, bad = _prep(args)
    if bad:
        return bad
    try:
        from . import tasks
    except ImportError:
        import tasks
    
    run_id, e = _text(args, "run_id")
    if e:
        return _err("Need run_id")
    
    run = tasks.get_task_run(run_id)
    if not run:
        return _err(f"Run {run_id} not found")
    
    run_tasks = tasks.get_tasks_by_run(run_id)
    
    # Count by status
    status_counts = {}
    for t in run_tasks:
        s = t.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1
    
    # Determine overall progress
    total = len(run_tasks)
    completed = status_counts.get("completed", 0) + status_counts.get("verified", 0)
    
    return _ok({
        "run_id": run_id,
        "goal": run.get("goal"),
        "head_seat": run.get("head_seat"),
        "model_key": run.get("model_key"),
        "run_status": run.get("status"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "completed_at": run.get("completed_at"),
        "task_count": total,
        "completed_count": completed,
        "status_counts": status_counts,
        "tasks": [
            {
                "task_id": t.get("id"),
                "seat": t.get("seat"),
                "title": t.get("title"),
                "status": t.get("status"),
                "owner": t.get("owner"),
                "due_date": t.get("due_date"),
            }
            for t in run_tasks
        ]
    }, ignored=_ignored("ceo_run_status", args))