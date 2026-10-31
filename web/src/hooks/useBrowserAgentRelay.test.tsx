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
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: (...args: unknown[]) => authenticatedFetch(...args),
}));
const getSessionSlim = vi.fn();
vi.mock("@/lib/sessionsApi", () => ({
  getSessionSlim: (...args: unknown[]) => getSessionSlim(...args),
}));
import { setSessionHost, setSessionParent } from "@/lib/sessionHost";
import { readArcaHostId, writeArcaHostId } from "@/lib/arcaHost";

import { emitBrowserActionRequest } from "@/lib/browserActionBus";
import type { BrowserActionRequestEvent } from "@/lib/events";
import { useBrowserAgentRelay } from "./useBrowserAgentRelay";
import legacyActions from "../../electron/test/fixtures/browserActions-v0.18.json";

const CONV = "conv_relay";
const renderRelay = (visibleId: string | null | undefined = CONV, client = new QueryClient()) => {
  return renderHook(({ id }) => useBrowserAgentRelay(id), {
    initialProps: { id: visibleId },
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
};

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
    browserSessionNetworkPolicy: true,
    browserOpenOrNavigate: vi.fn().mockResolvedValue({ ok: true, created: true }),
    browserScreenshot: vi
      .fn()
      .mockResolvedValue({ ok: true, dataUrl: "data:image/png;base64,AAA" }),
    browserPerformAction: vi.fn().mockResolvedValue({ ok: true, result: "ok" }),
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
  opts: { client?: QueryClient; expectResult?: boolean; source?: string } = {},
): Promise<void> {
  const { client = new QueryClient(), expectResult = true, source = CONV } = opts;
  renderRelay(CONV, client);
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

/** Read the result body POSTed back for the last action_result call. */
function postedResult(): Record<string, unknown> {
  const call = authenticatedFetch.mock.calls.find((c) =>
    String(c[0]).includes("/browser/action_result/"),
  );
  if (!call) throw new Error("no action_result POST recorded");
  return JSON.parse((call[1] as { body: string }).body) as Record<string, unknown>;
}

beforeEach(() => {
  writeArcaHostId(null);
  authenticatedFetch.mockReset();
  getSessionSlim.mockReset().mockImplementation(async (id: string) => ({
    id,
    hostId: "local-host",
    parentSessionId: null,
  }));
  for (const id of [CONV, "conv_visible_B", "conv_background_A", "parent", "middle", "root"]) {
    setSessionHost(id, null);
    setSessionParent(id, null);
  }
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
        sourceHostId: "local-host",
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
      sourceHostId: "local-host",
    });
    const resultUrl = String(
      authenticatedFetch.mock.calls.find((c) =>
        String(c[0]).includes("/browser/action_result/"),
      )![0],
    );
    expect(resultUrl).toContain(`/v1/sessions/${BACKGROUND}/browser/action_result/`);
  });

  it("completes the source session's screenshot when the visible session changes mid-claim", async () => {
    const bridge = installBridge();
    let resolveClaim!: (response: Response) => void;
    authenticatedFetch
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            resolveClaim = resolve;
          }),
      )
      .mockResolvedValue(jsonResponse({}));
    const hook = renderRelay(CONV);
    emitBrowserActionRequest(actionEvent("screenshot"), "conv_background_A");
    await vi.waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    hook.rerender({ id: "conv_visible_B" });
    resolveClaim(WON);
    await vi.waitFor(() =>
      expect(postedResult().result).toEqual({
        ok: true,
        data_url: "data:image/png;base64,AAA",
      }),
    );
    expect(bridge.browserScreenshot).toHaveBeenCalledWith("conv_background_A", "local-host");
    expect(authenticatedFetch.mock.calls[0][0]).toContain(
      "/v1/sessions/conv_background_A/browser/action_claim/",
    );
    expect(authenticatedFetch.mock.calls[1][0]).toContain(
      "/v1/sessions/conv_background_A/browser/action_result/",
    );
  });

  it("cancels a pending claimed action when leaving all conversations", async () => {
    const bridge = installBridge();
    let resolveClaim!: (response: Response) => void;
    authenticatedFetch
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            resolveClaim = resolve;
          }),
      )
      .mockResolvedValue(jsonResponse({}));
    const hook = renderRelay();
    emitBrowserActionRequest(actionEvent("screenshot"), "conv_background_A");
    await vi.waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    hook.rerender({ id: null });
    resolveClaim(WON);
    await vi.waitFor(() =>
      expect(postedResult().result).toEqual({
        ok: false,
        error: "browser relay context changed",
      }),
    );
    expect(bridge.browserScreenshot).not.toHaveBeenCalled();
    emitBrowserActionRequest(actionEvent("screenshot", {}, "later"), "conv_background_A");
    expect(authenticatedFetch).toHaveBeenCalledTimes(2);
  });
});

describe("useBrowserAgentRelay — action dispatch", () => {
  beforeEach(() => {
    // Every dispatch test wins the claim, then a benign result POST.
    authenticatedFetch.mockResolvedValue(WON);
  });
  it("derives inherited provenance from the source session, never model args or visible host", async () => {
    const bridge = installBridge();
    writeArcaHostId("arca-host");
    setSessionHost(CONV, "local-host");
    setSessionHost("parent", "arca-host");
    setSessionParent("conv_background_A", "parent");
    getSessionSlim.mockImplementation(async (id: string) => ({
      id,
      hostId: id === "parent" ? "arca-host" : null,
      parentSessionId: id === "parent" ? null : "parent",
    }));
    await runAction(
      actionEvent("navigate", {
        url: "http://localhost:5173",
        sourceHostId: "forged",
        isArca: true,
      }),
      { source: "conv_background_A" },
    );
    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      "conv_background_A",
      "http://localhost:5173",
      undefined,
      {
        force: true,
        agent: true,
        sourceHostId: "arca-host",
      },
    );
    expect(getSessionSlim.mock.calls.map(([id]) => id)).toEqual(["conv_background_A", "parent"]);
  });

  it("keeps the remembered Arca host after a same-machine preview", async () => {
    const bridge = installBridge();
    writeArcaHostId("remembered-arca-host");
    getSessionSlim.mockResolvedValue({ id: CONV, hostId: "local-host", parentSessionId: null });
    await runAction(actionEvent("navigate", { url: "http://localhost:5173" }));
    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      CONV,
      "http://localhost:5173",
      undefined,
      {
        force: true,
        agent: true,
        sourceHostId: "local-host",
      },
    );
    expect(readArcaHostId()).toBe("remembered-arca-host");
  });

  it("loads a child's own host before granting eligibility from a parent-list hint", async () => {
    const bridge = installBridge();
    setSessionHost("parent", "arca-host");
    setSessionParent("conv_background_A", "parent");
    getSessionSlim.mockResolvedValue({
      id: "conv_background_A",
      hostId: "other-host",
      parentSessionId: "parent",
    });
    await runAction(actionEvent("navigate", { url: "http://localhost:5173" }), {
      source: "conv_background_A",
    });
    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      "conv_background_A",
      "http://localhost:5173",
      undefined,
      { force: true, agent: true, sourceHostId: "other-host" },
    );
  });

  it.each(["navigate", "screenshot", "snapshot", "click", "type"])(
    "refreshes source provenance for %s after a host rebind",
    async (action) => {
      const bridge = installBridge();
      const client = new QueryClient();
      client.setQueryData(["session", CONV], { id: CONV, hostId: "local-host" });
      getSessionSlim.mockResolvedValue({ id: CONV, hostId: "remote-host", parentSessionId: null });
      await runAction(
        actionEvent(action, {
          url: "http://localhost",
          selector: "input",
          text: "hi",
          sourceHostId: "forged",
        }),
        { client },
      );
      expect(getSessionSlim).toHaveBeenCalledWith(CONV);
      const call =
        action === "navigate"
          ? bridge.browserOpenOrNavigate.mock.calls[0][3].sourceHostId
          : action === "screenshot"
            ? bridge.browserScreenshot.mock.calls[0][1]
            : bridge.browserPerformAction.mock.calls[0][3];
      expect(call).toBe("remote-host");
    },
  );

  it("reads an unloaded intermediate's own host instead of borrowing a distant Arca hint", async () => {
    const bridge = installBridge();
    setSessionParent(CONV, "middle");
    setSessionParent("middle", "root");
    setSessionHost("root", "arca-host");
    getSessionSlim.mockImplementation(async (id: string) => ({
      id,
      hostId: id === "middle" ? "other-host" : null,
      parentSessionId: id === CONV ? "middle" : "root",
    }));
    await runAction(actionEvent("navigate", { url: "http://localhost:5173" }));
    expect(getSessionSlim.mock.calls.map(([id]) => id)).toEqual([CONV, "middle"]);
    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      CONV,
      "http://localhost:5173",
      undefined,
      { force: true, agent: true, sourceHostId: "other-host" },
    );
  });

  it.each(["lookup failure", "cycle"])("uses public-only provenance after %s", async (failure) => {
    const bridge = installBridge();
    getSessionSlim.mockImplementation(async (id: string) => {
      if (failure === "lookup failure") throw new Error("source unavailable");
      return { id, hostId: null, parentSessionId: id === CONV ? "parent" : CONV };
    });
    await runAction(actionEvent("screenshot", { sourceHostId: "local-host" }));
    expect(bridge.browserScreenshot).toHaveBeenCalledWith(CONV, undefined);
    expect(postedResult().result).toMatchObject({ ok: true });
  });

  it.each(["navigate", "screenshot", "snapshot", "click", "type"])(
    "fails closed on an older shell for %s",
    async (action) => {
      const bridge = installBridge({ browserSessionNetworkPolicy: undefined });
      await runAction(actionEvent(action, { url: "https://example.com" }));
      expect(bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
      expect(bridge.browserScreenshot).not.toHaveBeenCalled();
      expect(bridge.browserPerformAction).not.toHaveBeenCalled();
      expect(postedResult().result).toMatchObject({
        ok: false,
        error: expect.stringMatching(/Update Omnigent Desktop/),
      });
    },
  );

  it("keeps source-host resolution alive across visible-session switches", async () => {
    const bridge = installBridge();
    let resolveHost!: (source: unknown) => void;
    getSessionSlim.mockImplementation((id: string) =>
      id === "parent"
        ? new Promise((resolve) => {
            resolveHost = resolve;
          })
        : Promise.resolve({ id, hostId: null, parentSessionId: "parent" }),
    );
    const hook = renderRelay();
    emitBrowserActionRequest(
      actionEvent("navigate", { url: "http://localhost:5173" }),
      "conv_background_A",
    );
    await vi.waitFor(() => expect(getSessionSlim).toHaveBeenCalledWith("parent"));
    hook.rerender({ id: "conv_visible_B" });
    setSessionHost("conv_visible_B", "local-host");
    resolveHost({ id: "parent", hostId: "arca-host", parentSessionId: null });
    await vi.waitFor(() =>
      expect(postedResult().result).toEqual({
        ok: true,
        data: { final_url: "http://localhost:5173" },
      }),
    );
    expect(getSessionSlim.mock.calls.map(([id]) => id)).toEqual(["conv_background_A", "parent"]);
    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      "conv_background_A",
      "http://localhost:5173",
      undefined,
      { force: true, agent: true, sourceHostId: "arca-host" },
    );
    expect(authenticatedFetch.mock.calls[1][0]).toContain(
      "/v1/sessions/conv_background_A/browser/action_result/",
    );
  });

  it("does not dispatch a stale metadata resolution after relay unmount", async () => {
    const bridge = installBridge();
    let resolve!: (source: unknown) => void;
    getSessionSlim.mockImplementation(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    );
    const hook = renderRelay();
    emitBrowserActionRequest(actionEvent("navigate", { url: "http://localhost:5173" }), CONV);
    await vi.waitFor(() => expect(getSessionSlim).toHaveBeenCalledWith(CONV));
    hook.unmount();
    resolve({ id: CONV, hostId: "arca-host", parentSessionId: null });
    await vi.waitFor(() =>
      expect(postedResult().result).toEqual({
        ok: false,
        error: "browser relay context changed",
      }),
    );
    expect(bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
  });

  it("keeps the authoritative source lookup alive across visible-session switches", async () => {
    const bridge = installBridge();
    let resolve!: (source: unknown) => void;
    getSessionSlim.mockImplementation(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    );
    const hook = renderRelay();
    emitBrowserActionRequest(
      actionEvent("navigate", { url: "http://localhost:5173" }),
      "conv_background_A",
    );
    await vi.waitFor(() => expect(getSessionSlim).toHaveBeenCalledWith("conv_background_A"));
    hook.rerender({ id: "conv_visible_B" });
    setSessionHost("conv_visible_B", "local-host");
    resolve({ id: "conv_background_A", hostId: "arca-host", parentSessionId: null });
    await vi.waitFor(() => expect(postedResult().result).toHaveProperty("ok", true));
    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      "conv_background_A",
      "http://localhost:5173",
      undefined,
      { force: true, agent: true, sourceHostId: "arca-host" },
    );
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
      browserPerformAction: vi
        .fn()
        .mockResolvedValue({ ok: true, result: JSON.stringify({ snapshot_id: "s1", tree: "x" }) }),
    });
    await runAction(actionEvent("snapshot"));
    expect(bridge.browserPerformAction).toHaveBeenCalledWith(
      "conv_relay",
      "snapshot",
      undefined,
      "local-host",
    );
    expect((postedResult().result as { data: { snapshot_id: string } }).data.snapshot_id).toBe(
      "s1",
    );
  });

  it("snapshot: reports a parse error on non-JSON output", async () => {
    installBridge({
      browserPerformAction: vi.fn().mockResolvedValue({ ok: true, result: "not json" }),
    });
    await runAction(actionEvent("snapshot"));
    expect((postedResult().result as { error: string }).error).toMatch(/snapshot parse failed/);
  });

  it.each(legacyActions.requests)(
    "supports an older desktop's $action transport with its frozen template",
    async ({ action, args, script }) => {
      const browserExecute = vi.fn().mockResolvedValue({ ok: true, result: "{}" });
      installBridge({ browserPerformAction: undefined, browserExecute });
      await runAction(actionEvent(action, args));
      expect(browserExecute).toHaveBeenCalledWith(CONV, script, "local-host");
      expect(postedResult().result).toMatchObject({ ok: true });
    },
  );

  it("prefers typed actions when a desktop also offers legacy compatibility", async () => {
    const browserExecute = vi.fn();
    const bridge = installBridge({ browserExecute });
    await runAction(actionEvent("click", { ref: 1 }));
    expect(bridge.browserPerformAction).toHaveBeenCalledWith(
      CONV,
      "click",
      { ref: 1 },
      "local-host",
    );
    expect(browserExecute).not.toHaveBeenCalled();
  });

  it.each([
    { action: "click", args: { ref: 7, snapshot_id: "snap-9" } },
    { action: "click", args: { selector: "button.submit" } },
    { action: "type", args: { ref: 3, text: "hello" } },
  ])("forwards $action with $args and returns its result", async ({ action, args }) => {
    const bridge = installBridge();
    await runAction(actionEvent(action, args));
    expect(bridge.browserPerformAction).toHaveBeenCalledWith(
      "conv_relay",
      action,
      args,
      "local-host",
    );
    expect((postedResult().result as { ok: boolean }).ok).toBe(true);
  });

  it("click: surfaces the in-page execute error", async () => {
    installBridge({
      browserPerformAction: vi
        .fn()
        .mockResolvedValue({ ok: false, error: "selector not found: x" }),
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

  it.each(["snapshot", "click", "type"])("%s: missing browser action bridge", async (action) => {
    installBridge({ browserPerformAction: undefined });
    await runAction(actionEvent(action, { ref: 1, text: "x" }));
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
      browserPerformAction: vi.fn().mockRejectedValue(new Error("execute blew up")),
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

    renderRelay();
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

    renderRelay();
    emitBrowserActionRequest(actionEvent("screenshot"), CONV);
    await Promise.resolve();
    await Promise.resolve();

    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
});
