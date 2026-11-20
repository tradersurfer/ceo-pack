/**
 * CEO Pack — Hermes Desktop half (v1: embed).
 *
 * Plain ESM runtime plugin, loaded uncompiled by the Desktop app. No build
 * step, no Hermes repo changes. The Desktop app's sidebar does NOT read the
 * server's dashboard manifests (that mechanism feeds the web Dashboard UI
 * only) — its sidebar rows come from Desktop plugins, and this is that half.
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
 * What it does: adds a "CEO Pack" row to the Desktop sidebar and a /ceo-pack
 * page that frames the already-working served dashboard
 * (http://localhost:9119/ceo-pack) via the SDK's SandboxedFrame. No
 * dashboard logic is re-implemented here — the backend stays authoritative,
 * per the Desktop guide (agent behavior lives behind the gateway).
 *
 * Known v1 limits:
 *   - Gateway URL is hardcoded to localhost:9119 (Adrian's laptop gateway).
 *     If the gateway ever moves ports, this line moves with it.
 *   - The frame is a sandboxed, opaque-origin iframe, so it carries none of
 *     the Desktop app's auth. If the gateway login-walls it, the page shows
 *     the login screen instead of the dashboard — that settles in seconds on
 *     first load, and v2 is a native page over ctx.rest (authenticated
 *     through the app, same API the web dashboard uses).
 */

import { host, PALETTE_AREA, ROUTES_AREA, SandboxedFrame, SIDEBAR_NAV_AREA } from '@hermes/plugin-sdk'
import { jsx } from 'react/jsx-runtime'

const ID = 'ceo-pack'
const GATEWAY_URL = 'http://localhost:9119'

function CeoPackPage() {
  return jsx('div', {
    style: { width: '100%', height: '100%' },
    children: jsx(SandboxedFrame, {
      src: `${GATEWAY_URL}/ceo-pack`,
      title: 'CEO Pack',
      style: { width: '100%', height: '100%', border: 0, display: 'block' }
    })
  })
}

export default {
  id: ID, // must match the folder name
  name: 'CEO Pack',
  register(ctx) {
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
