def ceo_task_create(args, **kwargs):
    """Create a persistent task with full lifecycle tracking."""
    args, bad = _prep(args)
    if bad:
        return bad
    from . import tasks
    
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
        "status": "proposed",
        "approval_required": approval_required,
        "note": "Task created with status 'proposed'. Type 1 tasks require owner approval via ceo_task_approve."
    }, ignored=_ignored("ceo_task_create", args))


def ceo_task_status(args, **kwargs):
    """Get task status, history, and result."""
    args, bad = _prep(args)
    if bad:
        return bad
    from . import tasks
    
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
    from . import tasks
    
    run_id = args.get("run_id")
    seat = args.get("seat")
    status = args.get("status")
    limit = args.get("limit", 50)
    
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
    from . import tasks
    
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
        "status": "approved",
        "approved_by": approved_by,
        "note": "Task approved. Assign owner/profile via ceo_task_assign, then it can be executed."
    }, ignored=_ignored("ceo_task_approve", args))


def ceo_task_assign(args, **kwargs):
    """Assign a task to an owner/profile and optionally a model route."""
    args, bad = _prep(args)
    if bad:
        return bad
    from . import tasks
    
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
    
    # Update status to assigned if it was approved
    if task.get("status") == "approved":
        tasks.update_task_status(task_id, "assigned", actor=owner, note=f"Assigned to {owner}")
    
    return _ok({
        "task_id": task_id,
        "owner": owner,
        "model_route": model_route,
        "status": "assigned",
        "note": "Task assigned. Ready for execution."
    }, ignored=_ignored("ceo_task_assign", args))


def ceo_run_create(args, **kwargs):
    """Create an orchestration run for a goal."""
    args, bad = _prep(args)
    if bad:
        return bad
    from . import tasks
    
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
        "status": "planned",
        "note": "Run created. Use ceo_orchestrate or ceo_task_create to add tasks."
    }, ignored=_ignored("ceo_run_create", args))


def ceo_run_status(args, **kwargs):
    """Get orchestration run status, progress, and all associated tasks."""
    args, bad = _prep(args)
    if bad:
        return bad
    from . import tasks
    
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