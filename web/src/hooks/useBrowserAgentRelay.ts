/** Browser MCP relay hook — runs agent-issued browser actions against the
 *  Electron WebContentsView via a claim-first protocol: on a
 *  `browser.action_request` SSE event every renderer races to CLAIM the action
 *  (atomic CAS on the server), and only the `{claimed:true, claim_token}` winner
 *  dispatches to `window.omnigentDesktop.browser*` and POSTs the result back
 *  with its token — so multiple windows can't double-execute.
 *  Gated on `isElectronShell()`: without a WebContentsView the hook registers
 *  nothing and actions time out cleanly (no headless fallback). */
import { useEffect } from "react";
import { onBrowserActionRequest } from "@/lib/browserActionBus";
import type { BrowserActionRequestEvent } from "@/lib/events";
import { isElectronShell } from "@/lib/nativeBridge";
import { authenticatedFetch } from "@/lib/identity";

/** Subset of `window.omnigentDesktop` the relay calls (typed locally, not via
 *  nativeBridge). All optional — an older shell may predate the feature, so the
 *  relay feature-detects before calling. */
interface BrowserDesktopBridge {
  browserOpenOrNavigate?: (
    conversationId: string,
    url: string,
    bounds?: unknown,
    opts?: { force?: boolean; agent?: boolean },
  ) => Promise<{ ok: boolean; created?: boolean; error?: string }>;
  browserScreenshot?: (
    conversationId: string,
  ) => Promise<{ ok: boolean; dataUrl?: string; error?: string }>;
  browserExecute?: (
    conversationId: string,
    js: string,
  ) => Promise<{ ok: boolean; result?: string; error?: string }>;
}

function getBrowserDesktop(): BrowserDesktopBridge | null {
  if (!isElectronShell()) return null;
  const w = window as unknown as { omnigentDesktop?: BrowserDesktopBridge };
  return w.omnigentDesktop ?? null;
}

/** The shape the relay POSTs back to the server as the action `result`. Normalized
 *  so the backend/tool layer can branch on `ok` alone. */
interface ActionResult {
  ok: boolean;
  error?: string;
  data?: Record<string, unknown>;
  data_url?: string;
}

/** Safely interpolate a string into a `browserExecute` JS payload — always
 *  `JSON.stringify` (handles the full escape table); never hand-roll. */
function jsString(s: string): string {
  return JSON.stringify(s);
}

/** Same for numbers — stringify at the interpolation site so a caller that
 *  skips the shape guard still emits a valid in-page literal. */
function jsNumber(n: number): string {
  return JSON.stringify(n);
}

/** In-page JS producing an a11y-style tree with stable `[ref=N]` ids. Refs live
 *  in `window.__omni_refs__` (resolved by click/type) under a per-snapshot
 *  `__omni_snapshot_id__`, so a stale ref is rejected with a precise error.
 *  Picks interactive/landmark/heading/list elements (skips hidden/zero-area);
 *  accessible name = aria-label > alt > placeholder > title > text, ≤80 chars.
 *  Snapshot REPLACES the ref map — two agents on one page collide (known limit,
 *  but the snapshot_id check turns a mis-click into a clean error).
 *  Returns JSON: `{ snapshot_id, url, title, tree }`. */
const SNAPSHOT_JS = `(() => {
  const snapshotId = (crypto && crypto.randomUUID) ? crypto.randomUUID() : ('snap-' + Date.now() + '-' + Math.random().toString(36).slice(2, 10));
  window.__omni_refs__ = new Map();
  window.__omni_snapshot_id__ = snapshotId;
  let nextRef = 0;
  const lines = [];

  const TAG_ROLE = {
    a: 'link', button: 'button', textarea: 'textbox', select: 'combobox',
    nav: 'navigation', main: 'main', header: 'banner', footer: 'contentinfo',
    form: 'form', section: 'region', article: 'article', aside: 'complementary',
    ul: 'list', ol: 'list', li: 'listitem', img: 'image', label: 'label',
    table: 'table', tr: 'row', td: 'cell', th: 'columnheader',
    dialog: 'dialog', summary: 'button', details: 'group',
  };
  const isInteractiveTag = (tag) =>
    tag === 'a' || tag === 'button' || tag === 'input' ||
    tag === 'textarea' || tag === 'select' || tag === 'label' ||
    tag === 'summary';
  const isHeading = (tag) => tag && tag.length === 2 && tag[0] === 'h' && tag[1] >= '1' && tag[1] <= '6';

  function getRole(el) {
    const explicit = el.getAttribute && el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName && el.tagName.toLowerCase();
    if (!tag) return null;
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'submit' || t === 'button' || t === 'reset') return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'range') return 'slider';
      if (t === 'hidden') return null;
      return 'textbox';
    }
    if (isHeading(tag)) return 'heading';
    if (TAG_ROLE[tag]) return TAG_ROLE[tag];
    if (el.hasAttribute && (el.hasAttribute('tabindex') || el.hasAttribute('onclick'))) return 'generic';
    return null;
  }

  function getName(el, depth = 0) {
    if (depth > 4) return '';
    if (el.getAttribute) {
      const lbl = el.getAttribute('aria-label');
      if (lbl) return lbl;
      const alt = el.getAttribute('alt');
      if (alt) return alt;
      const ph = el.getAttribute('placeholder');
      if (ph) return ph;
      const ttl = el.getAttribute('title');
      if (ttl) return ttl;
    }
    const tag = el.tagName && el.tagName.toLowerCase();
    if (tag === 'input' && el.value && el.type !== 'password') return String(el.value).slice(0, 80);
    let text = '';
    for (const node of el.childNodes) {
      if (node.nodeType === 3) text += node.textContent;
      else if (node.nodeType === 1) {
        const childTag = node.tagName && node.tagName.toLowerCase();
        if (depth === 0 && isInteractiveTag(childTag)) continue;
        text += getName(node, depth + 1);
      }
      if (text.length > 80) break;
    }
    return text.trim().replace(/\\s+/g, ' ').slice(0, 80);
  }

  function isVisible(el) {
    if (!el.getBoundingClientRect) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    if (style.opacity === '0') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function walk(el, depth) {
    if (!(el instanceof Element)) return;
    if (!isVisible(el)) return;
    const role = getRole(el);
    const picked = role !== null && role !== 'generic';
    let childDepth = depth;
    if (picked) {
      const ref = ++nextRef;
      window.__omni_refs__.set(ref, new WeakRef(el));
      const name = getName(el);
      const indent = '  '.repeat(depth);
      let line = indent + '- ' + role;
      if (name) line += ' ' + JSON.stringify(name);
      line += ' [ref=' + ref + ']';
      lines.push(line);
      childDepth = depth + 1;
    }
    for (const child of el.children) {
      walk(child, childDepth);
    }
  }

  if (document.body) walk(document.body, 0);

  return JSON.stringify({
    snapshot_id: snapshotId,
    url: window.location.href,
    title: document.title,
    tree: lines.join('\\n'),
  });
})()`;

/** Build the "find element" preamble for an action that accepts EITHER ref OR
 *  selector. Sets `el` in the in-page scope; throws on miss.
 *
 *  When `snapshot_id` is provided (recommended), the resolver validates it
 *  matches `window.__omni_snapshot_id__` BEFORE looking up the ref — gives the
 *  agent a precise "snapshot superseded" error instead of a generic "ref is
 *  stale". */
function findElJs(args: Record<string, unknown>): string {
  const ref = typeof args.ref === "number" ? args.ref : undefined;
  const snapshotId = typeof args.snapshot_id === "string" ? args.snapshot_id : undefined;
  const selector = typeof args.selector === "string" ? args.selector : "";
  if (ref !== undefined) {
    const idCheck = snapshotId
      ? `if (window.__omni_snapshot_id__ !== ${jsString(snapshotId)}) ` +
        `throw new Error('snapshot ' + ${jsString(snapshotId)} + ' was superseded by ' + (window.__omni_snapshot_id__ || '(none)') + ' — call browser_snapshot again'); `
      : "";
    return (
      idCheck +
      `const el = (window.__omni_refs__ && window.__omni_refs__.get(${jsNumber(ref)}))?.deref(); ` +
      // Distinguish no-snapshot / ref-missing / GC'd so the agent's retry is specific.
      `if (!window.__omni_refs__) throw new Error('no snapshot in this page — call browser_snapshot first'); ` +
      `if (!window.__omni_refs__.has(${jsNumber(ref)})) throw new Error('ref ' + ${jsNumber(ref)} + ' not in snapshot — call browser_snapshot again'); ` +
      `if (!el) throw new Error('ref ' + ${jsNumber(ref)} + ' was garbage-collected — call browser_snapshot again'); `
    );
  }
  return (
    `const el = document.querySelector(${jsString(selector)}); ` +
    `if (!el) throw new Error('selector not found: ' + ${jsString(selector)}); `
  );
}

/** Execute one claimed action against the conversation's WebContentsView.
 *  `conversationId` targets the right view; `desktop` is the (feature-detected)
 *  bridge. Returns a normalized `ActionResult` — never throws (the outer catch
 *  converts any in-page/IPC error to `{ok:false, error}`). */
async function dispatch(
  conversationId: string,
  action: string,
  args: Record<string, unknown>,
  desktop: BrowserDesktopBridge,
): Promise<ActionResult> {
  try {
    switch (action) {
      case "navigate": {
        const url = String(args.url ?? "");
        if (!url) return { ok: false, error: "url is required" };
        if (!desktop.browserOpenOrNavigate) {
          return { ok: false, error: "this desktop shell does not support the browser pane" };
        }
        // force: honor the explicit agent nav even on same-URL. agent: mark it
        // model-issued so the registry applies the scheme/host allowlist (Risk).
        const r = await desktop.browserOpenOrNavigate(conversationId, url, undefined, {
          force: true,
          agent: true,
        });
        if (!r?.ok) return { ok: false, error: r?.error ?? "navigate failed" };
        return { ok: true, data: { final_url: url } };
      }
      case "screenshot": {
        if (!desktop.browserScreenshot) {
          return { ok: false, error: "this desktop shell does not support the browser pane" };
        }
        const r = await desktop.browserScreenshot(conversationId);
        if (!r?.ok || !r.dataUrl) return { ok: false, error: r?.error ?? "No browser open" };
        return { ok: true, data_url: r.dataUrl };
      }
      case "snapshot": {
        if (!desktop.browserExecute) {
          return { ok: false, error: "this desktop shell does not support the browser pane" };
        }
        const r = await desktop.browserExecute(conversationId, SNAPSHOT_JS);
        if (!r?.ok) return { ok: false, error: r?.error ?? "snapshot failed" };
        try {
          const parsed = JSON.parse(r.result ?? "{}") as Record<string, unknown>;
          return { ok: true, data: parsed };
        } catch (e) {
          return { ok: false, error: `snapshot parse failed: ${(e as Error).message}` };
        }
      }
      case "click": {
        if (!desktop.browserExecute) {
          return { ok: false, error: "this desktop shell does not support the browser pane" };
        }
        const js =
          `(() => { ${findElJs(args)} ` +
          `el.scrollIntoView({ block: 'center', inline: 'center' }); ` +
          `el.click(); return 'ok'; })()`;
        const r = await desktop.browserExecute(conversationId, js);
        if (!r?.ok) return { ok: false, error: r?.error ?? "click failed" };
        return { ok: true };
      }
      case "type": {
        if (!desktop.browserExecute) {
          return { ok: false, error: "this desktop shell does not support the browser pane" };
        }
        const text = String(args.text ?? "");
        const js =
          `(() => { ${findElJs(args)} ` +
          `el.focus(); ` +
          `const setter = Object.getOwnPropertyDescriptor(el.constructor.prototype, 'value')?.set; ` +
          `if (setter) setter.call(el, ${jsString(text)}); else el.value = ${jsString(text)}; ` +
          `el.dispatchEvent(new Event('input', { bubbles: true })); ` +
          `el.dispatchEvent(new Event('change', { bubbles: true })); ` +
          `return 'ok'; })()`;
        const r = await desktop.browserExecute(conversationId, js);
        if (!r?.ok) return { ok: false, error: r?.error ?? "type failed" };
        return { ok: true };
      }
      default:
        return { ok: false, error: `Unknown browser action: ${action}` };
    }
  } catch (e) {
    return { ok: false, error: (e as Error).message };
  }
}

/** POST the atomic claim FIRST. The server does a check-and-set: the winning
 *  renderer gets `{claimed:true, claim_token}`; losers get `{claimed:false}`
 *  and drop the action. Returns the claim token, or null when this renderer did
 *  not win (or the claim call failed — treat as "not ours"). */
async function claimAction(conversationId: string, actionId: string): Promise<string | null> {
  try {
    const resp = await authenticatedFetch(
      `/v1/sessions/${encodeURIComponent(conversationId)}/browser/action_claim/${encodeURIComponent(actionId)}`,
      { method: "POST" },
    );
    if (!resp.ok) return null;
    const body = (await resp.json()) as { claimed?: boolean; claim_token?: string };
    if (body?.claimed && typeof body.claim_token === "string") return body.claim_token;
    return null;
  } catch (e) {
    console.warn("[browser-relay] claim failed", e);
    return null;
  }
}

/** POST the action result WITH the claim token so the server can resolve the
 *  parked Future (and reject any tokenless / mismatched attempt). Best-effort:
 *  a network blip here surfaces to the agent as the server's action timeout. */
async function postResult(
  conversationId: string,
  actionId: string,
  claimToken: string,
  result: ActionResult,
): Promise<void> {
  try {
    await authenticatedFetch(
      `/v1/sessions/${encodeURIComponent(conversationId)}/browser/action_result/${encodeURIComponent(actionId)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ result, claim_token: claimToken }),
      },
    );
  } catch (e) {
    // Backend blip — the server's action timeout surfaces it to the agent; log for maintainers.
    console.warn("[browser-relay] POST result failed", e);
  }
}

/**
 * Register the embedded-browser relay for a conversation. No-op outside Electron.
 * Mount ONE instance per active conversation (typically from `BrowserPane`).
 *
 * @param conversationId The conversation whose WebContentsView this relay drives.
 */
export function useBrowserAgentRelay(conversationId: string | null | undefined): void {
  useEffect(() => {
    if (!conversationId) return;
    if (!isElectronShell()) return;

    const handler = async (evt: BrowserActionRequestEvent) => {
      const desktop = getBrowserDesktop();
      if (!desktop) return; // not the Electron shell — nothing to claim
      // Claim FIRST — only the winner proceeds, so two windows can't double-execute.
      const claimToken = await claimAction(conversationId, evt.actionId);
      if (!claimToken) return;
      const result = await dispatch(conversationId, evt.action, evt.args, desktop);
      await postResult(conversationId, evt.actionId, claimToken, result);
    };

    return onBrowserActionRequest(handler);
  }, [conversationId]);
}
