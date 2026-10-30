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
import {
  navigationHostOptions,
  SESSION_HOST_PROTOCOL,
  supportsBrowser,
  type BrowserHostOptions,
} from "@/lib/nativeBridge";
import { fetchSessionHostId } from "@/lib/fetchSessionHostId";
import { authenticatedFetch } from "@/lib/identity";
import { useQueryClient } from "@tanstack/react-query";
import { isArcaHost, readArcaHostId, writeArcaHostId } from "@/lib/arcaHost";
import type { Host } from "@/hooks/useHosts";

/** Subset of `window.omnigentDesktop` the relay calls (typed locally, not via
 *  nativeBridge). All optional — an older shell may predate the feature, so the
 *  relay feature-detects before calling. */
interface BrowserDesktopBridge {
  browserOpenOrNavigate?: (
    conversationId: string,
    url: string,
    bounds?: unknown,
    opts?: BrowserHostOptions & {
      force?: boolean;
      agent?: boolean;
      rememberedArcaHostId?: string;
      sourceIsArcaHost?: boolean;
    },
  ) => Promise<{ ok: boolean; created?: boolean; hostIsThisMachine?: boolean; error?: string }>;
  browserHasView?: (
    conversationId: string,
  ) => Promise<{ exists: boolean; hostIsThisMachine?: boolean }>;
  browserScreenshot?: (
    conversationId: string,
    opts?: BrowserHostOptions,
  ) => Promise<{ ok: boolean; dataUrl?: string; error?: string }>;
  /** @deprecated Compatibility with pre-typed desktops until v0.21. */
  browserExecute?: (
    conversationId: string,
    script: string,
    opts?: BrowserHostOptions,
  ) => Promise<{ ok: boolean; result?: string; error?: string }>;
  browserPerformAction?: (
    conversationId: string,
    action: "snapshot" | "click" | "type",
    args?: Record<string, unknown>,
    opts?: BrowserHostOptions,
  ) => Promise<{ ok: boolean; result?: string; error?: string }>;
}

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

async function performAction(
  desktop: BrowserDesktopBridge,
  conversationId: string,
  action: "snapshot" | "click" | "type",
  args?: Record<string, unknown>,
  opts?: BrowserHostOptions,
) {
  if (desktop.browserPerformAction) {
    return opts
      ? desktop.browserPerformAction(conversationId, action, args, opts)
      : desktop.browserPerformAction(conversationId, action, args);
  }
  // Pre-typed desktops retain their existing executor until the v0.21 transition.
  if (desktop.browserExecute) {
    const script = buildBrowserActionScript(action, args);
    return opts
      ? desktop.browserExecute(conversationId, script, opts)
      : desktop.browserExecute(conversationId, script);
  }
  return { ok: false, error: "this desktop shell does not support the browser pane" };
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
  sourceHostId: string | null,
  rememberedArcaHostId: string | null,
  sourceIsArcaHost: boolean,
  readHostOptions?: BrowserHostOptions,
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
          ...navigationHostOptions(sourceHostId),
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
      case "screenshot": {
        if (!desktop.browserScreenshot) {
          return { ok: false, error: "this desktop shell does not support the browser pane" };
        }
        const r = readHostOptions
          ? await desktop.browserScreenshot(conversationId, readHostOptions)
          : await desktop.browserScreenshot(conversationId);
        if (!r?.ok || !r.dataUrl) return { ok: false, error: r?.error ?? "No browser open" };
        return { ok: true, data_url: r.dataUrl };
      }
      case "snapshot": {
        const r = await performAction(
          desktop,
          conversationId,
          "snapshot",
          undefined,
          readHostOptions,
        );
        if (!r?.ok) return { ok: false, error: r?.error ?? "snapshot failed" };
        try {
          const parsed = JSON.parse(r.result ?? "{}") as Record<string, unknown>;
          return { ok: true, data: parsed };
        } catch (e) {
          return { ok: false, error: `snapshot parse failed: ${(e as Error).message}` };
        }
      }
      case "click": {
        const r = await performAction(desktop, conversationId, "click", args, readHostOptions);
        if (!r?.ok) return { ok: false, error: r?.error ?? "click failed" };
        return { ok: true };
      }
      case "type": {
        const r = await performAction(desktop, conversationId, "type", args, readHostOptions);
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
      const needsHostRevalidation = ["screenshot", "snapshot", "click", "type"].includes(
        evt.action,
      );
      let privilegedView = true;
      if (needsHostRevalidation && desktop.browserHasView) {
        try {
          const view = await desktop.browserHasView(sourceConversationId);
          privilegedView = view.hostIsThisMachine === true;
        } catch {
          // A failed probe requires current host metadata before acting.
        }
      }
      const revalidateHost = needsHostRevalidation && privilegedView;
      const sourceHostId =
        evt.action === "navigate" || revalidateHost
          ? await fetchSessionHostId(sourceConversationId, { queryClient })
          : null;
      const readHostOptions = revalidateHost
        ? { sessionHostProtocol: SESSION_HOST_PROTOCOL, sourceHostId }
        : undefined;
      const rememberedArcaHostId = evt.action === "navigate" ? readArcaHostId() : null;
      const sourceIsArcaHost =
        evt.action === "navigate" && sourceHostId !== null
          ? queryClient
              .getQueriesData<Host[]>({ queryKey: ["hosts"] })
              .flatMap(([, rows]) => rows ?? [])
              .some(
                (host) => host.host_id === sourceHostId && isArcaHost(host, rememberedArcaHostId),
              )
          : false;
      const result = cancelled
        ? { ok: false, error: "browser relay context changed" }
        : await dispatch(
            sourceConversationId,
            evt.action,
            evt.args,
            desktop,
            sourceHostId,
            rememberedArcaHostId,
            sourceIsArcaHost,
            readHostOptions,
          );
      await postResult(sourceConversationId, evt.actionId, claimToken, result);
    };

    const unsubscribe = onBrowserActionRequest(handler);
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [relayEnabled, queryClient]);
}
