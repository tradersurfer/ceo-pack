/**
 * CEO Pack — Hermes Desktop half (v2.4: futuristic pass, Nov 20 —
 * page-scoped dark command-center palette + Live Agent Activity panel.
 *
 * Adrian's call (Nov 20, from his screenshots): the web tab had the Live
 * Agent Activity panel and the dark look, Desktop had neither — and the
 * page looked washed out because the Desktop app runs light. The v2.3
 * reskin used the app's theme vars, so in light mode the page stayed
 * light. v2.4 paints the page itself: a fixed deep-navy / teal / gold
 * palette (PAL below), independent of the app theme. The Hermes shell
 * (sidebar, top bar) stays host-themed; only this page goes dark. All
 * data, writes, polling, and flash behavior are unchanged — presentation
 * only. No --ui-* theme vars remain in this file by design.
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
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'ceo-pack'
const POLL_MS = 30000
const LIVE_WINDOW_MS = 2 * 60 * 1000
const QUERY_ROOT = ['ceo-pack']

// CEO Pack command-center palette — page-scoped, intentionally fixed
// (see the header note). Gold = CEO identity + primary actions, teal =
// live/operational, green = live/verified, red = failure/destructive,
// amber = pending attention, slate = muted text. All 6-digit hex so glow
// shadows can append an alpha suffix (e.g. PAL.green + '55').
const PAL = {
  bg0: '#060b16',
  bg1: '#0a1424',
  panel: '#0e1b30',
  panel2: '#132642',
  line: 'rgba(45, 212, 191, 0.25)',
  lineGold: 'rgba(232, 182, 76, 0.5)',
  text: '#ece9db',
  muted: '#93a5ba',
  gold: '#e8b64c',
  goldDeep: '#c98f2a',
  goldText: '#1a1206',
  teal: '#2dd4bf',
  green: '#4ade80',
  red: '#fb7185',
  amber: '#fbbf24'
}

// Change flash (web parity): module-level snapshot of what we last saw,
// so rows that move between 30s polls flash once. First load only primes.
const lastSeen = { tasks: {}, history: {} }
let lastSeenPrimed = false

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

function statusLabel(s) {
  return s === 'blocked' || s === 'failed' ? 'Blocked / failed' : s.charAt(0).toUpperCase() + s.slice(1)
}

// Status pill colors (spec §6, on the PAL palette): proposed muted,
// approved gold, assigned teal, running green, blocked amber, failed red,
// completed/verified green, rejected muted. The label text is always
// present — color never carries status alone.
const STATUS_COLORS = {
  proposed: PAL.muted,
  approved: PAL.gold,
  assigned: PAL.teal,
  running: PAL.green,
  completed: PAL.green,
  verified: PAL.green,
  blocked: PAL.amber,
  failed: PAL.red,
  rejected: PAL.muted
}

function StatusPill({ status }) {
  const color = STATUS_COLORS[status] || PAL.muted
  return jsx('span', {
    style: { border: `1px solid ${color}`, color, padding: '1px 8px', fontSize: '0.8rem', fontWeight: 700, whiteSpace: 'nowrap', borderRadius: '999px', background: 'rgba(6, 11, 22, 0.4)' },
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

function StatBox({ value, label, color, icon }) {
  const accent = color || PAL.muted
  return jsxs('div', {
    style: { border: `1px solid ${PAL.line}`, background: PAL.panel, minWidth: '110px', borderRadius: '6px', overflow: 'hidden', boxShadow: `0 0 18px ${accent}22` },
    children: [
      jsx('div', { style: { height: '3px', background: `linear-gradient(90deg, ${accent}, transparent 75%)` }, children: '' }),
      jsxs('div', {
        style: { padding: '10px 16px 12px', display: 'flex', flexDirection: 'column', gap: '2px' },
        children: [
          icon ? jsx('div', { style: { fontSize: '1rem', color: accent }, children: icon }) : null,
          jsx('div', { style: { fontSize: '1.5rem', fontWeight: 700, color: PAL.text }, children: String(value) }),
          jsx('div', { style: { color: PAL.muted, fontSize: '0.85rem' }, children: label })
        ]
      })
    ]
  })
}

function SummaryStrip({ summary }) {
  if (!summary) return null
  const byStatus = summary.tasks_by_status || {}
  return jsxs('div', {
    style: { display: 'flex', gap: '12px', flexWrap: 'wrap', marginBottom: '16px' },
    children: [
      jsx(StatBox, { value: summary.tasks_total ?? 0, label: 'Tasks', color: PAL.gold, icon: '▤' }),
      jsx(StatBox, { value: summary.running ?? 0, label: 'Running', color: PAL.teal, icon: '▶' }),
      jsx(StatBox, { value: summary.stuck ?? 0, label: 'Blocked / failed', color: PAL.red, icon: '⚠' }),
      jsx(StatBox, { value: summary.pending_approvals ?? 0, label: 'Pending approval', color: PAL.gold, icon: '◷' }),
      jsx(StatBox, { value: byStatus.verified ?? 0, label: 'Verified', color: PAL.green, icon: '✓' }),
      jsx(StatBox, { value: summary.total_runs ?? 0, label: 'Runs', color: PAL.teal, icon: '▸' })
    ]
  })
}

function TabButton({ id, label, active, onSelect }) {
  return jsx('button', {
    type: 'button',
    onClick: () => onSelect(id),
    style: {
      padding: '6px 14px',
      border: `1px solid ${active ? PAL.gold : PAL.line}`,
      background: active ? `linear-gradient(90deg, ${PAL.gold}, ${PAL.goldDeep})` : 'transparent',
      color: active ? PAL.goldText : PAL.text,
      fontWeight: active ? 700 : 600,
      cursor: 'pointer',
      borderRadius: '4px'
    },
    children: label
  })
}

function CeoPackPage() {
  const [tab, setTab] = useState('org')
  const [selectedSeat, setSelectedSeat] = useState(null)
  const [writeBusy, setWriteBusy] = useState(false)
  const [writeError, setWriteError] = useState('')
  const [changed, setChanged] = useState({ tasks: {}, history: {} })
  const data = useCeoData()
  const loading = data.summary.isLoading || data.roster.isLoading
  const loadError = data.summary.error || data.roster.error || data.tasks.error || data.approvals.error

  // Change flash (web parity): diff each poll against the module-level
  // snapshot; just-changed rows flash once for 5s. First load primes.
  useEffect(() => {
    const tasksData = data.tasks.data
    const activityData = data.activity.data
    if (!tasksData && !activityData) return undefined
    const fresh = { tasks: {}, history: {} }
    if (lastSeenPrimed) {
      ;(tasksData?.tasks || []).forEach((t) => {
        const prev = lastSeen.tasks[taskId(t)]
        if (!prev || prev.status !== t.status || prev.updated_at !== t.updated_at) fresh.tasks[taskId(t)] = true
      })
      ;(activityData?.recent_history || []).forEach((ev) => {
        if (!lastSeen.history[ev.id]) fresh.history[ev.id] = true
      })
    }
    const seenTasks = {}
    ;(tasksData?.tasks || []).forEach((t) => { seenTasks[taskId(t)] = { status: t.status, updated_at: t.updated_at } })
    const seenHistory = {}
    ;(activityData?.recent_history || []).forEach((ev) => { seenHistory[ev.id] = true })
    lastSeen.tasks = seenTasks
    lastSeen.history = seenHistory
    lastSeenPrimed = true
    setChanged(fresh)
    const timer = setTimeout(() => setChanged({ tasks: {}, history: {} }), 5000)
    return () => clearTimeout(timer)
  }, [data.tasks.data, data.activity.data])

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
    return jsx('div', { style: { padding: '24px', background: PAL.bg0, color: PAL.text, height: '100%' }, children: 'Loading CEO Pack…' })
  }

  const generatedAt = data.summary.data?.generated_at
  const updatedText = generatedAt ? `Updated ${relTime(generatedAt)}` : ''
  return jsxs('div', {
    style: { padding: '20px 24px', height: '100%', overflowY: 'auto', background: `linear-gradient(180deg, ${PAL.bg1} 0%, ${PAL.bg0} 65%)`, color: PAL.text, colorScheme: 'dark' },
    children: [
      jsxs('div', {
        style: { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '16px', flexWrap: 'wrap', marginBottom: '12px' },
        children: [
          jsxs('div', {
            style: { display: 'flex', gap: '12px', alignItems: 'flex-start' },
            children: [
              jsx('span', { style: { fontSize: '30px', lineHeight: 1, color: PAL.gold, textShadow: '0 0 18px rgba(232, 182, 76, 0.5)' }, children: '♛' }),
              jsxs('div', {
                children: [
                  jsx('h1', { style: { fontSize: '1.4rem', margin: 0, color: PAL.gold, textShadow: '0 0 18px rgba(232, 182, 76, 0.45)' }, children: 'CEO Pack' }),
                  jsx('div', { style: { fontSize: '1rem', fontWeight: 600, color: PAL.text }, children: 'Executive Workspace' }),
                  jsx('p', { style: { color: PAL.muted, margin: '4px 0 0' }, children: 'Your AI C-suite at a glance. Every number below is a live read of the task registry — nothing is invented.' })
                ]
              })
            ]
          }),
          jsxs('div', {
            style: { display: 'flex', alignItems: 'center', gap: '12px' },
            children: [
              jsx('button', {
                type: 'button', disabled: writeBusy, onClick: () => { refreshAll() },
                style: { padding: '6px 14px', border: `1px solid ${PAL.line}`, background: 'transparent', color: PAL.text, cursor: 'pointer', borderRadius: '4px' },
                children: 'Refresh'
              }),
              updatedText ? jsx('span', { style: { color: PAL.muted, fontSize: '0.85rem', whiteSpace: 'nowrap' }, children: updatedText }) : null
            ]
          })
        ]
      }),
      jsx(SummaryStrip, { summary: data.summary.data }),
      loadError ? jsx('div', {
        style: { border: `1px solid ${PAL.red}`, background: 'rgba(251, 113, 133, 0.08)', color: PAL.text, padding: '12px', marginBottom: '12px', borderRadius: '6px' },
        children: `Couldn't reach the registry: ${loadError.message || loadError}`
      }) : null,
      writeError ? jsx('div', {
        style: { border: `1px solid ${PAL.red}`, background: 'rgba(251, 113, 133, 0.08)', color: PAL.text, padding: '12px', marginBottom: '12px', borderRadius: '6px' },
        children: `Write failed: ${writeError}`
      }) : null,
      jsxs('div', {
        style: { display: 'flex', gap: '8px', marginBottom: '16px', flexWrap: 'wrap' },
        children: TAB_LABELS.map(([id, label]) => jsx(TabButton, { id, label, active: tab === id, onSelect: setTab, key: id }))
      }),
      tab === 'org' ? jsx(OrgChart, { roster: data.roster.data, tasks: data.tasks.data, selectedSeat, onSelectSeat: setSelectedSeat, changed }) : null,
      tab === 'activity' ? jsx(ActivityPanel, { activity: data.activity.data, changed }) : null,
      tab === 'board' ? jsx(TaskBoard, { tasks: data.tasks.data, roster: data.roster.data, runs: data.runs.data, writes, busy: writeBusy, changed }) : null,
      tab === 'approvals' ? jsx(ApprovalsPanel, { approvals: data.approvals.data, writes, busy: writeBusy, changed }) : null
    ]
  })
}

function SeatAvatar({ node }) {
  if (node.avatar_url) {
    return jsx('img', { src: node.avatar_url, alt: node.display_name || node.seat, style: { width: '44px', height: '44px', borderRadius: '50%', objectFit: 'cover', margin: '0 auto 6px', display: 'block', border: `2px solid ${PAL.line}` } })
  }
  return jsx('span', { style: { width: '44px', height: '44px', borderRadius: '50%', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', fontWeight: 700, fontSize: '16px', background: 'rgba(45, 212, 191, 0.18)', color: PAL.teal, margin: '0 auto 6px' }, children: node.initials || '?' })
}

function SeatCard({ node, selected, onSelect }) {
  const live = isLive(node.last_activity)
  const gaps = (node.missing || []).length
  return jsxs('button', {
    type: 'button',
    onClick: () => onSelect(node.seat),
    style: {
      border: `1px solid ${selected ? PAL.gold : PAL.line}`,
      background: selected ? PAL.panel2 : PAL.panel,
      boxShadow: selected ? '0 0 18px rgba(232, 182, 76, 0.25)' : undefined,
      color: PAL.text,
      padding: '14px',
      minWidth: '150px',
      cursor: 'pointer',
      textAlign: 'center',
      borderRadius: '6px'
    },
    children: [
      jsx(SeatAvatar, { node }),
      jsxs('div', {
        style: { display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '6px' },
        children: [
          jsx('span', {
            className: live ? 'animate-pulse' : undefined,
            style: {
              width: '12px', height: '12px', borderRadius: '50%',
              background: live ? PAL.green : 'rgba(147, 165, 186, 0.4)',
              boxShadow: live ? '0 0 10px 2px rgba(74, 222, 128, 0.55)' : undefined,
              display: 'inline-block'
            },
            children: ''
          }),
          jsx('span', { style: { fontWeight: 700 }, children: node.display_name || node.seat }),
          live ? jsx('span', { style: { color: PAL.green, fontWeight: 700, fontSize: '0.8rem' }, children: '● LIVE' }) : null
        ]
      }),
      jsx('div', { style: { color: PAL.muted, fontSize: '0.85rem', marginTop: '4px' }, children: node.seat }),
      jsx('div', { style: { color: PAL.muted, fontSize: '0.85rem' }, children: `Last active: ${relTime(node.last_activity)}` }),
      jsx('div', { style: { color: PAL.muted, fontSize: '0.85rem' }, children: node.profile ? node.profile : 'Reference agent' }),
      gaps > 0 ? jsx('div', { style: { marginTop: '6px', fontSize: '0.8rem', color: PAL.muted }, children: `${gaps} CAPABILITY GAP${gaps === 1 ? '' : 'S'}` }) : null
    ]
  })
}

// Live Agent Activity (Desktop parity with the web tab — v2.3 shipped this
// panel web-only; the Desktop org tab had no equivalent). Same data the
// web panel reads: roster seats sorted by last_activity desc, live dot +
// "Last active: X" only — no invented "working on" lines.
function LiveActivityPanel({ roster }) {
  const seats = [...(roster?.seats || [])].sort((a, b) => {
    const ta = a.last_activity ? new Date(a.last_activity).getTime() : -1
    const tb = b.last_activity ? new Date(b.last_activity).getTime() : -1
    return tb - ta
  })
  const anyLive = seats.some((s) => isLive(s.last_activity))
  return jsxs('div', {
    style: { border: `1px solid ${PAL.line}`, background: PAL.panel, borderRadius: '6px', padding: '12px 14px', width: '300px', flexShrink: 0, alignSelf: 'flex-start' },
    children: [
      jsxs('div', {
        style: { display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px' },
        children: [
          jsx('h3', { style: { margin: 0, fontSize: '0.8rem', letterSpacing: '0.08em', textTransform: 'uppercase', color: PAL.text }, children: 'Live Agent Activity' }),
          anyLive ? jsx('span', { className: 'animate-pulse', style: { width: '8px', height: '8px', borderRadius: '50%', background: PAL.green, boxShadow: '0 0 10px 2px rgba(74, 222, 128, 0.55)', display: 'inline-block' }, children: '' }) : null
        ]
      }),
      seats.length === 0
        ? jsx('p', { style: { color: PAL.muted, margin: '8px 0 0' }, children: 'No seats yet.' })
        : jsx('div', {
          children: seats.map((s) => jsxs('div', {
            key: s.seat,
            style: { display: 'flex', alignItems: 'center', gap: '10px', padding: '8px 0', borderBottom: `1px solid ${PAL.line}` },
            children: [
              jsx('span', {
                className: isLive(s.last_activity) ? 'animate-pulse' : undefined,
                style: {
                  width: '10px', height: '10px', borderRadius: '50%',
                  background: isLive(s.last_activity) ? PAL.green : 'rgba(147, 165, 186, 0.4)',
                  boxShadow: isLive(s.last_activity) ? '0 0 10px 2px rgba(74, 222, 128, 0.55)' : undefined,
                  display: 'inline-block', flex: 'none'
                },
                children: ''
              }),
              jsx('span', { style: { width: '28px', height: '28px', borderRadius: '50%', background: 'rgba(45, 212, 191, 0.18)', color: PAL.teal, display: 'inline-flex', alignItems: 'center', justifyContent: 'center', fontWeight: 700, fontSize: '11px', flex: 'none' }, children: s.initials || '?' }),
              jsxs('div', {
                style: { minWidth: 0, flex: 1 },
                children: [
                  jsx('div', { style: { fontWeight: 600, color: PAL.text }, children: s.display_name || s.seat }),
                  jsx('div', { style: { color: PAL.muted, fontSize: '0.8rem' }, children: `${s.seat} · Last active: ${relTime(s.last_activity)}` })
                ]
              })
            ]
          }))
        })
    ]
  })
}

function OrgChart({ roster, tasks, selectedSeat, onSelectSeat, changed }) {
  if (!roster) return jsx('div', { style: { color: PAL.text }, children: 'No roster yet.' })
  const seats = roster.seats || []
  const head = seats.find((s) => s.seat === roster.head_seat) || seats[0]
  const restSeats = seats.filter((s) => s !== head)
  const seatTasks = selectedSeat && tasks
    ? (tasks.tasks || []).filter((t) => t.seat === selectedSeat)
    : []
  return jsxs('div', {
    children: [
      jsx('p', { style: { color: PAL.muted }, children: 'Reporting shown by the plugin’s accountability model — the head seat holds priorities. Departments and custom reporting lines plug in here when they exist; none are shown until they do. Roster loaded from the applied install config.' }),
      jsxs('div', {
        style: { display: 'flex', gap: '16px', alignItems: 'flex-start', flexWrap: 'wrap' },
        children: [
          jsxs('div', {
            style: { flex: '1 1 420px', minWidth: 0 },
            children: [
              head ? jsx('div', { style: { display: 'flex', justifyContent: 'center', marginBottom: '12px' }, children: jsx(SeatCard, { node: head, selected: selectedSeat === head.seat, onSelect: onSelectSeat }) }) : null,
              restSeats.length ? jsx('div', { style: { width: '1px', height: '16px', background: PAL.line, margin: '0 auto 12px' }, children: '' }) : null,
              jsxs('div', {
                style: { display: 'flex', gap: '12px', flexWrap: 'wrap', justifyContent: 'center' },
                children: restSeats.map((s) => jsx(SeatCard, { node: s, selected: selectedSeat === s.seat, onSelect: onSelectSeat, key: s.seat }))
              })
            ]
          }),
          jsx(LiveActivityPanel, { roster })
        ]
      }),
      selectedSeat ? jsxs('div', {
        style: { marginTop: '20px' },
        children: [
          jsxs('div', {
            style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center' },
            children: [
              jsx('h3', { style: { margin: 0, color: PAL.text }, children: `Assignments: ${selectedSeat} (${seatTasks.length})` }),
              jsx('button', {
                type: 'button',
                onClick: () => onSelectSeat(null),
                style: { padding: '4px 10px', border: `1px solid ${PAL.line}`, background: 'transparent', color: PAL.text, cursor: 'pointer', borderRadius: '4px' },
                children: 'Clear seat filter'
              })
            ]
          }),
          seatTasks.length === 0
            ? jsx('p', { style: { color: PAL.muted }, children: 'No tasks for this seat yet.' })
            : jsx('div', {
              style: { display: 'flex', flexDirection: 'column', gap: '8px', marginTop: '8px' },
              children: seatTasks.map((t) => jsx(TaskRow, { task: t, key: taskId(t), changed: changed?.tasks?.[taskId(t)] }))
            })
        ]
      }) : null
    ]
  })
}

function ActivityPanel({ activity, changed }) {
  if (!activity) return jsx('div', { style: { color: PAL.text }, children: 'No activity yet.' })
  const history = activity.recent_history || []
  return jsxs('div', {
    children: [
      jsxs('div', {
        style: { display: 'flex', gap: '12px', flexWrap: 'wrap', marginBottom: '12px' },
        children: [
          jsxs('div', {
            style: { border: `1px solid ${PAL.line}`, background: PAL.panel, borderRadius: '6px', padding: '12px 14px', flex: '1 1 220px' },
            children: [
              jsx('h3', { style: { margin: '0 0 8px', color: PAL.text }, children: `Running now (${(activity.running || []).length})` }),
              (activity.running || []).length === 0
                ? jsx('p', { style: { color: PAL.muted }, children: 'Nothing running right now.' })
                : jsx('div', {
                  style: { display: 'flex', flexDirection: 'column', gap: '8px' },
                  children: (activity.running || []).map((t) => jsx(TaskRow, { task: t, key: taskId(t), changed: changed?.tasks?.[taskId(t)] }))
                })
            ]
          }),
          jsxs('div', {
            style: { border: `1px solid ${PAL.line}`, background: PAL.panel, borderRadius: '6px', padding: '12px 14px', flex: '1 1 220px' },
            children: [
              jsx('h3', { style: { margin: '0 0 8px', color: PAL.text }, children: `Blocked / stuck (${(activity.stuck || []).length})` }),
              (activity.stuck || []).length === 0
                ? jsx('p', { style: { color: PAL.muted }, children: 'Nothing blocked or stuck.' })
                : jsx('div', {
                  style: { display: 'flex', flexDirection: 'column', gap: '8px' },
                  children: (activity.stuck || []).map((t) => jsx(TaskRow, { task: t, key: taskId(t), changed: changed?.tasks?.[taskId(t)] }))
                })
            ]
          })
        ]
      }),
      jsx('h3', { style: { color: PAL.text }, children: 'Recent execution history' }),
      history.length === 0
        ? jsx('p', { style: { color: PAL.muted }, children: 'No history yet.' })
        : jsx('div', {
          style: { display: 'flex', flexDirection: 'column', gap: '4px', borderLeft: `1px solid ${PAL.teal}`, paddingLeft: '16px' },
          children: history.map((h, i) => jsx('div', {
            key: i,
            style: {
              borderBottom: `1px solid ${PAL.line}`, padding: '6px 0',
              borderColor: changed?.history?.[h.id] ? PAL.teal : undefined,
              background: changed?.history?.[h.id] ? PAL.panel2 : undefined,
              color: PAL.text
            },
            children: `${h.title || h.task_title || 'Task'}: ${h.from_status || '—'} → ${h.to_status || '—'} · ${h.actor || 'system'} · ${relTime(h.at)}${h.note ? ` — ${h.note}` : ''}`
          }))
        })
    ]
  })
}

function TaskRow({ task, changed }) {
  return jsxs('div', {
    style: {
      border: `1px solid ${changed ? PAL.teal : PAL.line}`,
      background: changed ? PAL.panel2 : PAL.panel,
      padding: '10px 12px',
      borderRadius: '6px',
      color: PAL.text
    },
    children: [
      jsx('div', { style: { fontWeight: 600 }, children: task.title }),
      jsxs('div', {
        style: { display: 'flex', gap: '8px', marginTop: '4px', fontSize: '0.85rem', color: PAL.muted, alignItems: 'center' },
        children: [
          jsx(StatusPill, { status: task.status }),
          task.approval_required ? jsx('span', { style: { color: PAL.gold }, children: 'TYPE 1' }) : null,
          jsx('span', { children: `${task.seat} · updated ${relTime(task.updated_at)}` })
        ]
      })
    ]
  })
}

function TaskCard({ task, writes, busy, changed, showPending }) {
  const [assignee, setAssignee] = useState(task.seat || '')
  const [open, setOpen] = useState(false)
  const canApprove = task.status === 'proposed' && task.approval_required
  const canAssign = task.status === 'approved' || task.status === 'proposed'
  const exec = task.execution || null
  return jsxs('div', {
    style: {
      border: `1px solid ${changed ? PAL.teal : PAL.line}`,
      background: changed ? PAL.panel2 : PAL.panel,
      padding: '10px 12px',
      borderRadius: '6px',
      color: PAL.text
    },
    children: [
      showPending ? jsx('span', { style: { border: `1px solid ${PAL.gold}`, color: PAL.gold, padding: '1px 8px', fontSize: '0.8rem', fontWeight: 700, whiteSpace: 'nowrap', borderRadius: '999px', background: 'rgba(232, 182, 76, 0.12)' }, children: 'PENDING' }) : null,
      jsx('div', {
        onClick: () => setOpen(!open),
        style: { fontWeight: 600, cursor: 'pointer', color: PAL.text },
        children: `${open ? '▾' : '▸'} ${task.title}`
      }),
      jsxs('div', {
        style: { display: 'flex', gap: '8px', marginTop: '4px', fontSize: '0.85rem', color: PAL.muted, alignItems: 'center' },
        children: [
          jsx(StatusPill, { status: task.status }),
          task.approval_required ? jsx('span', { style: { color: PAL.gold }, children: 'TYPE 1' }) : null,
          jsx('span', { children: `${task.seat} · updated ${relTime(task.updated_at)}` })
        ]
      }),
      open ? jsxs('div', {
        style: { marginTop: '8px', fontSize: '0.85rem', color: PAL.muted, display: 'flex', flexDirection: 'column', gap: '2px' },
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
            style: { padding: '4px 12px', border: `1px solid ${PAL.gold}`, background: `linear-gradient(90deg, ${PAL.gold}, ${PAL.goldDeep})`, color: PAL.goldText, fontWeight: 700, cursor: 'pointer', borderRadius: '4px' },
            children: 'Approve'
          }) : null,
          canApprove ? jsx('button', {
            type: 'button', disabled: busy,
            onClick: () => writes.reject(taskId(task)),
            style: { padding: '4px 12px', border: `1px solid ${PAL.red}`, background: 'transparent', color: PAL.red, cursor: 'pointer', borderRadius: '4px' },
            children: 'Reject'
          }) : null,
          canAssign ? jsxs('span', {
            style: { display: 'inline-flex', gap: '6px', alignItems: 'center' },
            children: [
              jsx('input', {
                value: assignee,
                onChange: (e) => setAssignee(e.target.value),
                placeholder: 'Assign to…',
                style: { padding: '4px 8px', border: `1px solid ${PAL.line}`, background: PAL.bg1, color: PAL.text }
              }),
              jsx('button', {
                type: 'button', disabled: busy || !assignee.trim(),
                onClick: () => writes.assign(taskId(task), assignee.trim()),
                style: { padding: '4px 12px', border: `1px solid ${PAL.line}`, background: 'transparent', color: PAL.text, cursor: 'pointer', borderRadius: '4px' },
                children: 'Assign'
              })
            ]
          }) : null
        ]
      })
    ]
  })
}

function TaskBoard({ tasks, roster, runs, writes, busy, changed }) {
  const [title, setTitle] = useState('')
  const [seat, setSeat] = useState('')
  const [run, setRun] = useState('')
  const [type1, setType1] = useState(false)
  const [seatFilter, setSeatFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const [search, setSearch] = useState('')
  const list = (tasks && tasks.tasks) || []
  const seats = (roster && roster.seats) || []
  const runList = (runs && runs.runs) || (runs && runs.items) || []
  // Board chips map to real lifecycle states: In Progress covers assigned
  // + running; Done covers completed + verified. Blocked, failed, and
  // rejected tasks show under All only.
  const BOARD_FILTERS = {
    all: null,
    proposed: ['proposed'],
    approved: ['approved'],
    'in-progress': ['assigned', 'running'],
    done: ['completed', 'verified']
  }
  const CHIPS = [
    ['all', 'All'],
    ['proposed', 'Proposed'],
    ['approved', 'Approved'],
    ['in-progress', 'In Progress'],
    ['done', 'Done']
  ]
  const chipCount = (key) => {
    const allowed = BOARD_FILTERS[key]
    if (!allowed) return list.length
    return list.filter((t) => allowed.includes(t.status)).length
  }
  const filtered = list.filter((t) => {
    if (seatFilter && t.seat !== seatFilter) return false
    const allowed = BOARD_FILTERS[statusFilter]
    if (allowed && !allowed.includes(t.status)) return false
    if (search && !(t.title || '').toLowerCase().includes(search.toLowerCase())) return false
    return true
  })
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
        style: { border: `1px solid ${PAL.line}`, background: PAL.panel, borderRadius: '6px', padding: '12px 14px', marginBottom: '16px' },
        children: [
          jsx('h3', { style: { margin: '0 0 10px', color: PAL.text }, children: 'Create New Task' }),
          jsxs('div', {
            style: { display: 'flex', gap: '8px', flexWrap: 'wrap' },
            children: [
              jsx('input', {
                value: title,
                onChange: (e) => setTitle(e.target.value),
                placeholder: 'New task title…',
                style: { flex: '1 1 220px', padding: '6px 10px', border: `1px solid ${PAL.line}`, background: PAL.bg1, color: PAL.text }
              }),
              jsx('select', {
                value: seat,
                onChange: (e) => setSeat(e.target.value),
                style: { padding: '6px 10px', border: `1px solid ${PAL.line}`, background: PAL.bg1, color: PAL.text },
                children: [
                  jsx('option', { value: '', key: 'none', children: 'Seat…' }),
                  ...seats.map((s) => jsx('option', { value: s.seat, key: s.seat, children: `${s.display_name || s.seat} (${s.seat})` }))
                ]
              }),
              jsx('select', {
                value: run,
                onChange: (e) => setRun(e.target.value),
                style: { padding: '6px 10px', border: `1px solid ${PAL.line}`, background: PAL.bg1, color: PAL.text },
                children: [
                  jsx('option', { value: '', key: 'none', children: 'Run…' }),
                  ...runList.map((r) => jsx('option', { value: runId(r), key: runId(r), children: runId(r) }))
                ]
              }),
              jsx('label', {
                style: { display: 'inline-flex', gap: '6px', alignItems: 'center', cursor: 'pointer', color: PAL.text },
                children: [
                  jsx('input', { type: 'checkbox', checked: type1, onChange: (e) => setType1(e.target.checked) }),
                  jsx('span', { children: 'Type 1 (needs owner approval)' })
                ]
              }),
              jsx('button', {
                type: 'button', disabled: busy || !canCreate, onClick: create,
                style: { padding: '6px 14px', border: `1px solid ${PAL.gold}`, background: `linear-gradient(90deg, ${PAL.gold}, ${PAL.goldDeep})`, color: PAL.goldText, fontWeight: 700, cursor: 'pointer', borderRadius: '4px' },
                children: 'Create task'
              })
            ]
          })
        ]
      }),
      jsxs('div', {
        style: { display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '16px', alignItems: 'center' },
        children: [
          jsxs('div', {
            style: { display: 'flex', gap: '6px', flexWrap: 'wrap' },
            children: CHIPS.map(([id, label]) => jsx('button', {
              type: 'button', key: id,
              onClick: () => setStatusFilter(id),
              style: {
                padding: '6px 12px',
                border: `1px solid ${statusFilter === id ? PAL.gold : PAL.line}`,
                background: statusFilter === id ? `linear-gradient(90deg, ${PAL.gold}, ${PAL.goldDeep})` : 'transparent',
                color: statusFilter === id ? PAL.goldText : PAL.text,
                fontWeight: statusFilter === id ? 700 : 600,
                cursor: 'pointer',
                borderRadius: '4px'
              },
              children: `${label} (${chipCount(id)})`
            }))
          }),
          jsx('select', {
            value: seatFilter,
            onChange: (e) => setSeatFilter(e.target.value),
            style: { padding: '6px 10px', border: `1px solid ${PAL.line}`, background: PAL.bg1, color: PAL.text },
            children: [
              jsx('option', { value: '', key: 'all-seats', children: 'All seats' }),
              ...seats.map((s) => jsx('option', { value: s.seat, key: s.seat, children: s.seat }))
            ]
          }),
          jsx('input', {
            value: search,
            onChange: (e) => setSearch(e.target.value),
            placeholder: 'Search tasks…',
            style: { flex: '1 1 160px', padding: '6px 10px', border: `1px solid ${PAL.line}`, background: PAL.bg1, color: PAL.text }
          }),
          filtered.length !== list.length ? jsx('span', { style: { alignSelf: 'center', color: PAL.muted, fontSize: '0.85rem' }, children: `Showing ${filtered.length} of ${list.length}` }) : null
        ]
      }),
      list.length === 0
        ? jsx('p', { style: { color: PAL.muted }, children: 'No tasks in the registry yet.' })
        : filtered.length === 0
          ? jsx('p', { style: { color: PAL.muted }, children: 'No tasks in this view.' })
          : jsx('div', {
            style: { display: 'flex', flexDirection: 'column', gap: '8px' },
            children: filtered.map((t) => jsx(TaskCard, { task: t, writes, busy, key: taskId(t), changed: changed?.tasks?.[taskId(t)] }))
          })
    ]
  })
}

function ApprovalsPanel({ approvals, writes, busy, changed }) {
  if (!approvals) return jsx('div', { style: { color: PAL.text }, children: 'No approvals yet.' })
  const pending = approvals.pending || []
  const history = approvals.history || []
  return jsxs('div', {
    children: [
      jsx('h3', { style: { color: PAL.text }, children: `Pending owner approval (${approvals.pending_count ?? pending.length})` }),
      pending.length === 0
        ? jsx('p', { style: { color: PAL.muted }, children: 'No pending owner approvals.' })
        : jsx('div', {
          style: { display: 'flex', flexDirection: 'column', gap: '8px' },
          children: pending.map((t) => jsx(TaskCard, { task: t, writes, busy, key: taskId(t), changed: changed?.tasks?.[taskId(t)], showPending: true }))
        }),
      jsx('h3', { style: { color: PAL.text }, children: 'Approval history' }),
      history.length === 0
        ? jsx('p', { style: { color: PAL.muted }, children: 'No approval history yet.' })
        : jsx('div', {
          style: { display: 'flex', flexDirection: 'column', gap: '4px' },
          children: history.map((h, i) => jsx('div', {
            key: i,
            style: { borderBottom: `1px solid ${PAL.line}`, padding: '6px 0', color: PAL.text },
            children: `${h.title || h.task_title || 'Task'} approved · by ${h.actor || 'Owner'} · ${relTime(h.at)}`
          }))
        }),
      approvals.decisions_note ? jsx('p', { style: { color: PAL.muted, marginTop: '12px' }, children: approvals.decisions_note }) : null
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
