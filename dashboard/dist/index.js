/**
 * CEO Pack — Executive Workspace dashboard.
 *
 * Visual redesign (Chat's spec, Nov 20, 2026): futuristic command center —
 * crown header, six KPI cards with icons, org chart with a Live Agent
 * Activity side panel, activity panels, board filter chips, approvals
 * with PENDING badges. Same API contract, same writes, same live window;
 * presentation only. No fabricated data: no deltas, no Business Health
 * panel, no invented activity lines.
 *
 * Plain IIFE, no build step. Uses window.__HERMES_PLUGIN_SDK__ for React +
 * host components and SDK.fetchJSON for API calls (host auth handling);
 * registers via window.__HERMES_PLUGINS__.register("ceo-pack", ...).
 *
 * Writes: five POST routes (create / approve / reject / assign / complete) that call
 * the same tasks.py functions the chat tools call — the same lifecycle +
 * permission checks, the same registry, the same history audit. The host
 * authenticates every /api/ call, so the dashboard caller is an
 * authenticated Hermes user (the owner). Write buttons disable while their
 * request is in flight; a failed write shows the server's detail string
 * inline instead of failing silently. No write UI is rendered for actions
 * the registry forbids (Approve/Reject only on proposed Type 1 tasks,
 * Assign only on proposed/approved ones, Mark complete only on running ones).
 *
 * Runtime truth: a task's status is shown exactly as the registry holds
 * it. Assigned is not "in progress", completed is not "verified". Where a
 * timestamp is missing the UI says "unknown" — it never invents one, and
 * zero counts are shown only as real counts of an empty registry.
 *
 * Live window: the tab polls every endpoint every 30s. A seat whose last
 * registry activity is under 2 minutes old shows a live dot; a task whose
 * status changed, a seat whose activity moved, or a feed line that just
 * arrived flashes once between polls. There is no push channel — polling
 * is the mechanism, and "live" means "within the last poll".
 *
 * Chat bin (v1, web only): a docked panel beside the tab content on all 4
 * tabs — chat with a seat while watching the board. It talks to the host
 * session/chat endpoints directly through the SDK (SDK.fetchJSON for
 * GET /api/sessions, /api/sessions/{id}/messages, /api/chat/workspaces;
 * SDK.buildWsUrl for /api/ws, which mints a fresh single-use ticket per
 * connect) — zero new plugin backend routes. One shared socket, one
 * vendored NDJSON JSON-RPC client (CeoChatWire below); the session_id on
 * every event routes it to the right seat's conversation. Seat -> profile
 * comes from GET /roster; an unbound seat falls back to the main agent
 * wearing that seat's hat, stated in plain words. The gate indicator in
 * the panel header says whether this seat's profile has CEO Pack (learned
 * at runtime from session.info's tool map, never guessed from the
 * roster) — warn mode is never described as enforcement. V1 cuts:
 * reasoning streams, inline approval answering (notices link to Sessions
 * instead), voice/images/uploads, model picker, multi-session tabs, PTY.
 * Desktop gets a link-out to Sessions only (see desktop/plugin.js) —
 * ctx.rest/ctx.socket are scoped to the plugin's own backend, so Desktop
 * cannot drive the host chat wire.
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

  // Authenticated POST helper (kanban pattern): same session-token
  // handling as fetchJSON, standard RequestInit. A failed write throws
  // with the server's detail string, which the UI shows inline.
  function postJSON(path, body) {
    return fetchJSON(API + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  }

  // Live window: what we last saw, so a seat that just moved, a task whose
  // status just changed, or a feed line that just arrived can flash once
  // instead of silently re-rendering. A seat counts as "live" when its
  // last registry activity is under 2 minutes old. Module-level on
  // purpose — it must survive re-renders, and the SDK hooks exposed here
  // do not include a ref hook.
  var LIVE_WINDOW_MS = 2 * 60 * 1000;
  var lastSeen = { tasks: {}, seats: {}, history: {} };
  var lastSeenPrimed = false;

  function isLive(iso) {
    if (!iso) return false;
    var t = new Date(iso).getTime();
    return !isNaN(t) && (Date.now() - t) < LIVE_WINDOW_MS;
  }

const STATUS_LABEL = {
    proposed: "Proposed", approved: "Approved", assigned: "Assigned",
    running: "Running", blocked: "Blocked", completed: "Completed",
    verified: "Verified", failed: "Failed", rejected: "Rejected",
  };

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

// --- BEGIN CeoChatWire ---
// Vendored NDJSON JSON-RPC client for the host /api/ws sidecar. The plugin
// is a plain IIFE and cannot import @hermes/shared, so the minimal client
// lives here (~200 lines, no dependencies): request/response matching by
// id, event dispatch by type, heartbeat, bounded reconnect with gap replay.
//
// Wire facts (verified against ~/workspace/hermes-agent-work):
// - tui_gateway/ws.py: "newline-delimited JSON-RPC both ways". Outbound
//   frames are {"jsonrpc":"2.0","id","method","params"} + "\n"; inbound
//   text is split on "\n". A frame with an id is a response; a frame with
//   method "event" is a notification whose params ARE the GatewayEvent
//   ({type, session_id, seq, payload}); a frame with an id AND a method is
//   a server->client request, answered -32601 here (the bin never answers
//   approvals in v1, and the backend must not wait out its deadline).
// - Auth: the URL comes from SDK.buildWsUrl("/api/ws") — async, mints a
//   FRESH single-use ticket per connect in gated mode (TTL 30s). Never
//   hand-roll the URL, never read window.__HERMES_SESSION_TOKEN__.
// - Heartbeat: gateway.ping every 15s (DEFAULT_HEARTBEAT_INTERVAL_MS);
//   a missed ping drops the socket so the reconnect path runs.
// - Reconnect: bounded redial budget (5 attempts, 1s/2s/4s/8s/16s), then
//   "failed" with a manual Retry. Per-session seq watermarks survive the
//   drop (module-level lastSeq); on reconnect each known session replays
//   via session.events.since, and a truncated replay re-resumes.
// - Close codes: 4401 sign-in expired, 4403 not allowed / host-origin
//   mismatch, 4404 chat disabled (chat_ws.py) — mapped to plain words,
//   never shown as bare codes.
function createCeoChatWire(deps) {
  var WebSocketCtor = deps.WebSocketCtor;
  var buildWsUrl = deps.buildWsUrl;
  var onEvent = deps.onEvent || function () {};
  var onState = deps.onState || function () {};
  var onReplayGap = deps.onReplayGap || function () {};
  var setTimeoutFn = deps.setTimeout || setTimeout;
  var clearTimeoutFn = deps.clearTimeout || clearTimeout;
  var setIntervalFn = deps.setInterval || setInterval;
  var clearIntervalFn = deps.clearInterval || clearInterval;
  var requestTimeoutMs = deps.requestTimeoutMs || 120000;
  var heartbeatMs = deps.heartbeatMs == null ? 15000 : deps.heartbeatMs;
  var maxRetries = deps.maxRetries == null ? 5 : deps.maxRetries;
  var backoffBaseMs = deps.backoffBaseMs || 1000;

  var nextId = 0;
  var pending = {};
  var ws = null;
  var buf = "";
  var state = "closed";
  var manualClose = false;
  var retryCount = 0;
  var retryTimer = null;
  var heartbeatTimer = null;
  var lastSeq = {};
  var replayEpoch = null;
  var knownSessions = {};
  var lastBanner = null;

  function setState(s, detail) { state = s; onState(s, detail || null); }
  function stopHeartbeat() {
    if (heartbeatTimer) { clearIntervalFn(heartbeatTimer); heartbeatTimer = null; }
  }

  function sendFrame(frame) {
    if (!ws || ws.readyState !== 1) return false;
    try { ws.send(JSON.stringify(frame) + "\n"); return true; }
    catch (_e) { return false; }
  }

  function request(method, params, opts) {
    var timeoutMs = (opts && opts.timeoutMs) || requestTimeoutMs;
    return new Promise(function (resolve, reject) {
      if (!ws || ws.readyState !== 1) { reject(new Error("not connected")); return; }
      var id = "w" + (++nextId);
      var timer = setTimeoutFn(function () {
        if (pending[id]) { delete pending[id]; reject(new Error("request timed out: " + method)); }
      }, timeoutMs);
      pending[id] = { resolve: resolve, reject: reject, timer: timer };
      if (!sendFrame({ jsonrpc: "2.0", id: id, method: method, params: params || {} })) {
        clearTimeoutFn(timer); delete pending[id];
        reject(new Error("not connected"));
      }
    });
  }

  function handleLine(line) {
    var frame;
    try { frame = JSON.parse(line); } catch (_e) { return; }
    if (!frame || typeof frame !== "object") return;
    if (frame.method === "event" && frame.params && typeof frame.params.type === "string") {
      var ev = frame.params;
      if (ev.session_id && typeof ev.seq === "number") {
        lastSeq[ev.session_id] = Math.max(lastSeq[ev.session_id] || 0, ev.seq);
      }
      if (ev.type === "gateway.ready" && ev.payload && ev.payload.replay_epoch) replayEpoch = ev.payload.replay_epoch;
      if (ev.type === "session.replay_gap") { onReplayGap(ev.session_id || null); return; }
      onEvent(ev);
      return;
    }
    if (frame.id !== undefined && frame.id !== null) {
      if (frame.method && !frame.result && !frame.error) {
        sendFrame({ jsonrpc: "2.0", error: { code: -32601, message: "Method not found" }, id: frame.id });
        return;
      }
      var p = pending[frame.id];
      if (!p) return;
      delete pending[frame.id];
      clearTimeoutFn(p.timer);
      if (frame.error) p.reject(new Error((frame.error && frame.error.message) || "request failed"));
      else p.resolve(frame.result);
    }
  }

  function onMessage(data) {
    buf += data;
    var lines = buf.split("\n");
    buf = lines.pop();
    for (var i = 0; i < lines.length; i++) {
      if (lines[i]) handleLine(lines[i]);
    }
  }

  function startHeartbeat() {
    stopHeartbeat();
    if (!heartbeatMs) return;
    heartbeatTimer = setIntervalFn(function () {
      request("gateway.ping", {}, { timeoutMs: 10000 }).then(null, function () {
        try { if (ws) ws.close(); } catch (_e) {}
      });
    }, heartbeatMs);
  }

  function scheduleReconnect() {
    if (retryCount >= maxRetries) {
      setState("failed", { message: lastBanner || "Couldn't reach the gateway — check it's running, then reopen chat." });
      return;
    }
    var delay = backoffBaseMs * Math.pow(2, retryCount);
    retryCount += 1;
    setState("retrying", { attempt: retryCount });
    retryTimer = setTimeoutFn(function () { retryTimer = null; connect(); }, delay);
  }

  function failConnect() {
    if (manualClose) { setState("closed"); return; }
    scheduleReconnect();
  }

  function replayKnown() {
    Object.keys(knownSessions).forEach(function (sid) {
      var prof = knownSessions[sid].profile;
      var params = { session_id: sid, last_seen: lastSeq[sid] || 0 };
      if (prof) params.profile = prof;
      if (replayEpoch) params.replay_epoch = replayEpoch;
      request("session.events.since", params).then(function (res) {
        if (!res) return;
        if (res.truncated) { onReplayGap(sid); return; }
        (res.events || []).forEach(function (ev) {
          if (ev && typeof ev.type === "string") { ev.replayed = true; onEvent(ev); }
        });
        if (typeof res.latest_seq === "number") lastSeq[sid] = Math.max(lastSeq[sid] || 0, res.latest_seq);
      }, function () { /* replay is best-effort; a gap re-resumes instead */ });
    });
  }

  function connect() {
    if (state === "open" || state === "connecting") return;
    manualClose = false;
    if (retryTimer) { clearTimeoutFn(retryTimer); retryTimer = null; }
    setState("connecting");
    var p;
    try { p = buildWsUrl("/api/ws"); }
    catch (err) { failConnect(); return; }
    Promise.resolve(p).then(function (url) {
      if (manualClose) return;
      var socket;
      try { socket = new WebSocketCtor(url); }
      catch (err) { failConnect(); return; }
      ws = socket;
      socket.onopen = function () {
        retryCount = 0;
        lastBanner = null;
        setState("open");
        startHeartbeat();
        replayKnown();
      };
      socket.onmessage = function (ev) { onMessage(ev.data || ""); };
      socket.onerror = function () { /* onclose carries the outcome */ };
      socket.onclose = function (ev) {
        stopHeartbeat();
        ws = null;
        buf = "";
        var code = ev && ev.code;
        if (manualClose) { setState("closed"); return; }
        if (code === 4401) {
          lastBanner = "Your sign-in expired — reconnect";
          setState("failed", { code: 4401, message: lastBanner });
          return;
        }
        if (code === 4403) {
          lastBanner = "Chat isn't available right now.";
          setState("failed", { code: 4403, message: lastBanner });
          return;
        }
        if (code === 4404) {
          lastBanner = "Chat is disabled on this gateway.";
          setState("failed", { code: 4404, message: lastBanner });
          return;
        }
        scheduleReconnect();
      };
    }, function () { failConnect(); });
  }

  function close() {
    manualClose = true;
    if (retryTimer) { clearTimeoutFn(retryTimer); retryTimer = null; }
    stopHeartbeat();
    Object.keys(pending).forEach(function (id) {
      clearTimeoutFn(pending[id].timer);
      pending[id].reject(new Error("closed"));
    });
    pending = {};
    if (ws) { try { ws.close(); } catch (_e) {} ws = null; }
    setState("closed");
  }

  function manualRetry() { retryCount = 0; lastBanner = null; connect(); }
  function noteSession(sessionId, profile) { if (sessionId) knownSessions[sessionId] = { profile: profile || null }; }
  function forgetSession(sessionId) { delete knownSessions[sessionId]; delete lastSeq[sessionId]; }

  return {
    connect: connect, request: request, close: close, manualRetry: manualRetry,
    noteSession: noteSession, forgetSession: forgetSession,
    getState: function () { return state; },
    getLastSeq: function (sid) { return lastSeq[sid] || 0; },
  };
}
// --- END CeoChatWire ---

  // --- Chat bin state (web v1) ---------------------------------------------
  // Module-level on purpose: the panel stays mounted (hidden, not
  // unmounted) when collapsed, and the socket + conversations live for the
  // tab's lifetime. The React component mirrors this store into its render
  // via chatNotify(); the wire callbacks read the store at call time, so
  // there are no stale closures.
  var CHAT_SDK_MIN = "1.1.0";
  var CHAT_RECENT_FIRST = 5;
  var CHAT_RECENT_PAGE = 20;
  var CHAT_HISTORY_RENDER = 50;
  var CHAT_HISTORY_PAGE = 50;

  var chatStore = {
    client: null,
    seat: null,
    seats: [],
    headSeat: "CEO",
    convs: {},
    recents: {},
    sessionSeats: {},
    conn: "closed",
    banner: null,
    listeners: [],
  };
  var chatMsgSeq = 0;
  var chatBodyEl = null;
  var chatAtBottom = true;

  function chatNotify() {
    chatStore.listeners.slice().forEach(function (fn) { try { fn(); } catch (_e) {} });
  }

  function sdkAtLeast(v, min) {
    function parts(s) {
      return String(s).split(".").map(function (x) { var n = parseInt(x, 10); return isNaN(n) ? 0 : n; });
    }
    var a = parts(v), b = parts(min);
    for (var i = 0; i < Math.max(a.length, b.length); i++) {
      var x = a[i] || 0, y = b[i] || 0;
      if (x !== y) return x > y;
    }
    return true;
  }

  // The SDK is a spike (sdk.d.ts header) — pin the contract at load. If the
  // host predates buildWsUrl or the 1.1.0 contract, the panel says so
  // instead of crashing. No method beyond fetchJSON / buildWsUrl is used.
  function chatSdkOk() {
    if (!SDK || typeof SDK.buildWsUrl !== "function" || typeof SDK.fetchJSON !== "function") return false;
    if (typeof WebSocket !== "function") return false;
    if (typeof SDK.sdkVersion !== "string") return false;
    return sdkAtLeast(SDK.sdkVersion, CHAT_SDK_MIN);
  }

  function chatUuid() {
    try {
      if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
    } catch (_e) {}
    return "chat-" + Date.now().toString(36) + "-" + Math.floor(Math.random() * 1e9).toString(36);
  }

  function chatSeatNode(seat, seats) {
    var list = seats || chatStore.seats || [];
    for (var i = 0; i < list.length; i++) if (list[i].seat === seat) return list[i];
    return null;
  }

  function chatSeatDisplay(seat) {
    var node = chatSeatNode(seat);
    return (node && node.display_name) || seat;
  }

  function chatSeatProfile(seat, conv) {
    var node = chatSeatNode(seat);
    if (!node || !node.profile) return null;
    if (conv && conv.useFallback) return null;
    return node.profile;
  }

  function chatNewConv() {
    return {
      sessionId: null, title: null, model: null,
      hasCeoPack: null,
      messages: [],
      running: false, creating: false, loadingEarlier: false,
      stream: null,
      toolRows: {},
      subagentRows: {},
      approvalNoteId: null,
      loadedCount: 0, totalCount: 0, hasMore: false, earlierLoaded: 0,
      loadedRowIds: {},
      failedSend: null, goneRetried: false, useFallback: false,
    };
  }

  function chatConv(seat) {
    if (!chatStore.convs[seat]) chatStore.convs[seat] = chatNewConv();
    return chatStore.convs[seat];
  }

  function chatAddMsg(seat, msg) {
    var conv = chatConv(seat);
    msg.id = "m" + (++chatMsgSeq);
    conv.messages.push(msg);
    return msg;
  }

  function chatFindMsg(conv, id) {
    for (var i = 0; i < conv.messages.length; i++) if (conv.messages[i].id === id) return conv.messages[i];
    return null;
  }

  function chatRemoveMsg(conv, id) {
    conv.messages = conv.messages.filter(function (m) { return m.id !== id; });
  }

  // session.info's tool map (SessionLiveInfo.tools: Record<string, string[]>)
  // is the ONLY source for "does this seat have CEO Pack". Keys may be tool
  // names or groups, so both keys and values are checked for ceo_*. null =
  // the map hasn't arrived yet; false = the map arrived with no ceo_ tools.
  function chatHasCeoPack(info) {
    if (!info || !info.tools) return null;
    var tools = info.tools;
    var keys = Object.keys(tools);
    for (var i = 0; i < keys.length; i++) {
      if (keys[i].indexOf("ceo_") === 0) return true;
      var vals = tools[keys[i]];
      if (vals && vals.length) {
        for (var j = 0; j < vals.length; j++) {
          if (typeof vals[j] === "string" && vals[j].indexOf("ceo_") === 0) return true;
        }
      }
    }
    return false;
  }

  function chatApplyInfo(conv, info) {
    if (!info) return;
    if (info.model) conv.model = info.provider ? (info.provider + "/" + info.model) : info.model;
    var has = chatHasCeoPack(info);
    if (has !== null) conv.hasCeoPack = has;
    if (info.title) conv.title = info.title;
  }

  // Transcript rows (TranscriptMessage) -> bin messages. Hidden rows are
  // off-screen sends, not user-visible chat. Reasoning sidecars are cut in
  // v1. Tool rows render as compact rows. A row with images and no text
  // renders as "[image]".
  function chatMapTranscript(messages) {
    var out = [];
    (messages || []).forEach(function (m) {
      if (!m) return;
      if (m.display_kind === "hidden") return;
      var base = { rowId: (m.row_id == null ? null : m.row_id) };
      if (m.role === "user") {
        if (m.text) out.push(Object.assign({ role: "user", text: m.text }, base));
        return;
      }
      if (m.role === "assistant") {
        if (m.reasoning && !m.text) return;
        if (m.text) out.push(Object.assign({ role: "assistant", text: m.text }, base));
        else if (typeof m.content === "string" && m.content) out.push(Object.assign({ role: "assistant", text: m.content }, base));
        return;
      }
      if (m.role === "tool" || (m.tool_call_id && m.name)) {
        out.push(Object.assign({ role: "tool", name: m.name || "tool", status: "done" }, base));
        return;
      }
      if (m.text) { out.push(Object.assign({ role: "assistant", text: m.text }, base)); return; }
      var imgs = m.inline_images || (m.display_metadata && m.display_metadata.inline_images);
      if (imgs) out.push(Object.assign({ role: "notice", text: "[image]" }, base));
    });
    return out;
  }

  // Event dispatch. Every event carries session_id, which routes it to the
  // right seat's conversation — including seats the user isn't looking at,
  // so a turn started on one seat keeps streaming while another is open.
  function chatOnEvent(ev) {
    var sid = ev.session_id || null;
    var seat = (sid && chatStore.sessionSeats[sid]) || chatStore.seat;
    if (!seat) return;
    var conv = chatConv(seat);
    if (sid && conv.sessionId && sid !== conv.sessionId) return;
    var p = ev.payload || {};
    switch (ev.type) {
      case "gateway.ready":
        break;
      case "message.start":
        conv.running = true;
        conv.stream = null;
        break;
      case "message.delta": {
        var text = (p.rendered != null ? p.rendered : p.text);
        if (text == null || text === "") break;
        if (!conv.stream || conv.stream.turnId !== (p.turn_id || null) || conv.stream.segment !== (p.segment == null ? null : p.segment)) {
          conv.stream = { msgId: null, turnId: p.turn_id || null, segment: p.segment == null ? null : p.segment, applied: 0 };
          conv.stream.msgId = chatAddMsg(seat, { role: "assistant", text: "" }).id;
        }
        var st = conv.stream;
        var chunk = text;
        if (typeof p.offset === "number" && p.offset < st.applied) {
          chunk = text.slice(st.applied - p.offset);
        }
        if (chunk) {
          st.applied += chunk.length;
          var target = chatFindMsg(conv, st.msgId);
          if (target) target.text += chunk;
        }
        conv.running = true;
        break;
      }
      case "message.complete":
        conv.running = false;
        conv.stream = null;
        break;
      case "message.interim":
        if (p.text) chatAddMsg(seat, { role: "notice", text: p.text });
        break;
      case "session.info":
        chatApplyInfo(conv, p);
        break;
      case "session.title":
        if (p.title) conv.title = p.title;
        break;
      case "tool.start": {
        var row = chatAddMsg(seat, { role: "tool", name: p.name || "tool", status: "running" });
        if (p.tool_id) conv.toolRows[p.tool_id] = row.id;
        break;
      }
      case "tool.progress":
        break;
      case "tool.complete": {
        var done = p.tool_id && conv.toolRows[p.tool_id] ? chatFindMsg(conv, conv.toolRows[p.tool_id]) : null;
        if (done) {
          done.status = "done";
          if (typeof p.duration_s === "number") done.duration = Math.round(p.duration_s * 10) / 10;
        }
        break;
      }
      case "subagent.start": {
        var goal = (p.goal && String(p.goal).length > 60) ? String(p.goal).slice(0, 60) + "…" : (p.goal || "");
        var note = chatAddMsg(seat, {
          role: "notice",
          text: chatSeatDisplay(seat) + " asked a helper" + (goal ? ": " + goal : "…"),
        });
        if (p.subagent_id) conv.subagentRows[p.subagent_id] = note.id;
        break;
      }
      case "subagent.complete": {
        var nId = p.subagent_id && conv.subagentRows[p.subagent_id];
        var n = nId ? chatFindMsg(conv, nId) : null;
        if (n) n.text = chatSeatDisplay(seat) + "'s helper finished.";
        break;
      }
      case "subagent.progress":
      case "subagent.tool":
        break;
      case "approval.request":
      case "clarify.request":
        if (!conv.approvalNoteId) {
          conv.approvalNoteId = chatAddMsg(seat, {
            role: "notice", kind: "approval",
            text: "The agent is asking for approval — open in Sessions to answer.",
            link: "/sessions",
          }).id;
        }
        break;
      case "approval.settled":
      case "clarify.settled":
        if (conv.approvalNoteId) {
          chatRemoveMsg(conv, conv.approvalNoteId);
          conv.approvalNoteId = null;
        }
        break;
      case "error":
        chatAddMsg(seat, { role: "notice", kind: "error", text: (p.message || p.text || "Something went wrong.") });
        conv.running = false;
        conv.stream = null;
        break;
      default:
        break;
    }
    chatNotify();
  }

  // --- Chat bin REST + RPC actions -----------------------------------------
  function chatWorkspaceCwd(profile) {
    var url = "/api/chat/workspaces" + (profile ? "?profile=" + encodeURIComponent(profile) : "");
    return fetchJSON(url).then(function (res) {
      return (res && res.default_cwd) ? res.default_cwd : null;
    }, function () { return null; });
  }

  function chatLazyCreate(seat, done) {
    var conv = chatConv(seat);
    var profile = chatSeatProfile(seat, conv);
    chatWorkspaceCwd(profile).then(function (cwd) {
      var params = { source: "ceo-pack", idempotency_key: chatUuid(), close_on_disconnect: false };
      if (profile) params.profile = profile;
      if (cwd) params.cwd = cwd;
      chatStore.client.request("session.create", params).then(function (res) {
        conv.sessionId = res.session_id;
        chatStore.sessionSeats[res.session_id] = seat;
        chatStore.client.noteSession(res.session_id, profile);
        chatApplyInfo(conv, res.info);
        if (res.messages && res.messages.length) {
          conv.messages = chatMapTranscript(res.messages).slice(-CHAT_HISTORY_RENDER);
          conv.loadedCount = conv.messages.length;
          conv.totalCount = res.message_count || conv.messages.length;
          conv.hasMore = (res.message_count || 0) > CHAT_HISTORY_RENDER;
        }
        done(null);
      }, function (err) { done(err); });
    });
  }

  function chatCreateFailed(seat, text, err) {
    var conv = chatConv(seat);
    var msg = parseApiErrorMessage(err);
    conv.failedSend = text;
    if (/profile/i.test(msg) && /not found|does not exist|no such|unknown/i.test(msg)) {
      chatAddMsg(seat, {
        role: "notice", kind: "error", action: "fallback", seat: seat,
        text: "That profile doesn't exist anymore — " + msg,
      });
    } else {
      chatAddMsg(seat, { role: "notice", kind: "error", text: "Couldn't start a chat with " + chatSeatDisplay(seat) + " — " + msg });
    }
    chatNotify();
  }

  function chatSubmit(seat, text) {
    var conv = chatConv(seat);
    var profile = chatSeatProfile(seat, conv);
    var params = { session_id: conv.sessionId, text: text, title_preview: String(text).slice(0, 60) };
    if (profile) params.profile = profile;
    conv.running = true;
    conv.failedSend = null;
    chatNotify();
    chatStore.client.request("prompt.submit", params).then(function () {
      // The reply arrives as events; nothing to do here.
    }, function (err) {
      conv.running = false;
      conv.stream = null;
      var msg = parseApiErrorMessage(err);
      if (/session/i.test(msg) && /not found|deleted|archived|gone|expired/i.test(msg) && !conv.goneRetried) {
        conv.goneRetried = true;
        var oldSid = conv.sessionId;
        conv.sessionId = null;
        if (oldSid) { delete chatStore.sessionSeats[oldSid]; chatStore.client.forgetSession(oldSid); }
        chatAddMsg(seat, { role: "notice", text: "That chat couldn't be reopened — started a new one." });
        chatSend(seat, text, true);
        return;
      }
      conv.goneRetried = false;
      conv.failedSend = text;
      chatAddMsg(seat, { role: "notice", kind: "error", text: "Hermes is busy — retry" });
      chatNotify();
    });
  }

  function chatSend(seat, text, isRetry) {
    var conv = chatConv(seat);
    if (conv.running || conv.creating) return;
    if (!chatStore.client || chatStore.conn !== "open") return;
    if (!isRetry) chatAddMsg(seat, { role: "user", text: text });
    if (!conv.sessionId) {
      conv.creating = true;
      chatNotify();
      chatLazyCreate(seat, function (err) {
        conv.creating = false;
        if (err) { chatCreateFailed(seat, text, err); return; }
        chatSubmit(seat, text);
      });
      return;
    }
    chatSubmit(seat, text);
  }

  function chatResumeSession(seat, sessionId) {
    var conv = chatConv(seat);
    if (conv.sessionId === sessionId && conv.messages.length) return;
    if (conv.creating) return;
    var profile = chatSeatProfile(seat, conv);
    var params = { session_id: sessionId };
    if (profile) params.profile = profile;
    conv.creating = true;
    chatNotify();
    chatStore.client.request("session.resume", params).then(function (res) {
      conv.creating = false;
      var oldSid = conv.sessionId;
      if (oldSid && oldSid !== res.session_id) {
        delete chatStore.sessionSeats[oldSid];
        chatStore.client.forgetSession(oldSid);
      }
      conv.sessionId = res.session_id;
      chatStore.sessionSeats[res.session_id] = seat;
      chatStore.client.noteSession(res.session_id, profile);
      var mapped = chatMapTranscript(res.messages || []).slice(-CHAT_HISTORY_RENDER);
      conv.messages = mapped;
      conv.loadedRowIds = {};
      (res.messages || []).forEach(function (m) {
        if (m && m.row_id != null) conv.loadedRowIds[m.row_id] = true;
      });
      conv.loadedCount = mapped.length;
      conv.totalCount = res.message_count || mapped.length;
      conv.hasMore = (res.message_count || 0) > CHAT_HISTORY_RENDER;
      conv.earlierLoaded = 0;
      conv.stream = null;
      conv.running = !!res.running;
      chatApplyInfo(conv, res.info);
      chatNotify();
    }, function () {
      conv.creating = false;
      conv.sessionId = null;
      conv.messages = [];
      chatAddMsg(seat, { role: "notice", text: "That chat couldn't be reopened — started a new one." });
      chatNotify();
    });
  }

  function chatLoadEarlier(seat) {
    var conv = chatConv(seat);
    if (!conv.sessionId || conv.loadingEarlier) return;
    var profile = chatSeatProfile(seat, conv);
    var offset = conv.earlierLoaded;
    var url = "/api/sessions/" + encodeURIComponent(conv.sessionId) + "/messages?order=oldest&limit=" + CHAT_HISTORY_PAGE + "&offset=" + offset;
    if (profile) url += "&profile=" + encodeURIComponent(profile);
    conv.loadingEarlier = true;
    chatNotify();
    fetchJSON(url).then(function (res) {
      conv.loadingEarlier = false;
      var rows = chatMapTranscript(res.messages || []);
      // Pages load oldest-first, so the middle of a long transcript fills
      // in as you keep loading; dedupe by row_id against rows already shown
      // (the REST page includes compression ancestors).
      var fresh = [];
      rows.forEach(function (m) {
        if (m.rowId == null || !conv.loadedRowIds[m.rowId]) {
          if (m.rowId != null) conv.loadedRowIds[m.rowId] = true;
          fresh.push(m);
        }
      });
      fresh.forEach(function (m) { m.id = "m" + (++chatMsgSeq); });
      conv.messages = fresh.concat(conv.messages);
      var returned = (res.pagination && res.pagination.returned) || rows.length;
      conv.earlierLoaded += returned;
      if (returned < CHAT_HISTORY_PAGE) conv.hasMore = false;
      chatNotify();
    }, function () {
      conv.loadingEarlier = false;
      chatNotify();
    });
  }

  function chatLoadRecents(seat, more) {
    var rec = chatStore.recents[seat] || { rows: [], offset: 0, total: 0, loading: false, loaded: false, profile: null };
    chatStore.recents[seat] = rec;
    if (rec.loading) return;
    var node = chatSeatNode(seat);
    var profile = node && node.profile;
    var limit = more ? CHAT_RECENT_PAGE : CHAT_RECENT_FIRST;
    var offset = more ? rec.offset : 0;
    var url = "/api/sessions?order=recent&limit=" + limit + "&offset=" + offset + "&archived=exclude";
    if (profile) url += "&profile=" + encodeURIComponent(profile);
    rec.loading = true;
    chatNotify();
    fetchJSON(url).then(function (res) {
      var rows = (res.sessions || []).map(function (s) {
        return { id: s.id, title: s.title || "Untitled chat", lastActive: s.last_active || null, count: s.message_count || 0 };
      });
      rec.rows = more ? rec.rows.concat(rows) : rows;
      rec.offset = offset + rows.length;
      rec.total = res.total || rec.rows.length;
      rec.loading = false;
      rec.loaded = true;
      rec.profile = profile || null;
      chatNotify();
    }, function () {
      rec.loading = false;
      chatNotify();
    });
  }

  // The roster loads after the panel mounts, so the first Recent fetch can
  // run before the seat's profile is known (and list the serving profile's
  // sessions instead). Refetch when the profile the list was fetched under
  // differs from the seat's profile now.
  function chatMaybeReloadRecents(seat) {
    var rec = chatStore.recents[seat];
    var node = chatSeatNode(seat);
    var prof = (node && node.profile) || null;
    if (!rec || !rec.loaded || rec.profile !== prof) chatLoadRecents(seat, false);
  }

  function chatUseFallback(seat) {
    var conv = chatConv(seat);
    conv.useFallback = true;
    var text = conv.failedSend;
    chatAddMsg(seat, { role: "notice", text: "Chatting with Hermes as your " + chatSeatDisplay(seat) + " instead." });
    // failedSend clears when the submit starts (chatSubmit); if the socket
    // is down the send returns early and the text stays queued for Retry.
    if (text) chatSend(seat, text, true);
    chatNotify();
  }

  // Overdue is a backend-derived flag on the task object (plugin_api
  // _is_overdue: non-terminal, due date before today). The frontend reads
  // it, never recomputes it — one source of truth.
  function Avatar(props) {
    var node = props.node;
    if (node.avatar_url) {
      return h("img", {
        className: "ceo-avatar-img" + (props.small ? " ceo-avatar-img-sm" : ""),
        src: node.avatar_url,
        alt: node.display_name || node.seat,
      });
    }
    return h("span", { className: "ceo-avatar-initials" + (props.small ? " ceo-avatar-initials-sm" : "") }, node.initials || "?");
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
      className: cn("ceo-task-card", props.selected && "ceo-task-card-selected", props.changed && "ceo-task-changed"),
      onClick: function () { props.onSelect(t.task_id); },
    },
      h("span", { className: "ceo-task-title" }, t.title || "(untitled task)"),
      h("span", { className: "ceo-task-meta" },
        h(StatusBadge, { status: t.status }),
        t.owner ? h("span", null, " · " + t.owner) : null,
        t.due_date ? h("span", null, " · due " + t.due_date) : null,
        t.overdue ? h(Badge, { className: "ceo-overdue" }, "Overdue") : null,
        t.approval_required ? h(Badge, { className: "ceo-type1" }, "Type 1") : null),
      h("span", { className: "ceo-task-sub" },
        (t.seat || "unassigned seat") + " · updated " + timeOrUnknown(t.updated_at)));
  }

  function TaskDetail(props) {
    var d = props.detail;
    if (!d) return null;
    var _as = useState(d.seat || "");
    var assignSeat = _as[0], setAssignSeat = _as[1];
    var status = d.task_status || d.status;
    var assignable = status === "proposed" || status === "approved";
    var completable = status === "running";
    return h(Card, { className: "ceo-detail" },
      h(CardHeader, null,
        h(CardTitle, null, d.title || "(untitled task)"),
        h(Button, { className: "ceo-close", onClick: props.onClose }, "Close")),
      h(CardContent, null,
        h("div", { className: "ceo-detail-grid" },
          h("div", null, h("strong", null, "Status: "), h(StatusBadge, { status: status })),
          h("div", null, h("strong", null, "Seat: "), d.seat || "unknown"),
          h("div", null, h("strong", null, "Owner: "), d.owner || "unassigned"),
          h("div", null, h("strong", null, "Route: "), d.model_route || "not routed"),
          h("div", null, h("strong", null, "Due: "), d.due_date || "none"),
          h("div", null, h("strong", null, "Created: "), timeOrUnknown(d.created_at)),
          h("div", null, h("strong", null, "Started: "), timeOrUnknown(d.started_at)),
          h("div", null, h("strong", null, "Completed: "), timeOrUnknown(d.completed_at)),
          h("div", null, h("strong", null, "Approved by: "), d.approved_by || "—"),
          h("div", null, h("strong", null, "Retries: "), String(d.retry_count == null ? 0 : d.retry_count))),
        h("div", { className: "ceo-detail-desc" },
          h("strong", null, "Description: "),
          d.description || "none"),
        assignable
          ? h("div", { className: "ceo-detail-assign" },
              h("h4", null, "Assign"),
              h("select", {
                className: "ceo-select", value: assignSeat,
                onChange: function (e) { setAssignSeat(e.target.value); },
              },
                h("option", { value: "" }, "Pick a seat…"),
                (props.seats || []).map(function (s) {
                  return h("option", { key: s.seat, value: s.seat }, s.display_name || s.seat);
                })),
              h(Button, {
                disabled: !assignSeat || props.assignBusy,
                onClick: function () { props.onAssign(assignSeat); },
              }, props.assignBusy ? "Assigning…" : "Assign"))
          : null,
        completable
          ? h("div", { className: "ceo-detail-assign" },
              h("h4", null, "Finish"),
              h(Button, {
                disabled: props.completeBusy,
                onClick: function () { props.onComplete(); },
              }, props.completeBusy ? "Completing…" : "Mark complete"),
              h("p", { className: "ceo-note" }, "Completed, not verified — verification is a separate step."))
          : null,
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

  function CreateTaskForm(props) {
    var _t = useState("");
    var title = _t[0], setTitle = _t[1];
    var _s = useState("");
    var seat = _s[0], setSeat = _s[1];
    var _r = useState("");
    var runId = _r[0], setRunId = _r[1];
    var _a = useState(false);
    var type1 = _a[0], setType1 = _a[1];
    var _d = useState("");
    var dueDate = _d[0], setDueDate = _d[1];
    var _de = useState("");
    var description = _de[0], setDescription = _de[1];
    var runs = props.runs || [];
    var effectiveRun = runId || (runs[0] && runs[0].run_id) || "";
    var canSubmit = title.trim() && seat && effectiveRun && !props.busy;
    function submit() {
      if (!canSubmit) return;
      // due_date / description are optional: undefined keys are dropped
      // by JSON.stringify, so the backend sees them only when given. A
      // bad due date is refused by the backend (400) and the parent shows
      // the server's detail string inline — never a silent failure.
      props.onCreate({
        run_id: effectiveRun, seat: seat,
        title: title.trim(), approval_required: type1,
        due_date: dueDate || undefined,
        description: description.trim() || undefined,
      }).then(function () {
        setTitle(""); setType1(false); setDueDate(""); setDescription("");
      }, function () { /* error shown by the parent */ });
    }
    if (!runs.length) {
      return h("p", { className: "ceo-note" },
        "No runs yet — create one from chat (ceo_run_create) and it appears here.");
    }
    return h("div", { className: "ceo-create-form" },
      h(Input, {
        className: "ceo-search", placeholder: "New task title…",
        value: title, onChange: function (e) { setTitle(e.target.value); },
      }),
      h("select", {
        className: "ceo-select", value: seat,
        onChange: function (e) { setSeat(e.target.value); },
      },
        h("option", { value: "" }, "Seat…"),
        (props.seats || []).map(function (s) {
          return h("option", { key: s.seat, value: s.seat }, s.display_name || s.seat);
        })),
      h("select", {
        className: "ceo-select", value: effectiveRun,
        onChange: function (e) { setRunId(e.target.value); },
      },
        runs.map(function (r) {
          return h("option", { key: r.run_id, value: r.run_id }, r.goal || r.run_id);
        })),
      h("label", { className: "ceo-type1-check" },
        h("input", {
          type: "checkbox", checked: type1,
          onChange: function (e) { setType1(e.target.checked); },
        }),
        " Type 1 (needs owner approval)"),
      h("input", {
        type: "date", className: "ceo-select", value: dueDate,
        title: "Due date (optional)",
        onChange: function (e) { setDueDate(e.target.value); },
      }),
      h("textarea", {
        className: "ceo-search", placeholder: "Description (optional)…",
        value: description, rows: 2,
        style: { flex: "1 1 100%" },
        onChange: function (e) { setDescription(e.target.value); },
      }),
      h(Button, { disabled: !canSubmit, onClick: submit },
        props.busy ? "Creating…" : "Create task"));
  }

  function OrgNode(props) {
    var node = props.node;
    var selected = props.selectedSeat === node.seat;
    var live = isLive(node.last_activity);
    var changedSeats = props.changedSeats || {};
    return h("div", { className: "ceo-org-node-wrap" },
      h("button", {
        type: "button",
        className: cn("ceo-org-node", selected && "ceo-org-node-selected", live && "ceo-org-node-live", changedSeats[node.seat] && "ceo-org-node-changed"),
        onClick: function () { props.onSelectSeat(node.seat); },
        title: "Show this seat's assignments",
      },
        h(Avatar, { node: node }),
        h("span", { className: "ceo-org-name" }, node.display_name || node.seat),
        h("span", { className: "ceo-org-seat" }, node.seat),
        live ? h("span", { className: "ceo-live-dot", title: "Active in the last 2 minutes" }) : null,
        live ? h("span", { className: "ceo-live-label" }, "LIVE") : null,
        h("span", { className: "ceo-org-activity" }, "Last active: " + timeOrUnknown(node.last_activity)),
        h("span", { className: "ceo-org-binding" },
          node.bound ? ("Bound: " + (node.profile || "profile")) : "Reference agent"),
        node.missing && node.missing.length
          ? h(Badge, { className: "ceo-missing" }, node.missing.length + " capability gap" + (node.missing.length === 1 ? "" : "s"))
          : null),
      node.children && node.children.length
        ? h("div", null,
            h("div", { className: "ceo-org-connector" }),
            h("div", { className: "ceo-org-children" },
              node.children.map(function (child) {
                return h(OrgNode, {
                  key: child.seat, node: child,
                  selectedSeat: props.selectedSeat, onSelectSeat: props.onSelectSeat,
                  changedSeats: changedSeats,
                });
              })))
        : null);
  }

  // Pending approval card: task + amber PENDING badge + Type 1 marker +
  // Approve / Reject / Assign. Assign uses the same route as the task
  // detail panel (owner = seat). Approval here is state, not an enforced
  // execution boundary — v0.5.0 is flag-only, stated in the UI note.
  // Stage 1 gate: the note under the buttons says whether approving this
  // task mints a one-time ticket for its recorded action, or is approval
  // only (no action recorded). Warn mode only — nothing is enforced yet.
  function ApprovalRow(props) {
    var t = props.task;
    var _s = useState(t.seat || "");
    var assignSeat = _s[0], setAssignSeat = _s[1];
    var key = t.task_id;
    var assignable = t.status === "proposed" || t.status === "approved";
    var gateNote = t.action_tool
      ? "Approving mints a one-time ticket for " + t.action_tool + " (30 min, single use). Warn mode only — nothing is enforced yet."
      : "This task names no action, so approving it mints no ticket — approval only. Warn mode only — nothing is enforced yet.";
    return h("div", { className: "ceo-approval-card" },
      h("div", { className: "ceo-task-meta" },
        h("span", { className: "ceo-pending-badge" }, "Pending"),
        t.approval_required ? h(Badge, { className: "ceo-type1" }, "Type 1") : null),
      h(TaskCard, { task: t, selected: props.selected, onSelect: props.onSelect, changed: props.changed }),
      h("div", { className: "ceo-approval-actions" },
        h(Button, {
          className: "ceo-approve",
          disabled: props.busy === "approve:" + key,
          onClick: function () { props.onApprove(key); },
        }, props.busy === "approve:" + key ? "Approving…" : "Approve"),
        h(Button, {
          className: "ceo-reject",
          disabled: props.busy === "reject:" + key,
          onClick: function () { props.onReject(key); },
        }, props.busy === "reject:" + key ? "Rejecting…" : "Reject"),
        assignable
          ? h("span", { className: "ceo-detail-assign" },
              h("select", {
                className: "ceo-select", value: assignSeat,
                onChange: function (e) { setAssignSeat(e.target.value); },
              },
                h("option", { value: "" }, "Assign to…"),
                (props.seats || []).map(function (s) {
                  return h("option", { key: s.seat, value: s.seat }, s.display_name || s.seat);
                })),
              h(Button, {
                disabled: !assignSeat || props.busy === "assign:" + key,
                onClick: function () { props.onAssign(key, assignSeat); },
              }, props.busy === "assign:" + key ? "Assigning…" : "Assign"))
          : null),
      h("p", { className: "ceo-note" }, gateNote));
  }

  // Plain-text bubbles + code fences only — no full markdown, no tables,
  // no link rendering. ``` toggles code; the rest renders pre-wrapped.
  function renderChatText(text) {
    var parts = String(text || "").split(/```/);
    return parts.map(function (seg, i) {
      if (i % 2 === 1) {
        var code = seg.replace(/^[a-zA-Z0-9_-]+\n/, "").replace(/^\n/, "").replace(/\n$/, "");
        return h("pre", { key: i, className: "ceo-chat-code" }, code);
      }
      return h("div", { key: i, className: "ceo-chat-text" }, seg);
    });
  }

  function renderChatMsg(m, seat) {
    if (m.role === "user") {
      return h("div", { key: m.id, className: "ceo-chat-msg ceo-chat-user" }, renderChatText(m.text));
    }
    if (m.role === "assistant") {
      return h("div", { key: m.id, className: "ceo-chat-msg ceo-chat-assistant" },
        m.text ? renderChatText(m.text) : h("span", { className: "ceo-chat-thinking" }, "…"));
    }
    if (m.role === "tool") {
      return h("div", { key: m.id, className: "ceo-chat-tool" },
        h("span", { className: "ceo-chat-tool-status" }, m.status === "done" ? "✓" : "…"),
        h("span", null, m.name),
        m.duration != null ? h("span", { className: "ceo-chat-tool-dur" }, m.duration + "s") : null);
    }
    return h("div", { key: m.id, className: "ceo-chat-notice" + (m.kind === "error" ? " ceo-chat-notice-error" : "") },
      h("span", null, m.text),
      m.link ? h("a", { href: m.link }, "Open in Sessions") : null,
      m.action === "fallback"
        ? h(Button, { onClick: function () { chatUseFallback(m.seat || seat); } },
            "Chat with Hermes as " + chatSeatDisplay(m.seat || seat) + " instead")
        : null);
  }

  function ChatBin(props) {
    var _v = useState(0);
    var bump = _v[1];
    var _in = useState("");
    var input = _in[0], setInput = _in[1];
    var _sr = useState(false);
    var showRecent = _sr[0], setShowRecent = _sr[1];

    useEffect(function () {
      var fn = function () { bump(function (n) { return n + 1; }); };
      chatStore.listeners.push(fn);
      return function () {
        var i = chatStore.listeners.indexOf(fn);
        if (i !== -1) chatStore.listeners.splice(i, 1);
      };
    }, []);

    var seats = props.seats || [];
    chatStore.seats = seats;
    if (props.headSeat) chatStore.headSeat = props.headSeat;

    // One client for the tab's lifetime; the socket stays up while the
    // panel is collapsed (it hides, it doesn't unmount).
    useEffect(function () {
      if (chatStore.client) return undefined;
      if (!chatSdkOk()) {
        chatStore.conn = "failed";
        chatStore.banner = { text: "Update Hermes to use chat here — this dashboard needs the plugin SDK 1.1.0 or newer.", retry: false };
        chatNotify();
        return undefined;
      }
      chatStore.client = createCeoChatWire({
        WebSocketCtor: WebSocket,
        buildWsUrl: SDK.buildWsUrl,
        onEvent: chatOnEvent,
        onState: function (s, detail) {
          chatStore.conn = s;
          if (s === "failed") {
            chatStore.banner = { text: (detail && detail.message) || "Couldn't reach the gateway — check it's running, then reopen chat.", retry: true };
          } else if (s === "retrying") {
            chatStore.banner = { text: "Connection dropped — retrying…", retry: false };
          } else {
            chatStore.banner = null;
          }
          chatNotify();
        },
        onReplayGap: function (sid) {
          var seat = sid && chatStore.sessionSeats[sid];
          if (seat) {
            var conv = chatConv(seat);
            if (conv.sessionId === sid) chatResumeSession(seat, sid);
          }
        },
      });
      chatStore.client.connect();
      return undefined;
    }, []);

    // Default seat: last-used, defaulting to the head seat (CEO).
    useEffect(function () {
      if (!chatStore.seat && seats.length) {
        chatStore.seat = chatStore.headSeat || "CEO";
        chatMaybeReloadRecents(chatStore.seat);
        chatNotify();
      }
    }, [seats.length, props.headSeat]);

    var seat = chatStore.seat || chatStore.headSeat || "CEO";
    var conv = chatConv(seat);
    var rec = chatStore.recents[seat] || { rows: [], offset: 0, total: 0, loading: false, loaded: false };

    useEffect(function () {
      chatMaybeReloadRecents(seat);
    }, [seat]);

    var msgCount = conv.messages.length;
    useEffect(function () {
      if (chatBodyEl && chatAtBottom) chatBodyEl.scrollTop = chatBodyEl.scrollHeight;
    }, [msgCount, seat, conv.running]);

    function switchSeat(next) {
      if (!next || next === chatStore.seat) return;
      chatStore.seat = next;
      var c = chatConv(next);
      if (c.sessionId && !c.messages.length && chatStore.conn === "open" && chatStore.client) {
        chatResumeSession(next, c.sessionId);
      }
      if (!chatStore.recents[next] || !chatStore.recents[next].loaded) chatLoadRecents(next, false);
      chatNotify();
    }

    var node = chatSeatNode(seat, seats);
    var displayName = (node && node.display_name) || seat;
    var bound = !!(node && node.bound);
    var conn = chatStore.conn;
    var connDot = conn === "open" ? "green" : (conn === "connecting" || conn === "retrying") ? "amber" : "grey";
    var connTitle = conn === "open" ? "Connected"
      : conn === "connecting" ? "Connecting…"
      : conn === "retrying" ? "Connection dropped — retrying…"
      : "Not connected";
    var gate = conv.hasCeoPack;
    var gateClass = gate === true ? "ceo-chat-gate-on" : gate === false ? "ceo-chat-gate-off" : "ceo-chat-gate-unknown";
    var gateWord = gate === true ? "on" : gate === false ? "off" : "?";
    var gateTitle = gate === true
      ? "Gate: on for this seat — the Type 1 gate watches tool calls here. Warn mode: it observes, it doesn't block."
      : gate === false
        ? "Gate: off in this profile — CEO Pack isn't installed here, so no Type 1 gate watches this chat."
        : "Gate: unknown yet — it shows once this seat's session reports its tools.";
    var canSend = conn === "open" && !conv.running && !conv.creating;

    function doSend() {
      var text = input.trim();
      if (!text || !canSend) return;
      setInput("");
      chatSend(seat, text);
    }

    function doStop() {
      if (!conv.sessionId || !chatStore.client) return;
      var params = { session_id: conv.sessionId };
      var profile = chatSeatProfile(seat, conv);
      if (profile) params.profile = profile;
      chatStore.client.request("session.interrupt", params).then(function () {
        conv.running = false;
        conv.stream = null;
        chatNotify();
      }, function () {
        conv.running = false;
        conv.stream = null;
        chatNotify();
      });
    }

    return h("div", { className: "ceo-chatbin" + (props.open ? "" : " ceo-chatbin-hidden") },
      h("div", { className: "ceo-chat-header" },
        h("select", {
          className: "ceo-select ceo-chat-seat", value: seat,
          onChange: function (e) { switchSeat(e.target.value); },
          title: "Which seat you're chatting with",
        },
          seats.length
            ? seats.map(function (s) {
                return h("option", { key: s.seat, value: s.seat }, s.display_name || s.seat);
              })
            : h("option", { value: seat }, seat)),
        h("span", { className: "ceo-chat-dot ceo-chat-dot-" + connDot, title: connTitle }),
        h("span", { className: "ceo-chat-gate " + gateClass, title: gateTitle }, "Gate: " + gateWord),
        h(Button, { className: "ceo-close", onClick: props.onClose, title: "Hide chat" }, "×")),
      h("div", { className: "ceo-chat-sub" },
        h("span", { className: "ceo-chat-model" }, "Model: " + (conv.model || "unknown")),
        conv.title ? h("span", { className: "ceo-chat-title" }, conv.title) : null),
      !bound
        ? h("p", { className: "ceo-note" },
            displayName + " has no profile yet — you're talking to Hermes as your " + displayName + ".")
        : null,
      bound && gate === false
        ? h("p", { className: "ceo-note" },
            "CEO Pack isn't installed in this profile — you're chatting with the agent as-is. /ceo won't work here.")
        : null,
      chatStore.banner
        ? h("div", { className: "ceo-chat-banner", role: "alert" },
            h("span", null, chatStore.banner.text),
            chatStore.banner.retry
              ? h(Button, {
                  className: "ceo-retry",
                  onClick: function () { if (chatStore.client) chatStore.client.manualRetry(); },
                }, "Retry")
              : null)
        : null,
      h("div", { className: "ceo-chat-recent" },
        h("button", {
          type: "button",
          className: "ceo-chat-recent-toggle",
          onClick: function () { setShowRecent(!showRecent); },
        },
          "Recent chats (" + rec.rows.length + ")" +
          (rec.total > rec.rows.length ? " of " + rec.total : "") +
          (showRecent ? " ▾" : " ▸")),
        showRecent
          ? h("div", null,
              rec.rows.length
                ? rec.rows.map(function (r) {
                    return h("button", {
                      type: "button",
                      key: r.id,
                      className: "ceo-chat-recent-row" + (conv.sessionId === r.id ? " ceo-chat-recent-active" : ""),
                      onClick: function () { chatResumeSession(seat, r.id); },
                    },
                      h("span", { className: "ceo-chat-recent-title" }, r.title),
                      h("span", { className: "ceo-chat-recent-meta" },
                        (r.count ? r.count + " messages · " : "") + timeOrUnknown(r.lastActive ? new Date(r.lastActive * 1000).toISOString() : null)));
                  })
                : h("p", { className: "ceo-note" }, "No recent chats for this seat yet."),
              rec.rows.length < rec.total
                ? h(Button, {
                    disabled: rec.loading,
                    onClick: function () { chatLoadRecents(seat, true); },
                  }, rec.loading ? "Loading…" : "Show more")
                : null)
          : null),
      h("div", {
        className: "ceo-chat-body",
        ref: function (el) { chatBodyEl = el; },
        onScroll: function (e) {
          var el = e.target;
          chatAtBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 40;
        },
      },
        conv.hasMore
          ? h("div", { className: "ceo-chat-more" },
              h(Button, {
                disabled: conv.loadingEarlier,
                onClick: function () { chatLoadEarlier(seat); },
              }, conv.loadingEarlier ? "Loading…" : "Load earlier messages"))
          : null,
        conv.messages.length === 0 && !conv.sessionId && !conv.creating
          ? h("div", { className: "ceo-chat-empty" },
              h("div", null, h("strong", null, displayName), " — " + seat),
              h("p", { className: "ceo-note" }, "No chats with " + displayName + " yet."))
          : conv.messages.map(function (m) { return renderChatMsg(m, seat); }),
        conv.creating
          ? h("div", { className: "ceo-chat-notice" }, "Starting chat…")
          : null),
      conv.failedSend
        ? h("div", { className: "ceo-chat-banner", role: "alert" },
            h("span", null, "Hermes is busy — retry"),
            h(Button, {
              className: "ceo-retry",
              onClick: function () {
                // failedSend clears when the submit starts (chatSubmit);
                // if the socket is down the send returns early and the
                // text stays queued.
                var text = conv.failedSend;
                if (text) chatSend(seat, text, true);
              },
            }, "Retry"))
        : null,
      h("div", { className: "ceo-chat-composer" },
        h("textarea", {
          className: "ceo-chat-input",
          rows: 2,
          value: input,
          placeholder: conn === "open"
            ? ("Ask your " + displayName + "…")
            : "Gateway is stopped — reconnect",
          disabled: !canSend,
          onChange: function (e) { setInput(e.target.value); },
          onKeyDown: function (e) {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); doSend(); }
          },
        }),
        conv.running
          ? h(Button, { onClick: doStop }, "Stop")
          : h(Button, { disabled: !canSend || !input.trim(), onClick: doSend }, "Send")),
      h("p", { className: "ceo-note" }, "Ask in plain words — you'll get a plain answer."));
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
    var _ch = useState({ tasks: {}, seats: {}, history: {}, at: 0 });
    var changed = _ch[0], setChanged = _ch[1];
    var _wb = useState(null);
    var writeBusy = _wb[0], setWriteBusy = _wb[1];
    var _we = useState(null);
    var writeError = _we[0], setWriteError = _we[1];
    var _co = useState(true);
    var chatOpen = _co[0], setChatOpen = _co[1];

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
        // Live window: diff this poll against what we last saw, so
        // just-changed rows flash once. The first load only primes the
        // snapshot — everything is "new" then, and flashing it all would
        // be noise.
        var fresh = { tasks: {}, seats: {}, history: {} };
        if (lastSeenPrimed) {
          (next.tasks && next.tasks.tasks || []).forEach(function (t) {
            var prev = lastSeen.tasks[t.task_id];
            if (!prev || prev.status !== t.status || prev.updated_at !== t.updated_at) fresh.tasks[t.task_id] = true;
          });
          (next.roster && next.roster.seats || []).forEach(function (s) {
            if (lastSeen.seats[s.seat] !== s.last_activity) fresh.seats[s.seat] = true;
          });
          (next.activity && next.activity.recent_history || []).forEach(function (ev) {
            if (!lastSeen.history[ev.id]) fresh.history[ev.id] = true;
          });
        }
        var seenTasks = {};
        (next.tasks && next.tasks.tasks || []).forEach(function (t) {
          seenTasks[t.task_id] = { status: t.status, updated_at: t.updated_at };
        });
        var seenSeats = {};
        (next.roster && next.roster.seats || []).forEach(function (s) {
          seenSeats[s.seat] = s.last_activity;
        });
        var seenHistory = {};
        (next.activity && next.activity.recent_history || []).forEach(function (ev) {
          seenHistory[ev.id] = true;
        });
        lastSeen = { tasks: seenTasks, seats: seenSeats, history: seenHistory };
        lastSeenPrimed = true;
        var stamp = Date.now();
        setChanged({ tasks: fresh.tasks, seats: fresh.seats, history: fresh.history, at: stamp });
        setTimeout(function () {
          setChanged(function (cur) {
            return cur.at === stamp ? { tasks: {}, seats: {}, history: {}, at: 0 } : cur;
          });
        }, 5000);
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
    var runs = ((data && data.runs) || {}).runs || [];

    // Board chips map to real lifecycle states: In Progress covers
    // assigned + running; Done covers completed + verified. Blocked,
    // failed, and rejected tasks show under All only.
    var BOARD_FILTERS = {
      all: null,
      proposed: ["proposed"],
      approved: ["approved"],
      "in-progress": ["assigned", "running"],
      done: ["completed", "verified"],
    };
    var filteredTasks = useMemo(function () {
      var q = filterText.trim().toLowerCase();
      var allowed = BOARD_FILTERS[filterStatus] || null;
      return tasks.filter(function (t) {
        if (selectedSeat && t.seat !== selectedSeat) return false;
        if (allowed && allowed.indexOf(t.status) === -1) return false;
        if (q && (t.title || "").toLowerCase().indexOf(q) === -1) return false;
        return true;
      });
    }, [tasks, selectedSeat, filterStatus, filterText]);

    // Chip counts come from the actual returned task data, not from
    // the seat/search-filtered view.
    function boardCount(key) {
      var allowed = BOARD_FILTERS[key];
      if (!allowed) return tasks.length;
      return tasks.filter(function (t) { return allowed.indexOf(t.status) !== -1; }).length;
    }

    function panelError(key, what) {
      if (!errors[key]) return null;
      return h(ErrorBox, { what: what, message: errors[key], onRetry: load });
    }

    // One write at a time per key: the button disables, the request runs,
    // success re-fetches everything (the 30s poll's change flash shows the
    // result), failure shows the server's detail string inline.
    function doWrite(key, promise) {
      setWriteError(null);
      setWriteBusy(key);
      promise.then(function () {
        setWriteBusy(null);
        load();
        if (selectedTaskId) {
          fetchJSON(API + "/tasks/" + encodeURIComponent(selectedTaskId)).then(
            function (v) { setSelectedTask(v); },
            function (err) { setSelectedTask({ _error: parseApiErrorMessage(err) }); });
        }
      }, function (err) {
        setWriteBusy(null);
        setWriteError(parseApiErrorMessage(err));
      });
      return promise;
    }

    function writeErrorBox() {
      if (!writeError) return null;
      return h("div", { className: "ceo-error", role: "alert" },
        h("strong", null, "That didn't go through. "),
        h("span", null, writeError),
        h(Button, { className: "ceo-retry", onClick: function () { setWriteError(null); } }, "Dismiss"));
    }

    function renderSummaryStrip() {
      if (errors.summary) return panelError("summary", "summary counts");
      if (!summary) return h("div", { className: "ceo-loading" }, "Loading summary…");
      var c = summary.tasks_by_status || {};
      function stat(label, value, accent, icon) {
        return h("div", { className: "ceo-stat ceo-stat-" + accent },
          h("span", { className: "ceo-stat-icon", "aria-hidden": "true" }, icon),
          h("span", { className: "ceo-stat-value" }, String(value)),
          h("span", { className: "ceo-stat-label" }, label));
      }
      // An empty registry still shows the six cards with honest zeroes —
      // hiding the strip made the dashboard look as if it failed to load.
      // The "no tasks yet" pointer stays, as a note under the strip.
      return h("div", null,
        h("div", { className: "ceo-stats" },
          stat("Tasks", summary.tasks_total, "gold", "▤"),
          stat("Running", c.running || 0, "teal", "▶"),
          stat("Blocked / failed", (c.blocked || 0) + (c.failed || 0), "red", "⚠"),
          stat("Pending approval", summary.pending_approvals, "amber", "◷"),
          stat("Verified", c.verified || 0, "green", "✓"),
          stat("Runs", summary.total_runs, "teal", "▸")),
        summary.empty
          ? h("p", { className: "ceo-note" },
              "No tasks yet — the registry is empty. Assign work from chat (ceo_task_create) and it shows up here.")
          : null);
    }

    function renderOrg() {
      if (errors.roster) return panelError("roster", "org chart");
      if (!roster) return h("div", { className: "ceo-loading" }, "Loading org chart…");
      var root = roster.org && roster.org.root;
      var seatTasks = selectedSeat
        ? tasks.filter(function (t) { return t.seat === selectedSeat; })
        : [];
      // Live Agent Activity side panel: real roster seats, newest activity
      // first. Name, seat, last-active time, live dot — no invented
      // "working on" lines.
      var liveSeats = (roster.seats || []).slice().sort(function (a, b) {
        var ta = a.last_activity ? new Date(a.last_activity).getTime() : 0;
        var tb = b.last_activity ? new Date(b.last_activity).getTime() : 0;
        return tb - ta;
      });
      return h("div", null,
        h("p", { className: "ceo-note" },
          "Reporting shown by the plugin's accountability model — the head seat holds priorities. ",
          "Departments and custom reporting lines plug in here when they exist; none are shown until they do. ",
          roster.snapshot_loaded
            ? "Roster loaded from the applied install config."
            : "No install config seen yet — showing the default roster."),
        h("div", { className: "ceo-org-layout" },
          h("div", null,
            root ? h(OrgNode, { node: root, selectedSeat: selectedSeat, onSelectSeat: setSelectedSeat, changedSeats: changed.seats }) : null,
            selectedSeat ? h("div", { className: "ceo-seat-tasks" },
              h("h3", null, "Assignments: " + selectedSeat + " (" + seatTasks.length + ")"),
              seatTasks.length
                ? seatTasks.map(function (t) {
                    return h(TaskCard, { key: t.task_id, task: t, selected: selectedTaskId === t.task_id, onSelect: setSelectedTaskId, changed: changed.tasks[t.task_id] });
                  })
                : h(EmptyState, null, "No assignments recorded for this seat yet."),
              h(Button, { onClick: function () { setSelectedSeat(null); } }, "Clear seat filter"))
              : h("p", { className: "ceo-note" }, "Click a seat to see its assignments.")),
          h("div", { className: "ceo-live-panel" },
            h("h3", null, "Live Agent Activity"),
            liveSeats.length
              ? liveSeats.map(function (s) {
                  var live = isLive(s.last_activity);
                  return h("div", { key: s.seat, className: "ceo-live-row" },
                    h(Avatar, { node: s, small: true }),
                    h("div", { className: "ceo-live-row-main" },
                      h("div", { className: "ceo-live-row-name" }, s.display_name || s.seat),
                      h("div", { className: "ceo-live-row-meta" }, s.seat + " · Last active: " + timeOrUnknown(s.last_activity))),
                    live ? h("span", { className: "ceo-live-dot", title: "Active in the last 2 minutes" }) : null);
                })
              : h(EmptyState, null, "No seats in the roster yet."))));
    }

    function renderActivity() {
      if (errors.activity) return panelError("activity", "activity");
      if (!activity) return h("div", { className: "ceo-loading" }, "Loading activity…");
      var hist = activity.recent_history || [];
      return h("div", null,
        h("div", { className: "ceo-activity-panels" },
          h("div", { className: "ceo-activity-panel" },
            h("h3", null, "Running now (" + (activity.running || []).length + ")"),
            (activity.running || []).length
              ? activity.running.map(function (t) {
                  return h(TaskCard, { key: t.task_id, task: t, selected: selectedTaskId === t.task_id, onSelect: setSelectedTaskId, changed: changed.tasks[t.task_id] });
                })
              : h(EmptyState, null, "Nothing running right now.")),
          h("div", { className: "ceo-activity-panel" },
            h("h3", null, "Blocked / stuck (" + (activity.stuck || []).length + ")"),
            (activity.stuck || []).length
              ? activity.stuck.map(function (t) {
                  return h(TaskCard, { key: t.task_id, task: t, selected: selectedTaskId === t.task_id, onSelect: setSelectedTaskId, changed: changed.tasks[t.task_id] });
                })
              : h(EmptyState, null, "Nothing blocked or stuck."))),
        h("h3", null, "Recent execution history"),
        hist.length
          ? h("ul", { className: "ceo-history" }, hist.map(function (ev) {
              return h("li", { key: String(ev.id), className: changed.history[ev.id] ? "ceo-history-new" : undefined },
                h("span", null, (ev.title || ev.task_id)),
                h("span", { className: "ceo-history-meta" },
                  " · " + (ev.from_status || "—") + " → " + ev.to_status + " · " + (ev.actor || "system") + " · " + timeOrUnknown(ev.at)),
                ev.note ? h("span", { className: "ceo-history-note" }, " — " + ev.note) : null);
            }))
          : h(EmptyState, null, "No recorded activity yet."));
    }

    function renderBoard() {
      if (errors.tasks) return panelError("tasks", "task board");
      if (!data || !data.tasks) return h("div", { className: "ceo-loading" }, "Loading tasks…");
      var chips = [
        { id: "all", label: "All" },
        { id: "proposed", label: "Proposed" },
        { id: "approved", label: "Approved" },
        { id: "in-progress", label: "In Progress" },
        { id: "done", label: "Done" },
      ];
      return h("div", null,
        h("div", { className: "ceo-create-panel" },
          h("h3", null, "Create New Task"),
          h(CreateTaskForm, {
            runs: runs,
            seats: roster ? roster.seats : [],
            busy: writeBusy === "create",
            onCreate: function (fields) {
              return doWrite("create", postJSON("/tasks", fields));
            },
          })),
        h("div", { className: "ceo-filters" },
          h("div", { className: "ceo-chips", role: "tablist" },
            chips.map(function (c) {
              return h(Button, {
                key: c.id,
                className: cn("ceo-chip", filterStatus === c.id && "ceo-chip-active"),
                onClick: function () { setFilterStatus(c.id); },
              }, c.label, h("span", { className: "ceo-chip-count" }, "(" + boardCount(c.id) + ")"));
            })),
          h(Label, null, "Seat"),
          h("select", {
            className: "ceo-select", value: selectedSeat || "",
            onChange: function (e) { setSelectedSeat(e.target.value || null); },
          },
            h("option", { value: "" }, "All seats"),
            (roster ? roster.seats : []).map(function (s) {
              return h("option", { key: s.seat, value: s.seat }, s.display_name || s.seat);
            })),
          h(Input, {
            className: "ceo-search", placeholder: "Search tasks…",
            value: filterText, onChange: function (e) { setFilterText(e.target.value); },
          })),
        writeErrorBox(),
        selectedTask
          ? (selectedTask._error
              ? h(ErrorBox, { what: "task detail", message: selectedTask._error, onRetry: function () { setSelectedTaskId(null); } })
              : h(TaskDetail, {
                  detail: selectedTask,
                  onClose: function () { setSelectedTaskId(null); },
                  seats: roster ? roster.seats : [],
                  assignBusy: writeBusy === "assign:" + selectedTask.task_id,
                  onAssign: function (seat) {
                    doWrite("assign:" + selectedTask.task_id,
                      postJSON("/tasks/" + encodeURIComponent(selectedTask.task_id) + "/assign", { owner: seat }));
                  },
                  completeBusy: writeBusy === "complete:" + selectedTask.task_id,
                  onComplete: function () {
                    doWrite("complete:" + selectedTask.task_id,
                      postJSON("/tasks/" + encodeURIComponent(selectedTask.task_id) + "/complete", {}));
                  },
                }))
          : null,
        tasks.length === 0
          ? h(EmptyState, null, "No tasks in the registry yet. Create one from chat (ceo_task_create) — it appears here, with its real status, once it exists.")
          : filteredTasks.length === 0
            ? h(EmptyState, null, "No tasks in this view.")
            : filteredTasks.map(function (t) {
                return h(TaskCard, { key: t.task_id, task: t, selected: selectedTaskId === t.task_id, onSelect: setSelectedTaskId, changed: changed.tasks[t.task_id] });
              }));
    }

    function renderApprovals() {
      if (errors.approvals) return panelError("approvals", "approvals");
      if (!approvals) return h("div", { className: "ceo-loading" }, "Loading approvals…");
      var pending = approvals.pending || [];
      var hist = approvals.history || [];
      return h("div", null,
        writeErrorBox(),
        h("h3", null, "Pending owner approval (" + approvals.pending_count + ")"),
        pending.length
          ? pending.map(function (t) {
              return h(ApprovalRow, {
                key: t.task_id, task: t,
                selected: selectedTaskId === t.task_id, onSelect: setSelectedTaskId,
                changed: changed.tasks[t.task_id],
                seats: roster ? roster.seats : [],
                busy: writeBusy,
                onApprove: function (id) { doWrite("approve:" + id, postJSON("/tasks/" + encodeURIComponent(id) + "/approve", {})); },
                onReject: function (id) { doWrite("reject:" + id, postJSON("/tasks/" + encodeURIComponent(id) + "/reject", {})); },
                onAssign: function (id, seat) { doWrite("assign:" + id, postJSON("/tasks/" + encodeURIComponent(id) + "/assign", { owner: seat })); },
              });
            })
          : h(EmptyState, null, "No pending owner approvals."),
        h("h3", null, "Approval history"),
        hist.length
          ? h("ul", { className: "ceo-history" }, hist.map(function (ev) {
              return h("li", { key: String(ev.id) },
                h("span", null, (ev.title || ev.task_id) + " approved"),
                h("span", { className: "ceo-history-meta" },
                  " · by " + (ev.actor || "unknown") + " · " + timeOrUnknown(ev.at)));
            }))
          : h(EmptyState, null, "No approval history yet."),
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
        h("div", { className: "ceo-header-title" },
          h("span", { className: "ceo-crown", "aria-hidden": "true" }, "♛"),
          h("div", null,
            h("h1", null, "CEO Pack"),
            h("div", { className: "ceo-title-sub" }, "Executive Workspace"),
            h("p", { className: "ceo-subtitle" },
              "Your AI C-suite at a glance. Every number below is a live read of the task registry — nothing is invented."))),
        h("div", { className: "ceo-header-actions" },
          h(Button, { onClick: function () { setChatOpen(!chatOpen); } }, chatOpen ? "Hide chat" : "Chat"),
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
      h("div", { className: "ceo-main-row" },
        h("div", { className: "ceo-panel" },
          tab === "org" ? renderOrg()
            : tab === "activity" ? renderActivity()
            : tab === "board" ? renderBoard()
            : renderApprovals()),
        h(ChatBin, {
          seats: roster ? roster.seats : [],
          headSeat: roster ? roster.head_seat : null,
          open: chatOpen,
          onClose: function () { setChatOpen(false); },
        })));
  }

  REGISTRY.register("ceo-pack", CEOPackPage);
})();
