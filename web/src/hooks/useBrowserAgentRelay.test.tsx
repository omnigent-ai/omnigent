import { renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// supportsBrowser gates the whole relay; force it true so the hook registers.
vi.mock("@/lib/nativeBridge", () => ({
  isElectronShell: () => true,
  supportsBrowser: () => true,
}));

// The relay POSTs claim + result through authenticatedFetch; mock it so we can
// script the claim response and inspect the result POST body.
const authenticatedFetch = vi.fn();
const sessionHosts = new Map<string, string>();
const sessionParents = new Map<string, string>();
const sessionSnapshots = new Map<
  string,
  { id: string; hostId: string | null; parentSessionId: string | null }
>();
const getSessionSlim = vi.fn(async (id: string) => sessionSnapshots.get(id)!);
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: (...args: unknown[]) => authenticatedFetch(...args),
}));
vi.mock("@/lib/sessionHost", () => ({
  getSessionHost: (id: string) => {
    const visited = new Set<string>();
    let currentId = id;
    while (!visited.has(currentId)) {
      visited.add(currentId);
      const host = sessionHosts.get(currentId);
      if (host) return host;
      const parent = sessionParents.get(currentId);
      if (!parent) return null;
      currentId = parent;
    }
    return null;
  },
  setSessionHost: (id: string, host: string | null) =>
    host ? sessionHosts.set(id, host) : sessionHosts.delete(id),
  setSessionParent: (id: string, parent: string | null) =>
    parent ? sessionParents.set(id, parent) : sessionParents.delete(id),
}));
vi.mock("@/lib/sessionsApi", () => ({ getSessionSlim: (id: string) => getSessionSlim(id) }));

import { emitBrowserActionRequest } from "@/lib/browserActionBus";
import type { BrowserActionRequestEvent } from "@/lib/events";
import { useBrowserAgentRelay } from "./useBrowserAgentRelay";

const CONV = "conv_relay";

function renderRelay(conversationId: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderHook(() => useBrowserAgentRelay(conversationId), {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
}

/** Build a `browser.action_request` event for the bus. */
function actionEvent(
  action: string,
  args: Record<string, unknown> = {},
  actionId = "baction_1",
): BrowserActionRequestEvent {
  return { type: "browser_action_request", actionId, action, args };
}

/** A Response-like stub for authenticatedFetch. */
function jsonResponse(body: unknown, ok = true): Response {
  return {
    ok,
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

/** Install a `window.omnigentDesktop` bridge; returns the mock so tests assert
 *  on the exact calls / scripted JS. */
function installBridge(overrides: Record<string, unknown> = {}) {
  const bridge = {
    browserBeginPreviewNavigation: vi.fn().mockResolvedValue({
      ok: true,
      requestId: "preview_request_1",
      deadline: Date.now() + 25_000,
    }),
    browserOpenOrNavigate: vi.fn().mockResolvedValue({ ok: true, created: true }),
    browserScreenshot: vi
      .fn()
      .mockResolvedValue({ ok: true, dataUrl: "data:image/png;base64,AAA" }),
    browserExecute: vi.fn().mockResolvedValue({ ok: true, result: "ok" }),
    ...overrides,
  };
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = bridge;
  return bridge;
}

/** Mount the relay and dispatch one action through the bus, then wait for the
 *  full claim → dispatch → result chain to settle. When the claim is expected
 *  to win, wait for the result POST; otherwise (drop paths) just wait for the
 *  single claim fetch to have fired. */
async function runAction(
  evt: BrowserActionRequestEvent,
  opts: { expectResult?: boolean; source?: string } = {},
): Promise<void> {
  const { expectResult = true, source = CONV } = opts;
  renderRelay(CONV);
  emitBrowserActionRequest(evt, source);
  if (expectResult) {
    await vi.waitFor(() => {
      expect(
        authenticatedFetch.mock.calls.some((c) => String(c[0]).includes("/browser/action_result/")),
      ).toBe(true);
    });
  } else {
    await vi.waitFor(() => expect(authenticatedFetch).toHaveBeenCalled());
    // Give the (dropped) handler a couple of turns to prove it does nothing more.
    await Promise.resolve();
    await Promise.resolve();
  }
}

/** The claim_token the winning-claim response carries in most tests. */
const WON = jsonResponse({ claimed: true, claim_token: "tok_1" });

/** Parse the JS string passed to browserExecute for the Nth call. */
function executedJs(bridge: { browserExecute: ReturnType<typeof vi.fn> }, n = 0): string {
  return bridge.browserExecute.mock.calls[n][1] as string;
}

/** Read the result body POSTed back for the last action_result call. */
function postedResult(): Record<string, unknown> {
  const call = authenticatedFetch.mock.calls.find((c) =>
    String(c[0]).includes("/browser/action_result/"),
  );
  if (!call) throw new Error("no action_result POST recorded");
  return JSON.parse((call[1] as { body: string }).body) as Record<string, unknown>;
}

beforeEach(() => {
  authenticatedFetch.mockReset();
  sessionHosts.clear();
  sessionParents.clear();
  sessionSnapshots.clear();
});

afterEach(() => {
  vi.clearAllMocks();
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = undefined;
});

describe("useBrowserAgentRelay — claim-first protocol", () => {
  it("drops the action when the claim is lost (no dispatch, no result POST)", async () => {
    const bridge = installBridge();
    authenticatedFetch.mockResolvedValueOnce(jsonResponse({ claimed: false }));

    await runAction(actionEvent("navigate", { url: "https://example.com" }), {
      expectResult: false,
    });

    // Only the claim fetch fired; no dispatch, no result POST.
    expect(bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
    expect(
      authenticatedFetch.mock.calls.some((c) => String(c[0]).includes("/browser/action_result/")),
    ).toBe(false);
  });

  it("drops the action when the claim call is not ok", async () => {
    const bridge = installBridge();
    authenticatedFetch.mockResolvedValueOnce(jsonResponse({}, false));

    await runAction(actionEvent("screenshot"), { expectResult: false });

    expect(bridge.browserScreenshot).not.toHaveBeenCalled();
  });

  it("drops the action when the claim fetch throws", async () => {
    const bridge = installBridge();
    authenticatedFetch.mockRejectedValueOnce(new Error("network"));

    await runAction(actionEvent("screenshot"), { expectResult: false });

    expect(bridge.browserScreenshot).not.toHaveBeenCalled();
  });

  it("on a won claim, dispatches and POSTs the result with the claim token", async () => {
    const bridge = installBridge();
    authenticatedFetch.mockResolvedValueOnce(WON).mockResolvedValueOnce(jsonResponse({}));

    await runAction(actionEvent("navigate", { url: "https://example.com" }));

    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      CONV,
      "https://example.com",
      undefined,
      {
        force: true,
        agent: true,
        hostId: null,
      },
    );
    const body = postedResult();
    expect(body.claim_token).toBe("tok_1");
    expect((body.result as { ok: boolean }).ok).toBe(true);
  });

  it("routes claim, dispatch, and result to the delivering conversation, not the mounted one", async () => {
    // A background conversation (A) issues a browser action while a different
    // conversation (B) is on screen and owns the relay. Every hop must target A:
    // claiming at B is rejected as an owner mismatch, so nothing executes and
    // A's browser tool times out. This is what background streams made reachable.
    const VISIBLE = "conv_visible_B";
    const BACKGROUND = "conv_background_A";
    const bridge = installBridge();
    authenticatedFetch.mockResolvedValueOnce(WON).mockResolvedValueOnce(jsonResponse({}));

    renderRelay(VISIBLE);
    emitBrowserActionRequest(actionEvent("navigate", { url: "https://a" }), BACKGROUND);

    await vi.waitFor(() => {
      expect(
        authenticatedFetch.mock.calls.some((c) => String(c[0]).includes("/browser/action_result/")),
      ).toBe(true);
    });

    const claimUrl = String(
      authenticatedFetch.mock.calls.find((c) =>
        String(c[0]).includes("/browser/action_claim/"),
      )![0],
    );
    expect(claimUrl).toContain(`/v1/sessions/${BACKGROUND}/browser/action_claim/`);
    expect(claimUrl).not.toContain(VISIBLE);

    // Dispatch targeted A's WebContentsView, and the result posted to A.
    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(BACKGROUND, "https://a", undefined, {
      force: true,
      agent: true,
      hostId: null,
    });
    expect(bridge.browserBeginPreviewNavigation).not.toHaveBeenCalled();
    expect(getSessionSlim).not.toHaveBeenCalled();
    const resultUrl = String(
      authenticatedFetch.mock.calls.find((c) =>
        String(c[0]).includes("/browser/action_result/"),
      )![0],
    );
    expect(resultUrl).toContain(`/v1/sessions/${BACKGROUND}/browser/action_result/`);
  });

  it("loads a cold delivering child's ancestor host only for a loopback preview", async () => {
    sessionSnapshots.set("cold_child", {
      id: "cold_child",
      hostId: null,
      parentSessionId: "cold_parent",
    });
    sessionSnapshots.set("cold_parent", {
      id: "cold_parent",
      hostId: "host_arca",
      parentSessionId: null,
    });
    const bridge = installBridge();
    authenticatedFetch.mockResolvedValueOnce(WON).mockResolvedValueOnce(jsonResponse({}));
    renderRelay("visible_chat");
    emitBrowserActionRequest(
      actionEvent("navigate", { url: "http://localhost:5173/app" }),
      "cold_child",
    );
    await vi.waitFor(() => expect(bridge.browserOpenOrNavigate).toHaveBeenCalled());
    expect(getSessionSlim.mock.calls.map((call) => call[0])).toEqual(["cold_child", "cold_parent"]);
    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      "cold_child",
      "http://localhost:5173/app",
      undefined,
      { force: true, agent: true, hostId: "host_arca" },
      "preview_request_1",
    );
  });

  it("times out cold metadata without opening a late preview", async () => {
    getSessionSlim.mockImplementationOnce(() => new Promise(() => {}));
    const bridge = installBridge({
      browserBeginPreviewNavigation: vi.fn().mockResolvedValue({
        ok: true,
        requestId: "expiring_request",
        deadline: Date.now() + 10,
      }),
    });
    authenticatedFetch.mockResolvedValueOnce(WON).mockResolvedValueOnce(jsonResponse({}));

    await runAction(actionEvent("navigate", { url: "http://localhost:5173" }));

    expect(bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
    expect((postedResult().result as { error: string }).error).toBe(
      "localhost preview request expired",
    );
  });
});

describe("useBrowserAgentRelay — action dispatch", () => {
  beforeEach(() => {
    // Every dispatch test wins the claim, then a benign result POST.
    authenticatedFetch.mockResolvedValue(WON);
  });

  it("navigate: reports the final_url and marks it agent+force", async () => {
    installBridge();
    await runAction(actionEvent("navigate", { url: "https://myhost/page" }));
    expect((postedResult().result as { data: { final_url: string } }).data.final_url).toBe(
      "https://myhost/page",
    );
  });

  it("navigate: empty url is rejected before touching the bridge", async () => {
    const bridge = installBridge();
    await runAction(actionEvent("navigate", { url: "" }));
    expect(bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
    expect((postedResult().result as { ok: boolean; error: string }).error).toMatch(
      /url is required/,
    );
  });

  it("navigate: surfaces the bridge error when the registry rejects", async () => {
    installBridge({
      browserOpenOrNavigate: vi.fn().mockResolvedValue({ ok: false, error: "blocked host" }),
    });
    await runAction(actionEvent("navigate", { url: "https://x" }));
    expect((postedResult().result as { error: string }).error).toBe("blocked host");
  });

  it("screenshot: returns the data_url from the bridge", async () => {
    installBridge();
    await runAction(actionEvent("screenshot"));
    expect((postedResult().result as { data_url: string }).data_url).toBe(
      "data:image/png;base64,AAA",
    );
  });

  it("screenshot: reports 'No browser open' when the bridge has no image", async () => {
    installBridge({ browserScreenshot: vi.fn().mockResolvedValue({ ok: true }) });
    await runAction(actionEvent("screenshot"));
    expect((postedResult().result as { error: string }).error).toMatch(/No browser open/);
  });

  it("snapshot: parses the executed JSON tree", async () => {
    const bridge = installBridge({
      browserExecute: vi
        .fn()
        .mockResolvedValue({ ok: true, result: JSON.stringify({ snapshot_id: "s1", tree: "x" }) }),
    });
    await runAction(actionEvent("snapshot"));
    // The snapshot JS is the fixed SNAPSHOT_JS constant (walks the DOM).
    expect(executedJs(bridge)).toContain("__omni_refs__");
    expect((postedResult().result as { data: { snapshot_id: string } }).data.snapshot_id).toBe(
      "s1",
    );
  });

  it("snapshot: reports a parse error on non-JSON output", async () => {
    installBridge({ browserExecute: vi.fn().mockResolvedValue({ ok: true, result: "not json" }) });
    await runAction(actionEvent("snapshot"));
    expect((postedResult().result as { error: string }).error).toMatch(/snapshot parse failed/);
  });

  it("click by ref: validates snapshot_id and clicks the resolved element", async () => {
    const bridge = installBridge();
    await runAction(actionEvent("click", { ref: 7, snapshot_id: "snap-9" }));
    const js = executedJs(bridge);
    expect(js).toContain('__omni_snapshot_id__ !== "snap-9"');
    expect(js).toContain("__omni_refs__");
    expect(js).toContain("el.click()");
    expect((postedResult().result as { ok: boolean }).ok).toBe(true);
  });

  it("click by selector: resolves via querySelector (neutral selector, JSON-escaped)", async () => {
    const bridge = installBridge();
    await runAction(actionEvent("click", { selector: "button.submit" }));
    const js = executedJs(bridge);
    expect(js).toContain('document.querySelector("button.submit")');
  });

  it("type: sets the value via the native setter and dispatches input/change", async () => {
    const bridge = installBridge();
    await runAction(actionEvent("type", { ref: 3, text: "hello" }));
    const js = executedJs(bridge);
    expect(js).toContain('"hello"'); // text JSON-escaped into the payload
    expect(js).toContain("input");
    expect(js).toContain("change");
    expect((postedResult().result as { ok: boolean }).ok).toBe(true);
  });

  it("click: surfaces the in-page execute error", async () => {
    installBridge({
      browserExecute: vi.fn().mockResolvedValue({ ok: false, error: "selector not found: x" }),
    });
    await runAction(actionEvent("click", { selector: "x" }));
    expect((postedResult().result as { error: string }).error).toBe("selector not found: x");
  });

  it("unknown action is reported, not dispatched", async () => {
    installBridge();
    await runAction(actionEvent("teleport"));
    expect((postedResult().result as { error: string }).error).toMatch(
      /Unknown browser action: teleport/,
    );
  });

  it("missing bridge method → 'does not support the browser pane'", async () => {
    installBridge({ browserExecute: undefined });
    await runAction(actionEvent("snapshot"));
    expect((postedResult().result as { error: string }).error).toMatch(
      /does not support the browser pane/,
    );
  });

  it("type: missing execute bridge → 'does not support the browser pane'", async () => {
    installBridge({ browserExecute: undefined });
    await runAction(actionEvent("type", { ref: 1, text: "x" }));
    expect((postedResult().result as { error: string }).error).toMatch(
      /does not support the browser pane/,
    );
  });

  it("navigate: missing open bridge → 'does not support the browser pane'", async () => {
    installBridge({ browserOpenOrNavigate: undefined });
    await runAction(actionEvent("navigate", { url: "https://x" }));
    expect((postedResult().result as { error: string }).error).toMatch(
      /does not support the browser pane/,
    );
  });

  it("dispatch surfaces a thrown in-page/IPC error as {ok:false} (outer catch)", async () => {
    installBridge({
      browserExecute: vi.fn().mockRejectedValue(new Error("execute blew up")),
    });
    await runAction(actionEvent("click", { selector: "x" }));
    expect((postedResult().result as { ok: boolean; error: string }).error).toBe("execute blew up");
  });
});

describe("useBrowserAgentRelay — result POST resilience", () => {
  it("swallows a failing result POST (best-effort; server timeout covers it)", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    installBridge();
    // Claim wins, but the result POST rejects — must not throw out of the handler.
    authenticatedFetch
      .mockResolvedValueOnce(WON)
      .mockRejectedValueOnce(new Error("result POST network error"));

    renderRelay(CONV);
    emitBrowserActionRequest(actionEvent("screenshot"), CONV);

    await vi.waitFor(() => {
      // Both the claim and the (failed) result POST were attempted.
      expect(authenticatedFetch).toHaveBeenCalledTimes(2);
    });
    // The postResult catch logged rather than throwing.
    await vi.waitFor(() => expect(warn).toHaveBeenCalled());
    warn.mockRestore();
  });

  it("does nothing when the shell exposes no bridge at handler time", async () => {
    // isElectronShell() is mocked true (hook registers), but omnigentDesktop is
    // absent — getBrowserDesktop() returns null, so the handler bails before claim.
    (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = undefined;

    renderRelay(CONV);
    emitBrowserActionRequest(actionEvent("screenshot"), CONV);
    await Promise.resolve();
    await Promise.resolve();

    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
});
