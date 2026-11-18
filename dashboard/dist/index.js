/**
 * CEO Pack — Executive Workspace dashboard.
 *
 * Plain IIFE, no build step. Uses window.__HERMES_PLUGIN_SDK__ for React +
 * host components and SDK.fetchJSON for API calls (host auth handling);
 * registers via window.__HERMES_PLUGINS__.register("ceo-pack", ...).
 *
 * Read-only by design: the UI displays registry/roster state and links
 * every number to a backend read. There are no write actions — creating,
 * approving, or assigning work goes through the chat tools, which run the
 * lifecycle and permission checks. A second task engine in the frontend
 * is explicitly out of scope.
 *
 * Runtime truth: a task's status is shown exactly as the registry holds
 * it. Assigned is not "in progress", completed is not "verified". Where a
 * timestamp is missing the UI says "unknown" — it never invents one, and
 * zero counts are shown only as real counts of an empty registry.
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  var REGISTRY = window.__HERMES_PLUGINS__;
  if (!SDK || !REGISTRY || typeof REGISTRY.register !== "function") return;

  var React = SDK.React;
  var h = React.createElement;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useMemo = SDK.hooks.useMemo;
  var useCallback = SDK.hooks.useCallback;
  var fetchJSON = SDK.fetchJSON;
  var C = SDK.components || {};
  // Host components with plain-DOM fallbacks, so the tab still renders if
  // an older host predates a component.
  var Card = C.Card || "div";
  var CardHeader = C.CardHeader || "div";
  var CardTitle = C.CardTitle || "div";
  var CardContent = C.CardContent || "div";
  var Badge = C.Badge || "span";
  var Button = C.Button || "button";
  var Separator = C.Separator || "hr";
  var Input = C.Input || "input";
  var Label = C.Label || "label";
  var cn = (SDK.utils && SDK.utils.cn) || function () {
    return Array.prototype.slice.call(arguments).filter(Boolean).join(" ");
  };
  var isoTimeAgo = (SDK.utils && SDK.utils.isoTimeAgo) || function (iso) { return iso || "unknown"; };

  var API = "/api/plugins/ceo-pack";

  var STATUS_ORDER = ["proposed", "approved", "assigned", "running", "blocked", "completed", "verified", "failed"];
  var STATUS_LABEL = {
    proposed: "Proposed", approved: "Approved", assigned: "Assigned",
    running: "Running", blocked: "Blocked", completed: "Completed",
    verified: "Verified", failed: "Failed",
  };
  var TERMINAL = { completed: true, verified: true, failed: true };

  // FastAPI bodies look like {"detail": "<message>"}. Pull the readable
  // message out so banners don't leak HTTP plumbing at the user.
  function parseApiErrorMessage(err) {
    var raw = (err && err.message) ? String(err.message) : String(err || "");
    var m = raw.match(/^(\d{3}):\s*(.*)$/s);
    var body = m ? m[2] : raw;
    try {
      var parsed = JSON.parse(body);
      if (parsed && typeof parsed.detail === "string") return parsed.detail;
    } catch (_e) { /* not JSON — fall through */ }
    return body || raw;
  }

  function timeOrUnknown(iso) {
    if (!iso) return "unknown";
    try { return isoTimeAgo(iso); } catch (_e) { return iso; }
  }

  function isOverdue(task) {
    if (!task || !task.due_date || TERMINAL[task.status]) return false;
    var today = new Date();
    today.setHours(0, 0, 0, 0);
    var due = new Date(task.due_date + "T00:00:00");
    return !isNaN(due.getTime()) && due < today;
  }

  function Avatar(props) {
    var node = props.node;
    if (node.avatar_url) {
      return h("img", {
        className: "ceo-avatar-img", src: node.avatar_url,
        alt: node.display_name || node.seat,
      });
    }
    return h("span", { className: "ceo-avatar-initials" }, node.initials || "?");
  }

  function StatusBadge(props) {
    return h(Badge, { className: "ceo-status ceo-status-" + props.status },
      STATUS_LABEL[props.status] || props.status || "unknown");
  }

  function ErrorBox(props) {
    return h("div", { className: "ceo-error", role: "alert" },
      h("strong", null, "Could not load " + props.what + ". "),
      h("span", null, props.message),
      h(Button, { className: "ceo-retry", onClick: props.onRetry }, "Retry"));
  }

  function EmptyState(props) {
    return h("div", { className: "ceo-empty" }, props.children);
  }

  function TaskCard(props) {
    var t = props.task;
    return h("button", {
      type: "button",
      className: cn("ceo-task-card", props.selected && "ceo-task-card-selected"),
      onClick: function () { props.onSelect(t.task_id); },
    },
      h("span", { className: "ceo-task-title" }, t.title || "(untitled task)"),
      h("span", { className: "ceo-task-meta" },
        h(StatusBadge, { status: t.status }),
        t.owner ? h("span", null, " · " + t.owner) : null,
        t.due_date ? h("span", null, " · due " + t.due_date) : null,
        isOverdue(t) ? h(Badge, { className: "ceo-overdue" }, "Overdue") : null,
        t.approval_required ? h(Badge, { className: "ceo-type1" }, "Type 1") : null),
      h("span", { className: "ceo-task-sub" },
        (t.seat || "unassigned seat") + " · updated " + timeOrUnknown(t.updated_at)));
  }

  function TaskDetail(props) {
    var d = props.detail;
    if (!d) return null;
    return h(Card, { className: "ceo-detail" },
      h(CardHeader, null,
        h(CardTitle, null, d.title || "(untitled task)"),
        h(Button, { className: "ceo-close", onClick: props.onClose }, "Close")),
      h(CardContent, null,
        h("div", { className: "ceo-detail-grid" },
          h("div", null, h("strong", null, "Status: "), h(StatusBadge, { status: d.task_status || d.status })),
          h("div", null, h("strong", null, "Seat: "), d.seat || "unknown"),
          h("div", null, h("strong", null, "Owner: "), d.owner || "unassigned"),
          h("div", null, h("strong", null, "Route: "), d.model_route || "not routed"),
          h("div", null, h("strong", null, "Due: "), d.due_date || "none"),
          h("div", null, h("strong", null, "Created: "), timeOrUnknown(d.created_at)),
          h("div", null, h("strong", null, "Started: "), timeOrUnknown(d.started_at)),
          h("div", null, h("strong", null, "Completed: "), timeOrUnknown(d.completed_at)),
          h("div", null, h("strong", null, "Approved by: "), d.approved_by || "—"),
          h("div", null, h("strong", null, "Retries: "), String(d.retry_count == null ? 0 : d.retry_count))),
        h("div", { className: "ceo-detail-exec" },
          h("h4", null, "Delegated execution"),
          (function () {
            var ex = d.execution;
            if (!ex) return h(EmptyState, null, "Not run yet — no delegated execution recorded for this task.");
            var route = ex.route || {};
            var toks = ex.tokens || {};
            return h("div", { className: "ceo-detail-grid" },
              h("div", null, h("strong", null, "Requested route: "), d.model_route || "not routed"),
              h("div", null, h("strong", null, "Ran on: "),
                route.refused ? "route refused"
                  : ((route.provider || route.model)
                      ? ((route.provider || "unknown provider") + " / " + (route.model || "unknown model"))
                      : "unknown")),
              h("div", null, h("strong", null, "Route label: "), route.label || "unknown"),
              h("div", null, h("strong", null, "Backend: "), route.backend || "unknown"),
              h("div", null, h("strong", null, "Reasoning effort: "), route.reasoning_effort || "unknown"),
              h("div", null, h("strong", null, "Service tier: "), route.service_tier || "unknown"),
              route.fallback_activated ? h("div", null, h(Badge, { className: "ceo-type1" }, "fallback provider took over")) : null,
              h("div", null, h("strong", null, "Tokens in / out: "),
                (toks.input == null ? "unknown" : String(toks.input)) + " / " + (toks.output == null ? "unknown" : String(toks.output))),
              h("div", null, h("strong", null, "Cost: "),
                ex.cost_usd == null ? "unknown"
                  : ("$" + ex.cost_usd + (ex.cost_status ? " (" + ex.cost_status + ")" : ""))),
              h("div", null, h("strong", null, "Duration: "), ex.duration_seconds == null ? "unknown" : (ex.duration_seconds + "s")),
              h("div", null, h("strong", null, "API calls: "), ex.api_calls == null ? "unknown" : String(ex.api_calls)),
              ex.exit_reason ? h("div", null, h("strong", null, "Exit: "), ex.exit_reason) : null,
              ex.failure_reason ? h("div", null, h("strong", null, "Failure: "), ex.failure_reason) : null,
              h("div", null, h("strong", null, "Recorded: "), timeOrUnknown(ex.recorded_at)));
          })()),
        h("div", { className: "ceo-detail-deps" },
          h("strong", null, "Dependencies: "),
          (d.dependencies && d.dependencies.length)
            ? d.dependencies.join(", ")
            : "none",
          d.dependencies_met === false ? h(Badge, { className: "ceo-type1" }, "blocked by dependency") : null),
        h("h4", null, "Execution history"),
        (d.history && d.history.length)
          ? h("ul", { className: "ceo-history" }, d.history.map(function (ev) {
              return h("li", { key: String(ev.id) },
                h("span", null, (ev.from_status || "—") + " → " + ev.to_status),
                h("span", { className: "ceo-history-meta" },
                  " · " + (ev.actor || "system") + " · " + timeOrUnknown(ev.at)),
                ev.note ? h("span", { className: "ceo-history-note" }, " — " + ev.note) : null);
            }))
          : h(EmptyState, null, "No recorded transitions yet — the task has not moved.")));
  }

  function OrgNode(props) {
    var node = props.node;
    var selected = props.selectedSeat === node.seat;
    return h("div", { className: "ceo-org-node-wrap" },
      h("button", {
        type: "button",
        className: cn("ceo-org-node", selected && "ceo-org-node-selected"),
        onClick: function () { props.onSelectSeat(node.seat); },
        title: "Show this seat's assignments",
      },
        h(Avatar, { node: node }),
        h("span", { className: "ceo-org-name" }, node.display_name || node.seat),
        h("span", { className: "ceo-org-seat" }, node.seat),
        h("span", { className: "ceo-org-binding" },
          node.bound ? ("Bound: " + (node.profile || "profile")) : "Reference agent"),
        node.missing && node.missing.length
          ? h(Badge, { className: "ceo-missing" }, node.missing.length + " capability gap" + (node.missing.length === 1 ? "" : "s"))
          : null),
      node.children && node.children.length
        ? h("div", { className: "ceo-org-children" },
            node.children.map(function (child) {
              return h(OrgNode, {
                key: child.seat, node: child,
                selectedSeat: props.selectedSeat, onSelectSeat: props.onSelectSeat,
              });
            }))
        : null);
  }

  function CEOPackPage() {
    var _s = useState(null);
    var data = _s[0], setData = _s[1];
    var _e = useState({});
    var errors = _e[0], setErrors = _e[1];
    var _l = useState(true);
    var loading = _l[0], setLoading = _l[1];
    var _t = useState("org");
    var tab = _t[0], setTab = _t[1];
    var _ss = useState(null);
    var selectedSeat = _ss[0], setSelectedSeat = _ss[1];
    var _st = useState(null);
    var selectedTaskId = _st[0], setSelectedTaskId = _st[1];
    var _sd = useState(null);
    var selectedTask = _sd[0], setSelectedTask = _sd[1];
    var _f = useState("");
    var filterText = _f[0], setFilterText = _f[1];
    var _fs = useState("all");
    var filterStatus = _fs[0], setFilterStatus = _fs[1];

    var load = useCallback(function () {
      setLoading(true);
      var jobs = {
        summary: fetchJSON(API + "/summary"),
        roster: fetchJSON(API + "/roster"),
        tasks: fetchJSON(API + "/tasks?limit=200"),
        activity: fetchJSON(API + "/activity"),
        approvals: fetchJSON(API + "/approvals"),
        runs: fetchJSON(API + "/runs?limit=20"),
      };
      var keys = Object.keys(jobs);
      Promise.all(keys.map(function (k) {
        return jobs[k].then(
          function (v) { return { k: k, ok: true, v: v }; },
          function (err) { return { k: k, ok: false, err: parseApiErrorMessage(err) }; });
      })).then(function (results) {
        var next = {};
        var nextErrors = {};
        results.forEach(function (r) {
          if (r.ok) next[r.k] = r.v; else nextErrors[r.k] = r.err;
        });
        setData(function (prev) { return Object.assign({}, prev || {}, next); });
        setErrors(nextErrors);
        setLoading(false);
      });
    }, []);

    useEffect(function () {
      load();
      var timer = setInterval(load, 30000);
      return function () { clearInterval(timer); };
    }, [load]);

    useEffect(function () {
      if (!selectedTaskId) { setSelectedTask(null); return; }
      fetchJSON(API + "/tasks/" + encodeURIComponent(selectedTaskId)).then(
        function (v) { setSelectedTask(v); },
        function (err) { setSelectedTask({ _error: parseApiErrorMessage(err) }); });
    }, [selectedTaskId]);

    var summary = (data && data.summary) || null;
    var roster = (data && data.roster) || null;
    var tasks = ((data && data.tasks) || {}).tasks || [];
    var activity = (data && data.activity) || null;
    var approvals = (data && data.approvals) || null;

    var filteredTasks = useMemo(function () {
      var q = filterText.trim().toLowerCase();
      return tasks.filter(function (t) {
        if (selectedSeat && t.seat !== selectedSeat) return false;
        if (filterStatus !== "all" && t.status !== filterStatus) return false;
        if (q && (t.title || "").toLowerCase().indexOf(q) === -1) return false;
        return true;
      });
    }, [tasks, selectedSeat, filterStatus, filterText]);

    var grouped = useMemo(function () {
      var g = {};
      STATUS_ORDER.forEach(function (s) { g[s] = []; });
      filteredTasks.forEach(function (t) {
        (g[t.status] || (g[t.status] = [])).push(t);
      });
      return g;
    }, [filteredTasks]);

    function panelError(key, what) {
      if (!errors[key]) return null;
      return h(ErrorBox, { what: what, message: errors[key], onRetry: load });
    }

    function renderSummaryStrip() {
      if (errors.summary) return panelError("summary", "summary counts");
      if (!summary) return h("div", { className: "ceo-loading" }, "Loading summary…");
      if (summary.empty) {
        return h(EmptyState, null,
          "No tasks yet — the registry is empty. Assign work from chat (ceo_task_create) and it shows up here.");
      }
      var c = summary.tasks_by_status || {};
      function stat(label, value) {
        return h("div", { className: "ceo-stat" },
          h("span", { className: "ceo-stat-value" }, String(value)),
          h("span", { className: "ceo-stat-label" }, label));
      }
      return h("div", { className: "ceo-stats" },
        stat("Tasks", summary.tasks_total),
        stat("Running", c.running || 0),
        stat("Blocked / failed", (c.blocked || 0) + (c.failed || 0)),
        stat("Pending approval", summary.pending_approvals),
        stat("Verified", c.verified || 0),
        stat("Runs", summary.total_runs));
    }

    function renderOrg() {
      if (errors.roster) return panelError("roster", "org chart");
      if (!roster) return h("div", { className: "ceo-loading" }, "Loading org chart…");
      var root = roster.org && roster.org.root;
      var seatTasks = selectedSeat
        ? tasks.filter(function (t) { return t.seat === selectedSeat; })
        : [];
      return h("div", null,
        h("p", { className: "ceo-note" },
          "Reporting shown by the plugin's accountability model — the head seat holds priorities. ",
          "Departments and custom reporting lines plug in here when they exist; none are shown until they do. ",
          roster.snapshot_loaded
            ? "Roster loaded from the applied install config."
            : "No install config seen yet — showing the default roster."),
        root ? h(OrgNode, { node: root, selectedSeat: selectedSeat, onSelectSeat: setSelectedSeat }) : null,
        selectedSeat ? h("div", { className: "ceo-seat-tasks" },
          h("h3", null, "Assignments: " + selectedSeat + " (" + seatTasks.length + ")"),
          seatTasks.length
            ? seatTasks.map(function (t) {
                return h(TaskCard, { key: t.task_id, task: t, selected: selectedTaskId === t.task_id, onSelect: setSelectedTaskId });
              })
            : h(EmptyState, null, "No assignments recorded for this seat yet."),
          h(Button, { onClick: function () { setSelectedSeat(null); } }, "Clear seat filter"))
          : h("p", { className: "ceo-note" }, "Click a seat to see its assignments."));
    }

    function renderActivity() {
      if (errors.activity) return panelError("activity", "activity");
      if (!activity) return h("div", { className: "ceo-loading" }, "Loading activity…");
      var hist = activity.recent_history || [];
      return h("div", null,
        h("h3", null, "Running now (" + (activity.running || []).length + ")"),
        (activity.running || []).length
          ? activity.running.map(function (t) {
              return h(TaskCard, { key: t.task_id, task: t, selected: selectedTaskId === t.task_id, onSelect: setSelectedTaskId });
            })
          : h(EmptyState, null, "Nothing running right now."),
        h("h3", null, "Blocked / stuck (" + (activity.stuck || []).length + ")"),
        (activity.stuck || []).length
          ? activity.stuck.map(function (t) {
              return h(TaskCard, { key: t.task_id, task: t, selected: selectedTaskId === t.task_id, onSelect: setSelectedTaskId });
            })
          : h(EmptyState, null, "Nothing blocked or stuck."),
        h("h3", null, "Recent execution history"),
        hist.length
          ? h("ul", { className: "ceo-history" }, hist.map(function (ev) {
              return h("li", { key: String(ev.id) },
                h("span", null, (ev.title || ev.task_id) + ": " + (ev.from_status || "—") + " → " + ev.to_status),
                h("span", { className: "ceo-history-meta" },
                  " · " + (ev.actor || "system") + " · " + timeOrUnknown(ev.at)),
                ev.note ? h("span", { className: "ceo-history-note" }, " — " + ev.note) : null);
            }))
          : h(EmptyState, null, "No recorded activity yet."));
    }

    function renderBoard() {
      if (errors.tasks) return panelError("tasks", "task board");
      if (!data || !data.tasks) return h("div", { className: "ceo-loading" }, "Loading tasks…");
      return h("div", null,
        h("div", { className: "ceo-filters" },
          h(Label, null, "Seat"),
          h("select", {
            className: "ceo-select", value: selectedSeat || "",
            onChange: function (e) { setSelectedSeat(e.target.value || null); },
          },
            h("option", { value: "" }, "All seats"),
            (roster ? roster.seats : []).map(function (s) {
              return h("option", { key: s.seat, value: s.seat }, s.display_name || s.seat);
            })),
          h(Label, null, "Status"),
          h("select", {
            className: "ceo-select", value: filterStatus,
            onChange: function (e) { setFilterStatus(e.target.value); },
          },
            h("option", { value: "all" }, "All statuses"),
            STATUS_ORDER.map(function (s) {
              return h("option", { key: s, value: s }, STATUS_LABEL[s]);
            })),
          h(Input, {
            className: "ceo-search", placeholder: "Search tasks…",
            value: filterText, onChange: function (e) { setFilterText(e.target.value); },
          })),
        selectedTask
          ? (selectedTask._error
              ? h(ErrorBox, { what: "task detail", message: selectedTask._error, onRetry: function () { setSelectedTaskId(null); } })
              : h(TaskDetail, { detail: selectedTask, onClose: function () { setSelectedTaskId(null); } }))
          : null,
        tasks.length === 0
          ? h(EmptyState, null, "No tasks in the registry yet. Create one from chat (ceo_task_create) — it appears here, with its real status, once it exists.")
          : STATUS_ORDER.map(function (s) {
              var list = grouped[s] || [];
              if (!list.length) return null;
              return h("div", { key: s, className: "ceo-status-group" },
                h("h3", null, STATUS_LABEL[s] + " (" + list.length + ")"),
                list.map(function (t) {
                  return h(TaskCard, { key: t.task_id, task: t, selected: selectedTaskId === t.task_id, onSelect: setSelectedTaskId });
                }));
            }));
    }

    function renderApprovals() {
      if (errors.approvals) return panelError("approvals", "approvals");
      if (!approvals) return h("div", { className: "ceo-loading" }, "Loading approvals…");
      var pending = approvals.pending || [];
      var hist = approvals.history || [];
      return h("div", null,
        h("h3", null, "Pending owner approval (" + approvals.pending_count + ")"),
        pending.length
          ? pending.map(function (t) {
              return h(TaskCard, { key: t.task_id, task: t, selected: selectedTaskId === t.task_id, onSelect: setSelectedTaskId });
            })
          : h(EmptyState, null, "Nothing waiting on the owner right now."),
        h("h3", null, "Approval history"),
        hist.length
          ? h("ul", { className: "ceo-history" }, hist.map(function (ev) {
              return h("li", { key: String(ev.id) },
                h("span", null, (ev.title || ev.task_id) + " approved"),
                h("span", { className: "ceo-history-meta" },
                  " · by " + (ev.actor || "unknown") + " · " + timeOrUnknown(ev.at)));
            }))
          : h(EmptyState, null, "No approvals recorded yet."),
        h("p", { className: "ceo-note" }, approvals.decisions_note || ""));
    }

    var tabs = [
      { id: "org", label: "Org Chart" },
      { id: "activity", label: "Activity" },
      { id: "board", label: "Task Board" },
      { id: "approvals", label: "Approvals" },
    ];

    return h("div", { className: "ceo-pack" },
      h("div", { className: "ceo-header" },
        h("div", null,
          h("h1", null, "CEO Pack — Executive Workspace"),
          h("p", { className: "ceo-subtitle" },
            "Your AI C-suite at a glance. Every number below is a live read of the task registry — nothing is invented.")),
        h("div", { className: "ceo-header-actions" },
          h(Button, { onClick: load }, "Refresh"),
          h("span", { className: "ceo-updated" },
            loading ? "Loading…" : ("Updated " + timeOrUnknown(summary ? summary.generated_at : null))))),
      renderSummaryStrip(),
      h(Separator, null),
      h("div", { className: "ceo-tabs", role: "tablist" },
        tabs.map(function (t) {
          return h(Button, {
            key: t.id,
            className: cn("ceo-tab", tab === t.id && "ceo-tab-active"),
            onClick: function () { setTab(t.id); },
          }, t.label);
        })),
      h("div", { className: "ceo-panel" },
        tab === "org" ? renderOrg()
          : tab === "activity" ? renderActivity()
          : tab === "board" ? renderBoard()
          : renderApprovals()));
  }

  REGISTRY.register("ceo-pack", CEOPackPage);
})();
