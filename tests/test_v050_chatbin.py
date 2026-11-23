"""CEO Pack chat bin tests (v1, Nov 21, 2026).

Static guardrails (spec §11) on the shipped JS + a node harness for the
vendored NDJSON JSON-RPC client (CeoChatWire), which is extracted from
dashboard/dist/index.js between its BEGIN/END markers and exercised with a
mock WebSocket. No browser exists in this repo, so the 12 manual live
checks are owed on Adrian's laptop — they are NOT claimed here.

Fictional data only; the Python tests read source files, no registry.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX_JS = ROOT / "dashboard" / "dist" / "index.js"
DESKTOP_JS = ROOT / "desktop" / "plugin.js"
MANIFEST = ROOT / "dashboard" / "manifest.json"
API_PY = ROOT / "dashboard" / "plugin_api.py"

sys.path.insert(0, str(ROOT))

import importlib.util


def _load_api():
    spec = importlib.util.spec_from_file_location(
        "ceo_pack_dashboard_api_chatbin", API_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ceo_pack_dashboard_api_chatbin"] = mod
    spec.loader.exec_module(mod)
    return mod


api = _load_api()


def _routes():
    out = []
    for route in api.router.routes:
        if isinstance(route, tuple):
            method, path, _fn = route
            out.append(({method}, path))
        else:
            out.append((set(getattr(route, "methods", set())), route.path))
    return out


class ChatBinStaticGuards(unittest.TestCase):
    def test_manifest_gains_no_chat_tab_or_override(self):
        """Placement is a docked panel (spec §1, option B): the manifest
        keeps exactly the one /ceo-pack tab and gains no override."""
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest.get("tab"),
            {"path": "/ceo-pack", "position": "after:kanban"})
        self.assertNotIn("override", manifest.get("tab", {}))
        self.assertNotIn("override", manifest)

    def test_no_new_backend_routes(self):
        """v1 = zero new backend routes (spec §9): the bin calls host
        endpoints directly; the plugin API keeps its known routes only."""
        routes = _routes()
        for methods, path in routes:
            self.assertNotIn("chat", path.lower(),
                             f"chat route appeared in the plugin API: {path}")
            self.assertFalse(path.startswith("/sessions"),
                            f"session route appeared: {path}")
            self.assertFalse(path.startswith("/ws"),
                            f"websocket route appeared: {path}")
        posts = {p for m, p in routes if "POST" in m}
        self.assertEqual(posts, {
            "/tasks",
            "/tasks/{task_id}/approve",
            "/tasks/{task_id}/reject",
            "/tasks/{task_id}/assign",
            "/tasks/{task_id}/complete",
        })

    def test_build_ws_url_used_no_hand_rolled_ws_url(self):
        """The socket URL comes from SDK.buildWsUrl (fresh ticket per
        connect): the SDK function is injected into the vendored client,
        and the client calls it with "/api/ws". No hand-assembled WS URL,
        no literal ws(s):// URL."""
        src = INDEX_JS.read_text(encoding="utf-8")
        self.assertIn("buildWsUrl: SDK.buildWsUrl", src)
        self.assertIn('buildWsUrl("/api/ws")', src)
        self.assertNotIn('new WebSocket("', src)
        self.assertNotIn("new WebSocket('", src)
        self.assertNotIn("ws://", src)
        self.assertNotIn("wss://", src)

    def test_session_token_never_read(self):
        """The token string appears only in comments (the rule itself);
        no code line reads window.__HERMES_SESSION_TOKEN__."""
        src = INDEX_JS.read_text(encoding="utf-8")
        for line in src.splitlines():
            if "__HERMES_SESSION_TOKEN__" in line:
                stripped = line.strip()
                self.assertTrue(
                    stripped.startswith("//") or stripped.startswith("*"),
                    f"non-comment token read: {line.strip()}")

    def test_session_create_shape(self):
        """session.create carries source 'ceo-pack', close_on_disconnect
        false (a dropped socket must not kill a real chat), and a UUID
        idempotency_key (a retried create returns the same session)."""
        src = INDEX_JS.read_text(encoding="utf-8")
        self.assertIn('"session.create"', src)
        self.assertIn('source: "ceo-pack"', src)
        self.assertIn("close_on_disconnect: false", src)
        self.assertIn("idempotency_key", src)

    def test_no_hidden_preamble_sent(self):
        """Adrian's answer to question 5: no hidden CEO preamble in v1.
        display_kind appears only as the transcript filter that SKIPS
        hidden rows — never as a send parameter."""
        src = INDEX_JS.read_text(encoding="utf-8")
        self.assertNotIn('display_kind: "hidden"', src)
        self.assertNotIn("display_kind:'hidden'", src)
        for line in src.splitlines():
            if "display_kind" in line:
                self.assertIn("===", line,
                              f"display_kind used outside the filter: {line.strip()}")

    def test_sdk_pinned_with_update_fallback(self):
        """The bin pins the SDK contract (1.1.0) and renders an update
        note instead of crashing on an older host."""
        src = INDEX_JS.read_text(encoding="utf-8")
        self.assertIn('CHAT_SDK_MIN = "1.1.0"', src)
        self.assertIn("Update Hermes to use chat here", src)

    def test_gate_copy_never_calls_warn_enforcement(self):
        """Warn mode is never described as enforcement anywhere in the
        shipped JS — and the gate title states it honestly instead."""
        for path in (INDEX_JS, DESKTOP_JS):
            src = path.read_text(encoding="utf-8")
            self.assertNotIn("Gate: protected", src)
            self.assertNotIn("warn mode is enforcement", src.lower())
            self.assertNotIn("warn mode enforces", src.lower())
        self.assertIn("it doesn't block",
                      INDEX_JS.read_text(encoding="utf-8"))

    def test_desktop_is_linkout_only(self):
        """Desktop v1: a link-out to Sessions, not a composer and not a
        second chat client (ctx.rest/socket can't reach the host wire)."""
        src = DESKTOP_JS.read_text(encoding="utf-8")
        self.assertIn("Open in Sessions", src)
        self.assertIn("openExternal", src)
        self.assertIn("/sessions", src)
        self.assertNotIn("prompt.submit", src)
        self.assertNotIn("buildWsUrl", src)
        self.assertNotIn("ChatBin", src)
        self.assertNotIn("CeoChatWire", src)
        self.assertEqual(src.count("var(--ui-"), 0,
                         "v2.4 rule: no --ui-* theme vars on Desktop")


# --- Node harness for the vendored client ---------------------------------
# The wire is extracted verbatim between its BEGIN/END markers and run
# under node with a mock WebSocket. Skipped when node is absent.

NODE_HARNESS = r"""
const fs = require('fs');
const INDEX = process.argv[2];
const src = fs.readFileSync(INDEX, 'utf8').replace(/\r\n/g, '\n');  // Windows autocrlf checkouts
const m = src.match(/\/\/ --- BEGIN CeoChatWire ---\n([\s\S]*?)\/\/ --- END CeoChatWire ---/);
if (!m) { console.log(JSON.stringify({ passed: [], failed: ['wire markers not found'] })); process.exit(0); }
const createCeoChatWire = new Function(m[1] + '\n;return createCeoChatWire;')();

const results = { passed: [], failed: [] };
function check(name, cond, extra) {
  if (cond) results.passed.push(name);
  else results.failed.push(name + (extra ? ' :: ' + extra : ''));
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

class MockWS {
  constructor(url) {
    this.url = url; this.sent = []; this.readyState = 0;
    MockWS.instances.push(this);
  }
  send(data) { this.sent.push(data); }
  close() { this.readyState = 3; if (this.onclose) this.onclose({ code: 1000 }); }
  _open() { this.readyState = 1; if (this.onopen) this.onopen(); }
  _msg(text) { if (this.onmessage) this.onmessage({ data: text }); }
  _close(code) { this.readyState = 3; if (this.onclose) this.onclose({ code }); }
}
MockWS.instances = [];

function makeWire(opts) {
  const events = [];
  const states = [];
  let gaps = [];
  let ticketN = 0;
  const wire = createCeoChatWire(Object.assign({
    WebSocketCtor: MockWS,
    buildWsUrl: (path) => {
      ticketN += 1;
      buildCalls.push(path);
      return Promise.resolve('ws://test-host' + path + '?ticket=T' + ticketN);
    },
    onEvent: (ev) => events.push(ev),
    onState: (s, d) => states.push([s, d]),
    onReplayGap: (sid) => gaps.push(sid),
    requestTimeoutMs: 80,
    heartbeatMs: 0,
    maxRetries: 2,
    backoffBaseMs: 5,
  }, opts || {}));
  const buildCalls = [];
  return { wire, events, states, gaps, buildCalls,
    get tickets() { return ticketN; } };
}

(async () => {
  // 1-2: connect uses buildWsUrl("/api/ws"); request frames are NDJSON.
  let t = makeWire();
  t.wire.connect();
  await sleep(20);
  check('buildWsUrl called with /api/ws', t.buildCalls.length === 1 && t.buildCalls[0] === '/api/ws', JSON.stringify(t.buildCalls));
  const ws = MockWS.instances[MockWS.instances.length - 1];
  check('ws opened with the ticket URL', ws.url === 'ws://test-host/api/ws?ticket=T1', ws.url);
  ws._open();
  await sleep(10);
  check('state open after open', t.states[t.states.length - 1][0] === 'open');
  const p1 = t.wire.request('session.create', { source: 'ceo-pack', idempotency_key: 'k1', close_on_disconnect: false });
  await sleep(5);
  check('frame is NDJSON with w1 id',
    ws.sent.length === 1 &&
    ws.sent[0] === '{"jsonrpc":"2.0","id":"w1","method":"session.create","params":{"source":"ceo-pack","idempotency_key":"k1","close_on_disconnect":false}}\n',
    ws.sent[0]);
  ws._msg('{"jsonrpc":"2.0","result":{"session_id":"s1"},"id":"w1"}\n');
  const r1 = await p1;
  check('response resolves with result', r1 && r1.session_id === 's1', JSON.stringify(r1));

  // 3: second request gets the next id.
  const p2 = t.wire.request('session.resume', { session_id: 's1' });
  await sleep(5);
  check('second request id is w2', ws.sent[1].indexOf('"id":"w2"') !== -1, ws.sent[1]);
  ws._msg('{"jsonrpc":"2.0","result":{"session_id":"s1","messages":[]},"id":"w2"}\n');
  await p2;

  // 4: chunked frames + two frames in one message.
  const p3 = t.wire.request('session.history', { session_id: 's1' });
  const p4 = t.wire.request('session.title', { session_id: 's1' });
  await sleep(5);
  ws._msg('{"jsonrpc":"2.0","result":{"count":0},"id":"w3"}');
  await sleep(5);
  ws._msg('\n{"jsonrpc":"2.0","result":{"title":"T"},"id":"w4"}\n');
  const [r3, r4] = await Promise.all([p3, p4]);
  check('split frame across two chunks resolves', r3 && r3.count === 0, JSON.stringify(r3));
  check('two frames in one message both resolve', r4 && r4.title === 'T', JSON.stringify(r4));

  // 5: event dispatch + seq watermark.
  ws._msg('{"jsonrpc":"2.0","method":"event","params":{"type":"message.delta","session_id":"s1","seq":7,"payload":{"text":"hi"}}}\n');
  await sleep(5);
  check('event dispatched with type/session/payload',
    t.events.length === 1 && t.events[0].type === 'message.delta' && t.events[0].session_id === 's1' && t.events[0].payload.text === 'hi',
    JSON.stringify(t.events));
  check('seq watermark tracked', t.wire.getLastSeq('s1') === 7, String(t.wire.getLastSeq('s1')));

  // 6: server->client request gets -32601, never hangs the backend.
  ws._msg('{"jsonrpc":"2.0","id":"srv1","method":"approval.respond","params":{}}\n');
  await sleep(5);
  const last = ws.sent[ws.sent.length - 1];
  check('unhandled server request answered -32601',
    last === '{"jsonrpc":"2.0","error":{"code":-32601,"message":"Method not found"},"id":"srv1"}\n', last);

  // 7: request timeout rejects.
  let timedOut = false;
  try { await t.wire.request('session.close', { session_id: 's1' }); } catch (e) { timedOut = /timed out/.test(e.message); }
  check('unanswered request times out', timedOut);

  // 8: drop -> bounded reconnect with a FRESH ticket per attempt, then give up.
  // A successful open resets the budget (standard backoff), so the
  // sequence is: drop -> retry (T2) -> open -> drop -> retry (T3) -> drop
  // before open -> retry (T4) -> drop -> failed. Four connects total.
  const callsBefore = t.buildCalls.length;
  ws._close(1006);
  await sleep(40);
  check('reconnect mints fresh tickets (buildWsUrl called again)',
    t.buildCalls.length > callsBefore, t.buildCalls.length + ' vs ' + callsBefore);
  const ws2 = MockWS.instances[MockWS.instances.length - 1];
  check('second connect uses a new ticket', ws2.url.indexOf('ticket=T2') !== -1, ws2.url);
  ws2._open();
  await sleep(10);
  ws2._close(1006);
  await sleep(40);
  const ws3 = MockWS.instances[MockWS.instances.length - 1];
  check('third connect uses a newer ticket', ws3.url.indexOf('ticket=T3') !== -1, ws3.url);
  ws3._close(1006);
  await sleep(40);
  const ws4 = MockWS.instances[MockWS.instances.length - 1];
  check('fourth connect uses a newer ticket', ws4.url.indexOf('ticket=T4') !== -1, ws4.url);
  ws4._close(1006);
  await sleep(40);
  check('gives up after the retry budget',
    t.states.some(([s]) => s === 'failed'), JSON.stringify(t.states.map(([s]) => s)));
  check('retry budget is bounded (4 connects total)',
    t.buildCalls.length === 4, String(t.buildCalls.length));

  // 9: 4401 fails fast with plain words and does NOT retry.
  let t9 = makeWire();
  t9.wire.connect();
  await sleep(20);
  const ws9 = MockWS.instances[MockWS.instances.length - 1];
  ws9._open();
  await sleep(10);
  ws9._close(4401);
  await sleep(30);
  const failed9 = t9.states.filter(([s]) => s === 'failed');
  check('4401 fails fast with sign-in-expired words',
    failed9.length === 1 && /sign-in expired/.test(failed9[0][1].message),
    JSON.stringify(failed9));
  check('4401 does not retry', t9.buildCalls.length === 1, String(t9.buildCalls.length));

  // 10: reconnect replays the gap via session.events.since; truncated -> gap.
  let t10 = makeWire();
  t10.wire.connect();
  await sleep(20);
  const ws10 = MockWS.instances[MockWS.instances.length - 1];
  ws10._open();
  await sleep(10);
  t10.wire.noteSession('s1', 'CFO');
  ws10._msg('{"jsonrpc":"2.0","method":"event","params":{"type":"message.delta","session_id":"s1","seq":12,"payload":{"text":"x"}}}\n');
  await sleep(5);
  ws10._close(1006);
  await sleep(40);
  const ws10b = MockWS.instances[MockWS.instances.length - 1];
  ws10b._open();
  await sleep(20);
  const replay = ws10b.sent.map((s) => { try { return JSON.parse(s); } catch (e) { return null; } })
    .find((f) => f && f.method === 'session.events.since');
  check('reconnect replays with the seq watermark',
    !!replay && replay.params.session_id === 's1' && replay.params.last_seen === 12 && replay.params.profile === 'CFO',
    JSON.stringify(replay));
  if (replay) {
    ws10b._msg(JSON.stringify({ jsonrpc: '2.0', result: { events: [], latest_seq: 12, truncated: true, count: 0, epoch: 'e1' }, id: replay.id }) + '\n');
    await sleep(10);
    check('truncated replay triggers the gap callback', t10.gaps.length === 1 && t10.gaps[0] === 's1', JSON.stringify(t10.gaps));
  } else {
    check('truncated replay triggers the gap callback', false, 'no replay request sent');
  }

  console.log(JSON.stringify(results));
})().catch((e) => {
  results.failed.push('harness crashed: ' + (e && e.stack || e));
  console.log(JSON.stringify(results));
});
"""


class ChatBinWireHarness(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is required for the wire harness")
    def test_vendored_client_under_node(self):
        """CeoChatWire, extracted verbatim from the shipped JS: framing,
        id map, chunked frames, event dispatch, -32601, timeout, bounded
        reconnect with fresh tickets, 4401 fast-fail, seq replay."""
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "chatbin-harness.js"
            harness.write_text(NODE_HARNESS, encoding="utf-8")
            proc = subprocess.run(
                ["node", str(harness), str(INDEX_JS)],
                capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0,
                         f"harness crashed: {proc.stderr[-2000:]}")
        results = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(results["failed"], [],
                         f"wire harness failures: {results['failed']}")
        self.assertGreaterEqual(len(results["passed"]), 15,
                                f"harness passed only: {results['passed']}")


if __name__ == "__main__":
    unittest.main()
