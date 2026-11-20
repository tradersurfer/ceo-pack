/**
 * CEO Pack — Hermes Desktop half (v2.1: native + color/interactive polish).
 *
 * Plain ESM runtime plugin, loaded uncompiled by the Desktop app. No build
 * step, no Hermes repo changes. The Desktop app's sidebar does NOT read the
 * server's dashboard manifests (that mechanism feeds the web Dashboard UI
 * only) — its sidebar rows come from Desktop plugins, and this is that half.
 *
 * v1 embedded the served dashboard in a SandboxedFrame. It rendered the
 * gateway login screen: the sandboxed iframe carries none of the Desktop
 * app's auth, and the gateway login-walls the dashboard. v2 renders the
 * dashboard NATIVELY: every read/write goes through `ctx.rest`, which the
 * main process authenticates with the native grant it mints for the active
 * profile — the same API the web dashboard uses
 * (`dashboard/API_CONTRACT.md`), no credentials anywhere, nothing
 * re-implemented server-side. The backend stays authoritative; this file is
 * presentation only.
 *
 * Doors (same file, same contract):
 *   - Standalone (test tonight):
 *       %LOCALAPPDATA%\hermes\desktop-plugins\ceo-pack\plugin.js
 *     Loads enabled by default; hot-reloads on save, or Cmd+K ->
 *     "Reload desktop plugins".
 *   - Unified package (durable home): <ceo-pack repo>/desktop/plugin.js
 *     Ships beside plugin.yaml + dashboard/plugin_api.py; the Electron shell
 *     copies it into desktop-plugins/ceo-pack/ with a .hermes-package.json
 *     marker. OPT-IN: flip it on in Capabilities -> Plugins after install.
 *
 * Polling mirrors the web dashboard: every query refetches every 30s.
 * "Live" means last_activity under 2 minutes old. Missing timestamps render
 * "unknown" — never invented.
 */

import { host, PALETTE_AREA, queryClient, ROUTES_AREA, SIDEBAR_NAV_AREA, useQuery } from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'ceo-pack'
const POLL_MS = 30000
const LIVE_WINDOW_MS = 2 * 60 * 1000
const QUERY_ROOT = ['ceo-pack']

// Bound in register() from ctx.rest (namespace-scoped to /api/plugins/ceo-pack).
let rest = null

function api(path, opts) {
  if (!rest) throw new Error('CEO Pack backend not bound yet')
  return rest(path, opts)
}

function refreshAll() {
  return queryClient.invalidateQueries({ queryKey: QUERY_ROOT })
}

function relTime(iso) {
  if (!iso) return 'unknown'
  const ms = Date.now() - new Date(iso).getTime()
  if (Number.isNaN(ms) || ms < 0) return 'unknown'
  if (ms < 60 * 1000) return 'just now'
  if (ms < 60 * 60 * 1000) return `${Math.floor(ms / 60000)}m ago`
  if (ms < 24 * 60 * 60 * 1000) return `${Math.floor(ms / 3600000)}h ago`
  return `${Math.floor(ms / 86400000)}d ago`
}

function isLive(iso) {
  if (!iso) return false
  const ms = Date.now() - new Date(iso).getTime()
  return !Number.isNaN(ms) && ms >= 0 && ms < LIVE_WINDOW_MS
}

function taskId(t) {
  return t.task_id || t.id
}

function runId(r) {
  return r.run_id || r.id
}

const TAB_LABELS = [
  ['org', 'Org Chart'],
  ['activity', 'Activity'],
  ['board', 'Task Board'],
  ['approvals', 'Approvals']
]

const STATUS_ORDER = ['proposed', 'approved', 'assigned', 'running', 'completed', 'verified', 'blocked', 'failed', 'rejected']

function statusLabel(s) {
  return s === 'blocked' || s === 'failed' ? 'Blocked / failed' : s.charAt(0).toUpperCase() + s.slice(1)
}

const STATUS_COLORS = {
  proposed: 'var(--ui-accent)',
  approved: 'var(--ui-success)',
  assigned: 'var(--ui-accent-secondary)',
  running: 'var(--ui-accent)',
  completed: 'var(--ui-success)',
  verified: 'var(--ui-success)',
  blocked: 'var(--ui-danger)',
  failed: 'var(--ui-danger)',
  rejected: 'var(--ui-text-tertiary)'
}

function StatusPill({ status }) {
  const color = STATUS_COLORS[status] || 'var(--ui-text-secondary)'
  return jsx('span', {
    style: { border: `1px solid ${color}`, color, padding: '1px 8px', fontSize: '0.8rem', whiteSpace: 'nowrap' },
    children: statusLabel(status)
  })
}

function useCeoData() {
  const summary = useQuery({ queryKey: [...QUERY_ROOT, 'summary'], queryFn: () => api('/summary'), refetchInterval: POLL_MS })
  const roster = useQuery({ queryKey: [...QUERY_ROOT, 'roster'], queryFn: () => api('/roster'), refetchInterval: POLL_MS })
  const tasks = useQuery({ queryKey: [...QUERY_ROOT, 'tasks'], queryFn: () => api('/tasks?limit=50'), refetchInterval: POLL_MS })
  const activity = useQuery({ queryKey: [...QUERY_ROOT, 'activity'], queryFn: () => api('/activity'), refetchInterval: POLL_MS })
  const approvals = useQuery({ queryKey: [...QUERY_ROOT, 'approvals'], queryFn: () => api('/approvals'), refetchInterval: POLL_MS })
  const runs = useQuery({ queryKey: [...QUERY_ROOT, 'runs'], queryFn: () => api('/runs?limit=20'), refetchInterval: POLL_MS })
  return { summary, roster, tasks, activity, approvals, runs }
}

function StatBox({ value, label, color }) {
  return jsxs('div', {
    style: { border: '1px solid var(--ui-stroke-secondary)', padding: '12px 16px', minWidth: '110px' },
    children: [
      jsx('div', { style: { fontSize: '1.5rem', fontWeight: 600, color: color || 'var(--ui-text-primary)' }, children: String(value) }),
      jsx('div', { style: { color: 'var(--ui-text-secondary)', fontSize: '0.85rem' }, children: label })
    ]
  })
}

function SummaryStrip({ summary }) {
  if (!summary) return null
  const byStatus = summary.tasks_by_status || {}
  return jsxs('div', {
    style: { display: 'flex', gap: '12px', flexWrap: 'wrap', marginBottom: '16px' },
    children: [
      jsx(StatBox, { value: summary.tasks_total ?? 0, label: 'Tasks' }),
      jsx(StatBox, { value: summary.running ?? 0, label: 'Running', color: (summary.running ?? 0) > 0 ? 'var(--ui-accent)' : undefined }),
      jsx(StatBox, { value: summary.stuck ?? 0, label: 'Blocked / failed', color: (summary.stuck ?? 0) > 0 ? 'var(--ui-danger)' : undefined }),
      jsx(StatBox, { value: summary.pending_approvals ?? 0, label: 'Pending approval', color: (summary.pending_approvals ?? 0) > 0 ? 'var(--ui-accent)' : undefined }),
      jsx(StatBox, { value: byStatus.verified ?? 0, label: 'Verified', color: (byStatus.verified ?? 0) > 0 ? 'var(--ui-success)' : undefined }),
      jsx(StatBox, { value: summary.total_runs ?? 0, label: 'Runs' })
    ]
  })
}

function TabButton({ id, label, active, onSelect }) {
  return jsx('button', {
    type: 'button',
    onClick: () => onSelect(id),
    style: {
      padding: '6px 14px',
      border: '1px solid var(--ui-stroke-secondary)',
      background: 'transparent',
      color: active ? 'var(--ui-accent)' : 'var(--ui-text-primary)',
      borderColor: active ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)',
      cursor: 'pointer'
    },
    children: label
  })
}

function CeoPackPage() {
  const [tab, setTab] = useState('org')
  const [selectedSeat, setSelectedSeat] = useState(null)
  const [writeBusy, setWriteBusy] = useState(false)
  const [writeError, setWriteError] = useState('')
  const data = useCeoData()
  const loading = data.summary.isLoading || data.roster.isLoading
  const loadError = data.summary.error || data.roster.error || data.tasks.error || data.approvals.error

  async function write(path, opts) {
    setWriteBusy(true)
    setWriteError('')
    try {
      const out = await api(path, opts)
      await refreshAll()
      return out
    } catch (err) {
      setWriteError(err && err.message ? err.message : String(err))
      return null
    } finally {
      setWriteBusy(false)
    }
  }

  const writes = {
    approve: (id) => write(`/tasks/${id}/approve`, { method: 'POST' }),
    reject: (id) => write(`/tasks/${id}/reject`, { method: 'POST', body: { note: 'Rejected from Desktop' } }),
    assign: (id, owner) => write(`/tasks/${id}/assign`, { method: 'POST', body: { owner } }),
    create: (body) => write('/tasks', { method: 'POST', body })
  }

  if (loading) {
    return jsx('div', { style: { padding: '24px' }, children: 'Loading CEO Pack…' })
  }

  return jsxs('div', {
    style: { padding: '20px 24px', height: '100%', overflowY: 'auto' },
    children: [
      jsx('style', { children: '@keyframes ceoPulse { 0% { opacity: 1; } 50% { opacity: 0.3; } 100% { opacity: 1; } }' }),
      jsx('h1', { style: { fontSize: '1.4rem', margin: '0 0 4px' }, children: 'CEO Pack — Executive Workspace' }),
      jsx('p', { style: { color: 'var(--ui-text-secondary)', margin: '0 0 16px' }, children: 'Your AI C-suite at a glance. Every number below is a live read of the task registry — nothing is invented.' }),
      jsx(SummaryStrip, { summary: data.summary.data }),
      loadError ? jsx('div', {
        style: { border: '1px solid var(--ui-stroke-secondary)', padding: '12px', marginBottom: '12px' },
        children: `Couldn't reach the registry: ${loadError.message || loadError}`
      }) : null,
      writeError ? jsx('div', {
        style: { border: '1px solid var(--ui-stroke-secondary)', padding: '12px', marginBottom: '12px' },
        children: `Write failed: ${writeError}`
      }) : null,
      jsxs('div', {
        style: { display: 'flex', gap: '8px', marginBottom: '16px', flexWrap: 'wrap' },
        children: TAB_LABELS.map(([id, label]) => jsx(TabButton, { id, label, active: tab === id, onSelect: setTab, key: id }))
      }),
      tab === 'org' ? jsx(OrgChart, { roster: data.roster.data, tasks: data.tasks.data, selectedSeat, onSelectSeat: setSelectedSeat }) : null,
      tab === 'activity' ? jsx(ActivityPanel, { activity: data.activity.data }) : null,
      tab === 'board' ? jsx(TaskBoard, { tasks: data.tasks.data, roster: data.roster.data, runs: data.runs.data, writes, busy: writeBusy }) : null,
      tab === 'approvals' ? jsx(ApprovalsPanel, { approvals: data.approvals.data, writes, busy: writeBusy }) : null,
      jsxs('div', {
        style: { marginTop: '16px' },
        children: [
          jsx('button', {
            type: 'button',
            disabled: writeBusy,
            onClick: () => { refreshAll() },
            style: { padding: '6px 14px', border: '1px solid var(--ui-stroke-secondary)', background: 'transparent', cursor: 'pointer' },
            children: 'Refresh'
          })
        ]
      })
    ]
  })
}

function SeatCard({ node, selected, onSelect }) {
  const live = isLive(node.last_activity)
  const gaps = (node.missing || []).length
  return jsxs('button', {
    type: 'button',
    onClick: () => onSelect(node.seat),
    style: {
      border: '1px solid var(--ui-stroke-secondary)',
      background: selected ? 'var(--ui-accent)' : 'transparent',
      color: 'var(--ui-text-primary)',
      padding: '14px',
      minWidth: '150px',
      cursor: 'pointer',
      textAlign: 'center'
    },
    children: [
      jsxs('div', {
        style: { display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '6px' },
        children: [
          jsx('span', {
            style: {
              width: '10px', height: '10px', borderRadius: '50%',
              background: live ? 'var(--ui-success)' : 'var(--ui-stroke-secondary)',
              display: 'inline-block',
              animation: live ? 'ceoPulse 1.6s ease-in-out infinite' : undefined
            },
            children: ''
          }),
          jsx('span', { style: { fontWeight: 600 }, children: node.display_name || node.seat })
        ]
      }),
      jsx('div', { style: { color: 'var(--ui-text-secondary)', fontSize: '0.85rem', marginTop: '4px' }, children: node.seat }),
      jsx('div', { style: { color: 'var(--ui-text-secondary)', fontSize: '0.85rem' }, children: `Last active: ${relTime(node.last_activity)}` }),
      jsx('div', { style: { color: 'var(--ui-text-secondary)', fontSize: '0.85rem' }, children: node.profile ? node.profile : 'Reference agent' }),
      gaps > 0 ? jsx('div', { style: { marginTop: '6px', fontSize: '0.8rem' }, children: `${gaps} CAPABILITY GAP${gaps === 1 ? '' : 'S'}` }) : null
    ]
  })
}

function OrgChart({ roster, tasks, selectedSeat, onSelectSeat }) {
  if (!roster) return jsx('div', { children: 'No roster yet.' })
  const seats = roster.seats || []
  const head = seats.find((s) => s.seat === roster.head_seat) || seats[0]
  const restSeats = seats.filter((s) => s !== head)
  const seatTasks = selectedSeat && tasks
    ? (tasks.tasks || []).filter((t) => t.seat === selectedSeat)
    : []
  return jsxs('div', {
    children: [
      jsx('p', { style: { color: 'var(--ui-text-secondary)' }, children: 'Reporting shown by the plugin’s accountability model — the head seat holds priorities. Departments and custom reporting lines plug in here when they exist; none are shown until they do. Roster loaded from the applied install config.' }),
      head ? jsx('div', { style: { display: 'flex', justifyContent: 'center', marginBottom: '12px' }, children: jsx(SeatCard, { node: head, selected: selectedSeat === head.seat, onSelect: onSelectSeat }) }) : null,
      jsxs('div', {
        style: { display: 'flex', gap: '12px', flexWrap: 'wrap', justifyContent: 'center' },
        children: restSeats.map((s) => jsx(SeatCard, { node: s, selected: selectedSeat === s.seat, onSelect: onSelectSeat, key: s.seat }))
      }),
      selectedSeat ? jsxs('div', {
        style: { marginTop: '20px' },
        children: [
          jsxs('div', {
            style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center' },
            children: [
              jsx('h3', { style: { margin: 0 }, children: `Assignments: ${selectedSeat} (${seatTasks.length})` }),
              jsx('button', {
                type: 'button',
                onClick: () => onSelectSeat(null),
                style: { padding: '4px 10px', border: '1px solid var(--ui-stroke-secondary)', background: 'transparent', cursor: 'pointer' },
                children: 'Clear seat filter'
              })
            ]
          }),
          seatTasks.length === 0
            ? jsx('p', { style: { color: 'var(--ui-text-secondary)' }, children: 'No tasks for this seat yet.' })
            : jsx('div', {
              style: { display: 'flex', flexDirection: 'column', gap: '8px', marginTop: '8px' },
              children: seatTasks.map((t) => jsx(TaskRow, { task: t, key: taskId(t) }))
            })
        ]
      }) : null
    ]
  })
}

function ActivityPanel({ activity }) {
  if (!activity) return jsx('div', { children: 'No activity yet.' })
  const history = activity.recent_history || []
  return jsxs('div', {
    children: [
      jsx('h3', { children: `Running now (${(activity.running || []).length})` }),
      (activity.running || []).length === 0
        ? jsx('p', { style: { color: 'var(--ui-text-secondary)' }, children: 'Nothing running right now.' })
        : jsx('div', {
          style: { display: 'flex', flexDirection: 'column', gap: '8px' },
          children: (activity.running || []).map((t) => jsx(TaskRow, { task: t, key: taskId(t) }))
        }),
      jsx('h3', { children: `Blocked / stuck (${(activity.stuck || []).length})` }),
      (activity.stuck || []).length === 0
        ? jsx('p', { style: { color: 'var(--ui-text-secondary)' }, children: 'Nothing blocked or stuck.' })
        : jsx('div', {
          style: { display: 'flex', flexDirection: 'column', gap: '8px' },
          children: (activity.stuck || []).map((t) => jsx(TaskRow, { task: t, key: taskId(t) }))
        }),
      jsx('h3', { children: 'Recent execution history' }),
      history.length === 0
        ? jsx('p', { style: { color: 'var(--ui-text-secondary)' }, children: 'No history yet.' })
        : jsx('div', {
          style: { display: 'flex', flexDirection: 'column', gap: '4px' },
          children: history.map((h, i) => jsx('div', {
            key: i,
            style: { borderBottom: '1px solid var(--ui-stroke-secondary)', padding: '6px 0' },
            children: `${h.title || h.task_title || 'Task'}: ${h.from_status || '—'} → ${h.to_status || '—'} · ${h.actor || 'system'} · ${relTime(h.at)}${h.note ? ` — ${h.note}` : ''}`
          }))
        })
    ]
  })
}

function TaskRow({ task }) {
  return jsxs('div', {
    style: { border: '1px solid var(--ui-stroke-secondary)', padding: '10px 12px' },
    children: [
      jsx('div', { style: { fontWeight: 600 }, children: task.title }),
      jsxs('div', {
        style: { display: 'flex', gap: '8px', marginTop: '4px', fontSize: '0.85rem', color: 'var(--ui-text-secondary)', alignItems: 'center' },
        children: [
          jsx(StatusPill, { status: task.status }),
          task.approval_required ? jsx('span', { style: { color: 'var(--ui-danger)' }, children: 'TYPE 1' }) : null,
          jsx('span', { children: `${task.seat} · updated ${relTime(task.updated_at)}` })
        ]
      })
    ]
  })
}

function TaskCard({ task, writes, busy }) {
  const [assignee, setAssignee] = useState(task.seat || '')
  const [open, setOpen] = useState(false)
  const canApprove = task.status === 'proposed' && task.approval_required
  const canAssign = task.status === 'approved' || task.status === 'proposed'
  const exec = task.execution || null
  return jsxs('div', {
    style: { border: '1px solid var(--ui-stroke-secondary)', padding: '10px 12px' },
    children: [
      jsx('div', {
        onClick: () => setOpen(!open),
        style: { fontWeight: 600, cursor: 'pointer' },
        children: `${open ? '▾' : '▸'} ${task.title}`
      }),
      jsxs('div', {
        style: { display: 'flex', gap: '8px', marginTop: '4px', fontSize: '0.85rem', color: 'var(--ui-text-secondary)', alignItems: 'center' },
        children: [
          jsx(StatusPill, { status: task.status }),
          task.approval_required ? jsx('span', { style: { color: 'var(--ui-danger)' }, children: 'TYPE 1' }) : null,
          jsx('span', { children: `${task.seat} · updated ${relTime(task.updated_at)}` })
        ]
      }),
      open ? jsxs('div', {
        style: { marginTop: '8px', fontSize: '0.85rem', color: 'var(--ui-text-secondary)', display: 'flex', flexDirection: 'column', gap: '2px' },
        children: [
          task.description ? jsx('div', { children: task.description }) : null,
          jsx('div', { children: `Created ${relTime(task.created_at)}${task.due_date ? ` · due ${task.due_date}` : ''}${task.approved_by ? ` · approved by ${task.approved_by}` : ''}` }),
          exec ? jsx('div', { children: `Run: ${exec.status || '—'}${exec.cost_usd != null ? ` · $${exec.cost_usd}` : ''}${exec.cost_status === 'unknown' ? ' · cost unknown' : ''}${exec.duration_seconds != null ? ` · ${exec.duration_seconds}s` : ''}` }) : null
        ]
      }) : null,
      jsxs('div', {
        style: { display: 'flex', gap: '8px', marginTop: '8px', flexWrap: 'wrap', alignItems: 'center' },
        children: [
          canApprove ? jsx('button', {
            type: 'button', disabled: busy,
            onClick: () => writes.approve(taskId(task)),
            style: { padding: '4px 12px', border: '1px solid var(--ui-stroke-secondary)', background: 'transparent', cursor: 'pointer' },
            children: 'Approve'
          }) : null,
          canApprove ? jsx('button', {
            type: 'button', disabled: busy,
            onClick: () => writes.reject(taskId(task)),
            style: { padding: '4px 12px', border: '1px solid var(--ui-stroke-secondary)', background: 'transparent', cursor: 'pointer' },
            children: 'Reject'
          }) : null,
          canAssign ? jsxs('span', {
            style: { display: 'inline-flex', gap: '6px', alignItems: 'center' },
            children: [
              jsx('input', {
                value: assignee,
                onChange: (e) => setAssignee(e.target.value),
                placeholder: 'Assign to…',
                style: { padding: '4px 8px', border: '1px solid var(--ui-stroke-secondary)', background: 'transparent', color: 'var(--ui-text-primary)' }
              }),
              jsx('button', {
                type: 'button', disabled: busy || !assignee.trim(),
                onClick: () => writes.assign(taskId(task), assignee.trim()),
                style: { padding: '4px 12px', border: '1px solid var(--ui-stroke-secondary)', background: 'transparent', cursor: 'pointer' },
                children: 'Assign'
              })
            ]
          }) : null
        ]
      })
    ]
  })
}

function TaskBoard({ tasks, roster, runs, writes, busy }) {
  const [title, setTitle] = useState('')
  const [seat, setSeat] = useState('')
  const [run, setRun] = useState('')
  const [type1, setType1] = useState(false)
  const [seatFilter, setSeatFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [search, setSearch] = useState('')
  const list = (tasks && tasks.tasks) || []
  const seats = (roster && roster.seats) || []
  const runList = (runs && runs.runs) || (runs && runs.items) || []
  const filtered = list.filter((t) => {
    if (seatFilter && t.seat !== seatFilter) return false
    if (statusFilter && t.status !== statusFilter) return false
    if (search && !(t.title || '').toLowerCase().includes(search.toLowerCase())) return false
    return true
  })
  const groups = {}
  for (const t of filtered) {
    const g = groups[t.status] || (groups[t.status] = [])
    g.push(t)
  }
  const canCreate = title.trim() && seat && run

  async function create() {
    if (!canCreate) return
    const out = await writes.create({ run_id: run, seat, title: title.trim(), approval_required: type1 })
    if (out) {
      setTitle('')
      setType1(false)
    }
  }

  return jsxs('div', {
    children: [
      jsxs('div', {
        style: { display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '16px' },
        children: [
          jsx('input', {
            value: title,
            onChange: (e) => setTitle(e.target.value),
            placeholder: 'New task title…',
            style: { flex: '1 1 220px', padding: '6px 10px', border: '1px solid var(--ui-stroke-secondary)', background: 'transparent', color: 'var(--ui-text-primary)' }
          }),
          jsx('select', {
            value: seat,
            onChange: (e) => setSeat(e.target.value),
            style: { padding: '6px 10px', border: '1px solid var(--ui-stroke-secondary)', background: 'transparent', color: 'var(--ui-text-primary)' },
            children: [
              jsx('option', { value: '', key: 'none', children: 'Seat…' }),
              ...seats.map((s) => jsx('option', { value: s.seat, key: s.seat, children: `${s.display_name || s.seat} (${s.seat})` }))
            ]
          }),
          jsx('select', {
            value: run,
            onChange: (e) => setRun(e.target.value),
            style: { padding: '6px 10px', border: '1px solid var(--ui-stroke-secondary)', background: 'transparent', color: 'var(--ui-text-primary)' },
            children: [
              jsx('option', { value: '', key: 'none', children: 'Run…' }),
              ...runList.map((r) => jsx('option', { value: runId(r), key: runId(r), children: runId(r) }))
            ]
          }),
          jsx('label', {
            style: { display: 'inline-flex', gap: '6px', alignItems: 'center', cursor: 'pointer' },
            children: [
              jsx('input', { type: 'checkbox', checked: type1, onChange: (e) => setType1(e.target.checked) }),
              jsx('span', { children: 'Type 1 (needs owner approval)' })
            ]
          }),
          jsx('button', {
            type: 'button', disabled: busy || !canCreate, onClick: create,
            style: { padding: '6px 14px', border: '1px solid var(--ui-accent)', background: 'transparent', color: 'var(--ui-accent)', cursor: 'pointer' },
            children: 'Create task'
          })
        ]
      }),
      jsxs('div', {
        style: { display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '16px' },
        children: [
          jsx('select', {
            value: seatFilter,
            onChange: (e) => setSeatFilter(e.target.value),
            style: { padding: '6px 10px', border: '1px solid var(--ui-stroke-secondary)', background: 'transparent', color: 'var(--ui-text-primary)' },
            children: [
              jsx('option', { value: '', key: 'all-seats', children: 'All seats' }),
              ...seats.map((s) => jsx('option', { value: s.seat, key: s.seat, children: s.seat }))
            ]
          }),
          jsx('select', {
            value: statusFilter,
            onChange: (e) => setStatusFilter(e.target.value),
            style: { padding: '6px 10px', border: '1px solid var(--ui-stroke-secondary)', background: 'transparent', color: 'var(--ui-text-primary)' },
            children: [
              jsx('option', { value: '', key: 'all-status', children: 'All statuses' }),
              ...STATUS_ORDER.map((s) => jsx('option', { value: s, key: s, children: statusLabel(s) }))
            ]
          }),
          jsx('input', {
            value: search,
            onChange: (e) => setSearch(e.target.value),
            placeholder: 'Search tasks…',
            style: { flex: '1 1 160px', padding: '6px 10px', border: '1px solid var(--ui-stroke-secondary)', background: 'transparent', color: 'var(--ui-text-primary)' }
          }),
          filtered.length !== list.length ? jsx('span', { style: { alignSelf: 'center', color: 'var(--ui-text-secondary)', fontSize: '0.85rem' }, children: `Showing ${filtered.length} of ${list.length}` }) : null
        ]
      }),
      STATUS_ORDER.map((status) => {
        const group = groups[status] || []
        if (group.length === 0) return null
        return jsxs('div', {
          key: status,
          style: { marginBottom: '16px' },
          children: [
            jsx('h3', { children: `${statusLabel(status)} (${group.length})` }),
            jsx('div', {
              style: { display: 'flex', flexDirection: 'column', gap: '8px' },
              children: group.map((t) => jsx(TaskCard, { task: t, writes, busy, key: taskId(t) }))
            })
          ]
        })
      })
    ]
  })
}

function ApprovalsPanel({ approvals, writes, busy }) {
  if (!approvals) return jsx('div', { children: 'No approvals yet.' })
  const pending = approvals.pending || []
  const history = approvals.history || []
  return jsxs('div', {
    children: [
      jsx('h3', { children: `Pending owner approval (${approvals.pending_count ?? pending.length})` }),
      pending.length === 0
        ? jsx('p', { style: { color: 'var(--ui-text-secondary)' }, children: 'Nothing waiting on the owner.' })
        : jsx('div', {
          style: { display: 'flex', flexDirection: 'column', gap: '8px' },
          children: pending.map((t) => jsx(TaskCard, { task: t, writes, busy, key: taskId(t) }))
        }),
      jsx('h3', { children: 'Approval history' }),
      history.length === 0
        ? jsx('p', { style: { color: 'var(--ui-text-secondary)' }, children: 'No approvals granted yet.' })
        : jsx('div', {
          style: { display: 'flex', flexDirection: 'column', gap: '4px' },
          children: history.map((h, i) => jsx('div', {
            key: i,
            style: { borderBottom: '1px solid var(--ui-stroke-secondary)', padding: '6px 0' },
            children: `${h.title || h.task_title || 'Task'} approved · by ${h.actor || 'Owner'} · ${relTime(h.at)}`
          }))
        }),
      approvals.decisions_note ? jsx('p', { style: { color: 'var(--ui-text-secondary)', marginTop: '12px' }, children: approvals.decisions_note }) : null
    ]
  })
}

export default {
  id: ID, // must match the folder name
  name: 'CEO Pack',
  register(ctx) {
    rest = ctx.rest
    ctx.onDispose(() => { rest = null })

    ctx.register({
      id: 'page',
      area: ROUTES_AREA,
      data: { path: '/ceo-pack' },
      render: () => jsx(CeoPackPage, {})
    })
    ctx.register({
      id: 'nav',
      area: SIDEBAR_NAV_AREA,
      order: 60,
      data: { path: '/ceo-pack', label: 'CEO Pack', codicon: 'organization' }
    })
    ctx.register({
      id: 'open',
      area: PALETTE_AREA,
      data: {
        id: 'ceo-pack.open',
        label: 'Open CEO Pack',
        keywords: ['ceo', 'ceo pack', 'dashboard', 'executive', 'tasks'],
        run: () => host.navigate('/ceo-pack')
      }
    })
  }
}
