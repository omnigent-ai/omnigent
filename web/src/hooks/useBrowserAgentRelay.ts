/** Browser MCP relay hook — runs agent-issued browser actions against the
 *  Electron WebContentsView via a claim-first protocol: on a
 *  `browser.action_request` SSE event every renderer races to CLAIM the action
 *  (atomic CAS on the server), and only the `{claimed:true, claim_token}` winner
 *  dispatches to `window.omnigentDesktop.browser*` and POSTs the result back
 *  with its token — so multiple windows can't double-execute.
 *  Gated on `supportsBrowser()`: without a WebContentsView the hook registers
 *  nothing and actions time out cleanly (no headless fallback). An older
 *  desktop build that predates the `browser*` bridge is treated as unsupported,
 *  so the relay never claims an action it couldn't fulfill. */
import { useEffect } from "react";
import { buildBrowserActionScript } from "../../electron/src/browserActionScript.mjs";
import { onBrowserActionRequest } from "@/lib/browserActionBus";
import type { BrowserActionRequestEvent } from "@/lib/events";
import { supportsBrowser } from "@/lib/nativeBridge";
import { authenticatedFetch } from "@/lib/identity";
import { useQueryClient } from "@tanstack/react-query";
import { sessionRoutingHostId, setSessionHost, setSessionParent } from "@/lib/sessionHost";
import { getSessionSlim } from "@/lib/sessionsApi";
import { isArcaHost, readArcaHostId, writeArcaHostId } from "@/lib/arcaHost";
import type { Host } from "@/hooks/useHosts";
import type { Session } from "@/lib/types";
import {
  addBrowserTab,
  agentBrowserTabId,
  browserTabIds,
  browserViewId,
  closeBrowserTab,
  selectAgentBrowserTab,
} from "@/hooks/useBrowserTabs";

/** Subset of `window.omnigentDesktop` the relay calls (typed locally, not via
 *  nativeBridge). All optional — an older shell may predate the feature, so the
 *  relay feature-detects before calling. */
interface BrowserDesktopBridge {
  browserOpenOrNavigate?: (
    conversationId: string,
    url: string,
    bounds?: unknown,
    opts?: {
      force?: boolean;
      agent?: boolean;
      sourceHostId?: string;
      rememberedArcaHostId?: string;
      sourceIsArcaHost?: boolean;
    },
  ) => Promise<{ ok: boolean; created?: boolean; error?: string }>;
  browserScreenshot?: (
    conversationId: string,
  ) => Promise<{ ok: boolean; dataUrl?: string; error?: string }>;
  /** @deprecated Compatibility with pre-typed desktops until v0.21. */
  browserExecute?: (
    conversationId: string,
    script: string,
  ) => Promise<{ ok: boolean; result?: string; error?: string }>;
  browserPerformAction?: (
    conversationId: string,
    action: string,
    args?: Record<string, unknown>,
  ) => Promise<{ ok: boolean; result?: string; error?: string }>;
  browserHasView?: (
    conversationId: string,
  ) => Promise<{ exists: boolean; url?: string; title?: string }>;
}

/** Where a navigation comes from, for main's loopback-eligibility check. */
interface NavigationSource {
  sourceHostId: string | null;
  rememberedArcaHostId: string | null;
  sourceIsArcaHost: boolean;
}

// Pre-typed desktops can only run these through the v0.18 script executor.
type LegacyAction = "snapshot" | "click" | "type";
const LEGACY_ACTIONS = new Set<string>(["snapshot", "click", "type"]);
const isLegacyAction = (action: string): action is LegacyAction => LEGACY_ACTIONS.has(action);
// Actions main performs and answers with a JSON object.
const DESKTOP_ACTIONS = new Set([
  "go_back",
  "go_forward",
  "reload",
  "hover",
  "drag",
  "scroll",
  "press_key",
  "select_option",
  "check",
]);

function isLoopbackPreview(url: string): boolean {
  try {
    const { hostname } = new URL(url);
    return (
      hostname === "localhost" ||
      hostname.endsWith(".localhost") ||
      hostname === "127.0.0.1" ||
      hostname === "[::1]"
    );
  } catch {
    return false;
  }
}

function getBrowserDesktop(): BrowserDesktopBridge | null {
  if (!supportsBrowser()) return null;
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

function unsupportedActionError(action: string): string {
  return `this Omnigent desktop app does not support browser_${action}; update the app to use it`;
}

async function performAction(
  desktop: BrowserDesktopBridge,
  conversationId: string,
  action: string,
  args?: Record<string, unknown>,
) {
  if (desktop.browserPerformAction) {
    const r = await desktop.browserPerformAction(conversationId, action, args);
    // An older shell's main process rejects actions it predates with this message.
    if (!r?.ok && r?.error === "Unsupported browser action" && !LEGACY_ACTIONS.has(action)) {
      return { ok: false, error: unsupportedActionError(action) };
    }
    return r;
  }
  // Pre-typed desktops retain their existing executor until the v0.21 transition.
  if (desktop.browserExecute && isLegacyAction(action)) {
    return desktop.browserExecute(conversationId, buildBrowserActionScript(action, args));
  }
  if (desktop.browserExecute) return { ok: false, error: unsupportedActionError(action) };
  return { ok: false, error: "this desktop shell does not support the browser pane" };
}

/** Parse main's JSON answer into the result's `data`, dropping its own `ok`. */
function actionData(result: string | undefined): Record<string, unknown> | undefined {
  try {
    const parsed: unknown = JSON.parse(result ?? "");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return undefined;
    const { ok: _ok, ...data } = parsed as Record<string, unknown>;
    return Object.keys(data).length > 0 ? data : undefined;
  } catch {
    return undefined;
  }
}

async function navigateView(
  desktop: BrowserDesktopBridge,
  viewId: string,
  url: string,
  source: NavigationSource,
): Promise<ActionResult> {
  if (!desktop.browserOpenOrNavigate) {
    return { ok: false, error: "this desktop shell does not support the browser pane" };
  }
  const { sourceHostId, rememberedArcaHostId, sourceIsArcaHost } = source;
  // force: honor the explicit agent nav even on same-URL. agent: mark it
  // model-issued so the registry applies the scheme/host allowlist (Risk).
  const r = await desktop.browserOpenOrNavigate(viewId, url, undefined, {
    force: true,
    agent: true,
    ...(sourceHostId ? { sourceHostId } : {}),
    ...(rememberedArcaHostId ? { rememberedArcaHostId } : {}),
    ...(sourceIsArcaHost ? { sourceIsArcaHost: true } : {}),
  });
  if (!r?.ok) return { ok: false, error: r?.error ?? "navigate failed" };
  if (
    isLoopbackPreview(url) &&
    sourceHostId &&
    rememberedArcaHostId &&
    sourceHostId !== rememberedArcaHostId
  ) {
    // Main only approves this after consent captures the source host.
    writeArcaHostId(sourceHostId);
  }
  return { ok: true, data: { final_url: url } };
}

/** `browser_tabs`: list, open, select or close the session's browser tabs. */
async function manageTabs(
  sessionId: string,
  args: Record<string, unknown>,
  desktop: BrowserDesktopBridge,
  source: NavigationSource,
): Promise<ActionResult> {
  const tabId = typeof args.tab_id === "string" ? args.tab_id : undefined;
  switch (args.action) {
    case "list": {
      const current = agentBrowserTabId(sessionId);
      const tabs = await Promise.all(
        browserTabIds(sessionId).map(async (id) => {
          const view = await desktop
            .browserHasView?.(browserViewId(sessionId, id))
            .catch(() => undefined);
          return {
            tab_id: id,
            url: view?.exists ? (view.url ?? "") : "",
            title: view?.exists ? (view.title ?? "") : "",
            current: id === current,
          };
        }),
      );
      return { ok: true, data: { tabs, current_tab_id: current } };
    }
    case "new": {
      const url = typeof args.url === "string" ? args.url : "";
      const created = addBrowserTab(sessionId, { forAgent: true });
      if (url) {
        const opened = await navigateView(desktop, browserViewId(sessionId, created), url, source);
        if (!opened.ok) return { ...opened, data: { tab_id: created } };
      }
      return { ok: true, data: { tab_id: created, ...(url ? { url } : {}) } };
    }
    case "select": {
      if (!tabId) return { ok: false, error: "tab_id is required to select a tab" };
      if (!selectAgentBrowserTab(sessionId, tabId)) {
        return { ok: false, error: `no open tab ${tabId}; call browser_tabs with action list` };
      }
      return { ok: true, data: { tab_id: tabId } };
    }
    case "close": {
      const target = tabId ?? agentBrowserTabId(sessionId);
      if (!browserTabIds(sessionId).includes(target)) {
        return { ok: false, error: `no open tab ${target}; call browser_tabs with action list` };
      }
      if (!(await closeBrowserTab(sessionId, target))) {
        return { ok: false, error: `tab ${target} did not close` };
      }
      return {
        ok: true,
        data: { closed_tab_id: target, current_tab_id: agentBrowserTabId(sessionId) },
      };
    }
    default:
      return { ok: false, error: "action must be one of list, new, select, close" };
  }
}

/** Execute one claimed action against the session's current agent browser tab.
 *  `desktop` is the (feature-detected) bridge. Returns a normalized
 *  `ActionResult` — never throws (the outer catch converts any in-page/IPC
 *  error to `{ok:false, error}`). */
async function dispatch(
  sessionId: string,
  action: string,
  args: Record<string, unknown>,
  desktop: BrowserDesktopBridge,
  source: NavigationSource,
): Promise<ActionResult> {
  const conversationId = browserViewId(sessionId, agentBrowserTabId(sessionId));
  try {
    if (DESKTOP_ACTIONS.has(action)) {
      const r = await performAction(desktop, conversationId, action, args);
      if (!r?.ok) return { ok: false, error: r?.error ?? `${action} failed` };
      const data = actionData(r.result);
      return data ? { ok: true, data } : { ok: true };
    }
    switch (action) {
      case "navigate": {
        const url = String(args.url ?? "");
        if (!url) return { ok: false, error: "url is required" };
        return await navigateView(desktop, conversationId, url, source);
      }
      case "tabs":
        return await manageTabs(sessionId, args, desktop, source);
      case "screenshot": {
        if (!desktop.browserScreenshot) {
          return { ok: false, error: "this desktop shell does not support the browser pane" };
        }
        const r = await desktop.browserScreenshot(conversationId);
        if (!r?.ok || !r.dataUrl) return { ok: false, error: r?.error ?? "No browser open" };
        return { ok: true, data_url: r.dataUrl };
      }
      case "snapshot": {
        const r = await performAction(desktop, conversationId, "snapshot");
        if (!r?.ok) return { ok: false, error: r?.error ?? "snapshot failed" };
        try {
          const parsed = JSON.parse(r.result ?? "{}") as Record<string, unknown>;
          return { ok: true, data: parsed };
        } catch (e) {
          return { ok: false, error: `snapshot parse failed: ${(e as Error).message}` };
        }
      }
      case "click": {
        // Defaults stay off the wire so older shells still take a plain click.
        const { button, double_click: doubleClick, ...target } = args;
        const mouseInput = (button !== undefined && button !== "left") || doubleClick === true;
        const clickArgs = mouseInput ? args : target;
        const r = await performAction(desktop, conversationId, "click", clickArgs);
        if (!r?.ok && mouseInput && r?.error === "Unsupported browser action argument") {
          return { ok: false, error: unsupportedActionError("click with button or double_click") };
        }
        if (!r?.ok) return { ok: false, error: r?.error ?? "click failed" };
        return { ok: true };
      }
      case "type": {
        const r = await performAction(desktop, conversationId, "type", args);
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
 * Register the embedded-browser relay. No-op outside Electron. Mounted for the
 * active conversation (from `AppShell`), but it serves EVERY conversation's
 * actions: an action is claimed, dispatched, and answered against the session
 * whose stream delivered it (`sourceConversationId`), not the mounted one — a
 * background conversation can issue a browser action while another is on screen,
 * and its WebContentsView is keyed by that session. Claiming at the mounted
 * (visible) session would be rejected by the server as an owner mismatch.
 *
 * @param conversationId Gate only — the relay registers while a conversation is
 *   open. Routing uses the delivering conversation, not this.
 */
export function useBrowserAgentRelay(conversationId: string | null | undefined): void {
  const relayEnabled = !!conversationId;
  const queryClient = useQueryClient();
  useEffect(() => {
    if (!relayEnabled) return;
    if (!supportsBrowser()) return;

    let cancelled = false;
    const handler = async (evt: BrowserActionRequestEvent, sourceConversationId: string | null) => {
      if (!sourceConversationId) return; // no delivering session — nothing to target
      const desktop = getBrowserDesktop();
      if (!desktop) return; // no browser-capable shell — nothing to claim
      // Claim FIRST — only the winner proceeds, so two windows can't double-execute.
      const claimToken = await claimAction(sourceConversationId, evt.actionId);
      if (!claimToken) return;
      let sourceHostId: string | null = null;
      // Tabs can open a URL, so they establish provenance like a navigation.
      const navigates = evt.action === "navigate" || evt.action === "tabs";
      if (navigates) {
        try {
          // Parent hints cannot establish which ancestor has the nearest own host binding.
          const visited = new Set<string>();
          let id: string | null = sourceConversationId;
          while (id !== null && !visited.has(id)) {
            visited.add(id);
            const hopId: string = id;
            // oxlint-disable-next-line no-await-in-loop -- Each parent comes from the previous snapshot.
            const source: Session = await queryClient.fetchQuery({
              queryKey: ["session", hopId],
              queryFn: () => getSessionSlim(hopId),
              staleTime: Infinity,
              retry: false,
            });
            setSessionHost(source.id, sessionRoutingHostId(source.hostId, source.labels));
            setSessionParent(source.id, source.parentSessionId);
            if (source.hostId) {
              sourceHostId = source.hostId;
              break;
            }
            id = source.parentSessionId;
          }
        } catch {
          // Unknown provenance stays denied for localhost; public browsing still works.
        }
      }
      const rememberedArcaHostId = navigates ? readArcaHostId() : null;
      const sourceIsArcaHost =
        navigates && sourceHostId !== null
          ? queryClient
              .getQueriesData<Host[]>({ queryKey: ["hosts"] })
              .flatMap(([, rows]) => rows ?? [])
              .some(
                (host) => host.host_id === sourceHostId && isArcaHost(host, rememberedArcaHostId),
              )
          : false;
      const result = cancelled
        ? { ok: false, error: "browser relay context changed" }
        : await dispatch(sourceConversationId, evt.action, evt.args, desktop, {
            sourceHostId,
            rememberedArcaHostId,
            sourceIsArcaHost,
          });
      await postResult(sourceConversationId, evt.actionId, claimToken, result);
    };

    const unsubscribe = onBrowserActionRequest(handler);
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [relayEnabled, queryClient]);
}
