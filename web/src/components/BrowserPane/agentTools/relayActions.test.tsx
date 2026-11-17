// The relay side of the agent browser tools (relayActions.ts,
// relayGate.ts): which actions run, on which tab, with what.
import { cleanup, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/nativeBridge", () => ({
  SESSION_HOST_PROTOCOL: 1,
  isElectronShell: () => true,
  supportsBrowser: () => true,
  getHostIdentity: async () => ({ cliInstalled: true, hostId: "host_laptop" }),
}));
const authenticatedFetch = vi.fn();
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: (...args: unknown[]) => authenticatedFetch(...args),
}));
const getSessionSlim = vi.fn();
vi.mock("@/lib/sessionsApi", () => ({
  getSessionSlim: (...args: unknown[]) => getSessionSlim(...args),
}));
import { emitBrowserActionRequest } from "@/lib/browserActionBus";
import { setSessionHost, setSessionParent } from "@/lib/sessionHost";
import { useBrowserAgentRelay } from "@/hooks/useBrowserAgentRelay";
import { AGENT_BROWSER_TAB_ID, browserViewId } from "@/hooks/useBrowserTabs";
import { AGENT_ACTION_EXPIRED_ERROR } from "./deadline";
import { fakeMainTabs, type FakeMainTabs } from "./fakeMainTabs";
import { writeBrowserAgentToolsEnabled } from "./preferences";
import { dispatchAgentBrowserAction } from "./relayActions";

interface Result {
  ok: boolean;
  error?: string;
  data?: Record<string, unknown>;
}

let sessionCount = 0;
let id: string;
let main: FakeMainTabs;
let desktop: Record<string, ReturnType<typeof vi.fn> | unknown>;
const perform = () => desktop.browserPerformAction as ReturnType<typeof vi.fn>;

/** A session with `label` (fresh per test: labels are looked up once). */
function session(label: string | undefined, hostId: string | null = null) {
  sessionCount += 1;
  id = `conv_${sessionCount}`;
  getSessionSlim.mockImplementation(async (sid: string) => ({
    id: sid,
    hostId,
    parentSessionId: null,
    labels: label ? { "omnigent.browser_agent_tools": label } : {},
  }));
}

/** A desktop app with the tools (or, with `older`, one without them). */
function app({ older = false } = {}) {
  main = fakeMainTabs();
  desktop = {
    ...(older
      ? {}
      : { browserTabs: main.browserTabs, onBrowserTabsChanged: main.onBrowserTabsChanged }),
    browserOpenOrNavigate: vi.fn().mockResolvedValue({ ok: true, created: true }),
    browserPerformAction: vi.fn().mockResolvedValue({ ok: true, result: "{}" }),
    browserHasView: vi.fn().mockResolvedValue({ exists: false }),
    browserClose: vi.fn().mockResolvedValue({ ok: true, removed: true }),
  };
  Object.assign(window, { omnigentDesktop: desktop });
}

/** The relay's dispatch, standing in: records where each action ran. */
const relayDispatch = vi.fn(
  async (
    viewId: string,
    action: string,
    args: Record<string, unknown>,
    d: object,
  ): Promise<Result> => {
    if (action === "navigate") {
      const open = (d as { browserOpenOrNavigate: (...a: unknown[]) => Promise<Result> })
        .browserOpenOrNavigate;
      return open(viewId, args.url, undefined, { force: true, agent: true });
    }
    return { ok: true, data: { ran: `${action} on ${viewId}` } };
  },
);

function run(
  action: string,
  args: Record<string, unknown> = {},
  deadlineAt?: number,
  readHostOptions?: { sessionHostProtocol: number; sourceHostId: string | null },
): Promise<Result> {
  return dispatchAgentBrowserAction(
    relayDispatch,
    id,
    action,
    args,
    desktop,
    "source-host",
    null,
    false,
    {
      ...(deadlineAt === undefined ? {} : { deadlineAt }),
      ...(readHostOptions && { readHostOptions }),
    },
  );
}

beforeEach(() => {
  writeBrowserAgentToolsEnabled(true);
  relayDispatch.mockClear();
  session("on");
  app();
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  Reflect.deleteProperty(window, "omnigentDesktop");
});

describe("which tools run", () => {
  it.each(["trusted", "standard", undefined])(
    "refuses an advanced tool in a session created without them (%s), even with the switch on",
    async (label) => {
      session(label);
      // A model can name a tool its session never offered (a prompt injection).
      for (const [action, args] of [
        ["press_key", { key: "Enter" }],
        ["tabs", { action: "list" }],
        ["click", { ref: 1, double_click: true }],
      ] as const) {
        // oxlint-disable-next-line no-await-in-loop -- One action at a time.
        expect((await run(action, args)).error, action).toMatch(
          /isn't available in this session: it was started without the advanced browser tools/,
        );
      }
      expect(perform()).not.toHaveBeenCalled();
      expect((await run("click", { ref: 1 })).ok).toBe(true);
    },
  );

  it("refuses the advanced tools while the switch is off", async () => {
    writeBrowserAgentToolsEnabled(false);
    expect((await run("hover", { selector: "#menu" })).error).toContain(
      "browser_hover is turned off",
    );
    expect(perform()).not.toHaveBeenCalled();
  });

  it("looks a session's label up once, and nothing runs when it answers after the deadline", async () => {
    session("on");
    let answer: (s: unknown) => void = () => {};
    getSessionSlim.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    );
    const late = run("hover", { ref: 1 }, Date.now() + 20);
    await new Promise((resolve) => {
      setTimeout(resolve, 40);
    });
    answer({ id, labels: { "omnigent.browser_agent_tools": "on" } });
    expect(await late).toEqual({ ok: false, error: AGENT_ACTION_EXPIRED_ERROR });
    expect(perform()).not.toHaveBeenCalled();
    getSessionSlim.mockClear();
    await run("hover", { ref: 1 });
    await run("scroll", { direction: "down" });
    expect(getSessionSlim).toHaveBeenCalledTimes(1);
  });

  it("runs no advanced action when the switch goes off while its session is looked up", async () => {
    session("on");
    main.otherWindow(id, (r) => ({ ...r, tabs: ["theirs"], agentTab: "theirs" }));
    let answer: (s: unknown) => void = () => {};
    getSessionSlim.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    );
    const pressing = run("press_key", { key: "Enter" });
    writeBrowserAgentToolsEnabled(false);
    answer({ id, labels: { "omnigent.browser_agent_tools": "on" } });
    expect((await pressing).error).toContain("browser_press_key is turned off");
    expect(perform()).not.toHaveBeenCalled();
  });
});

describe("in a desktop app without the tools", () => {
  beforeEach(() => app({ older: true }));

  it("refuses the advanced tools, asking for an update", async () => {
    expect((await run("go_back")).error).toMatch(/doesn't support browser_go_back; update the app/);
    expect((await run("click", { ref: 1, button: "right" })).error).toMatch(
      /doesn't support browser_click with button or double_click/,
    );
    expect(relayDispatch).not.toHaveBeenCalled();
  });

  it.each(["on", "trusted"])(
    "refuses even the standard tools in a session started in an app with them (%s)",
    async (label) => {
      session(label);
      for (const action of ["click", "type", "navigate", "snapshot"]) {
        // oxlint-disable-next-line no-await-in-loop -- One action at a time.
        expect((await run(action, { ref: 1 })).error, action).toMatch(
          new RegExp(
            `started in a newer Omnigent desktop app; update this app to use browser_${action}`,
          ),
        );
      }
      expect(relayDispatch).not.toHaveBeenCalled();
    },
  );

  it("runs the standard tools as without the advanced ones in other sessions: their own view and arguments", async () => {
    session(undefined);
    await run("click", { ref: 1, provenance: "model's" });
    expect(relayDispatch).toHaveBeenLastCalledWith(
      id,
      "click",
      { ref: 1, provenance: "model's" },
      desktop,
      "source-host",
      null,
      false,
    );
  });
});

describe("what main gets", () => {
  it("runs main's actions with the session's host, never the model's, and returns their data without main's ok", async () => {
    perform().mockImplementation(async (_viewId: string, action: string) => ({
      ok: true,
      result: JSON.stringify(
        action === "scroll" ? { ok: true, scroll_y: 640, at_end: false } : { ok: true },
      ),
    }));
    expect(
      await run("scroll", { direction: "down", provenance: { sourceHostId: "forged" } }),
    ).toEqual({
      ok: true,
      data: { scroll_y: 640, at_end: false },
    });
    expect(perform()).toHaveBeenLastCalledWith(id, "scroll", {
      direction: "down",
      provenance: { sourceHostId: "source-host" },
      deadlineAt: expect.any(Number),
    });
    for (const action of ["go_back", "go_forward", "reload"]) {
      // oxlint-disable-next-line no-await-in-loop -- One action at a time.
      await run(action);
      expect(perform()).toHaveBeenLastCalledWith(id, action, {
        provenance: { sourceHostId: "source-host" },
        deadlineAt: expect.any(Number),
      });
    }
    expect((await run("reload", { hard: true })).error).toBe("browser_reload takes no arguments");
    perform().mockResolvedValueOnce({ ok: false, error: "no element for ref 9" });
    expect(await run("hover", { ref: 9 })).toEqual({ ok: false, error: "no element for ref 9" });
  });

  it("sends right and double clicks to main, and plain clicks and typing through the relay's dispatch", async () => {
    await run("click", { ref: 2, button: "right" });
    expect(perform()).toHaveBeenLastCalledWith(id, "click", {
      ref: 2,
      button: "right",
      provenance: { sourceHostId: "source-host" },
      deadlineAt: expect.any(Number),
    });
    await run("type", { ref: 3, text: "hi", provenance: "forged" });
    expect(relayDispatch.mock.lastCall?.slice(0, 3)).toEqual([
      id,
      "type",
      {
        ref: 3,
        text: "hi",
        provenance: { sourceHostId: "source-host", realInput: true },
      },
    ]);
  });

  it.each([
    ["on", true],
    ["trusted", true],
    [undefined, false],
  ])("asks for real text input only in a session told to expect it (%s)", async (label, real) => {
    session(label);
    // Standard sessions are refused advanced tools only, so typing still runs.
    await run("type", { ref: 3, text: "hi" });
    expect(relayDispatch.mock.lastCall?.[2]).toEqual({
      ref: 3,
      text: "hi",
      provenance: real
        ? { sourceHostId: "source-host", realInput: true }
        : { sourceHostId: "source-host" },
    });
  });

  it("gives every call main does page work for the action's deadline", async () => {
    const screenshot = vi.fn().mockResolvedValue({ ok: true, dataUrl: "data:," });
    Object.assign(desktop, { browserScreenshot: screenshot });
    // The relay's dispatch calls the bridge it's given: perform for its page
    // actions, screenshot, and open for navigations.
    const viaBridge = vi.fn(
      async (viewId: string, action: string, args: Record<string, unknown>, d: object) => {
        const bridge = d as Record<string, (...a: unknown[]) => Promise<Result>>;
        if (action === "navigate")
          return bridge.browserOpenOrNavigate(viewId, args.url, undefined, { agent: true });
        if (action === "screenshot") return bridge.browserScreenshot(viewId);
        return bridge.browserPerformAction(
          viewId,
          action,
          action === "snapshot" ? undefined : args,
        );
      },
    );
    const deadlineAt = Date.now() + 10_000;
    const send = (action: string, args: Record<string, unknown> = {}) =>
      dispatchAgentBrowserAction(viaBridge, id, action, args, desktop, null, null, false, {
        deadlineAt,
      });
    for (const [action, args] of [
      ["snapshot", {}],
      ["click", { ref: 1 }],
      ["hover", { ref: 1 }],
      ["go_back", {}],
    ] as const) {
      // oxlint-disable-next-line no-await-in-loop -- One action at a time.
      await send(action, args);
      expect(perform().mock.lastCall?.[2], action).toMatchObject({ deadlineAt });
    }
    await send("screenshot");
    expect(screenshot).toHaveBeenLastCalledWith(id, { deadlineAt });
    await send("navigate", { url: "https://example.com/" });
    expect((desktop.browserOpenOrNavigate as ReturnType<typeof vi.fn>).mock.lastCall?.[3]).toEqual({
      agent: true,
      deadlineAt,
    });
  });
});

describe("the tab actions run on", () => {
  it("is the agent's tab in main, whichever window chose it", async () => {
    const opened = await run("tabs", { action: "new", url: "https://example.org/" });
    const tabId = String(opened.data?.tab_id);
    expect(relayDispatch.mock.lastCall?.slice(0, 2)).toEqual([
      browserViewId(id, tabId),
      "navigate",
    ]);
    await run("hover", { ref: 1 });
    expect(perform().mock.lastCall?.[0]).toBe(browserViewId(id, tabId));
    // Another window's browser_tabs select moves it here too.
    main.otherWindow(id, (r) => ({ ...r, tabs: [...r.tabs, "theirs"], agentTab: "theirs" }));
    await run("snapshot");
    expect(relayDispatch.mock.lastCall?.slice(0, 2)).toEqual([
      browserViewId(id, "theirs"),
      "snapshot",
    ]);
  });

  it("is the reserved tab again after the agent closes it, for every action after", async () => {
    main.otherWindow(id, (r) => ({
      ...r,
      tabs: ["user-tab", AGENT_BROWSER_TAB_ID],
      selected: AGENT_BROWSER_TAB_ID,
    }));
    expect(await run("tabs", { action: "close" })).toEqual({
      ok: true,
      data: { closed_tab_id: AGENT_BROWSER_TAB_ID, current_tab_id: AGENT_BROWSER_TAB_ID },
    });
    expect(desktop.browserClose).toHaveBeenCalledWith(id, undefined, {
      deadlineAt: expect.any(Number),
      expectedNavs: { tabId: AGENT_BROWSER_TAB_ID, count: 0 },
    });
    await run("snapshot");
    expect(relayDispatch.mock.lastCall?.slice(0, 2)).toEqual([
      browserViewId(id, AGENT_BROWSER_TAB_ID),
      "snapshot",
    ]);
    expect((await run("tabs", { action: "select", tab_id: "gone" })).error).toMatch(
      /no open tab gone/,
    );
    expect((await run("tabs", { action: "close", tab_id: "gone" })).ok).toBe(false);
    expect((await run("tabs", { action: "rename" })).error).toMatch(/list, new, select, close/);
  });

  it("closes nothing once its time ran out while it read the tabs", async () => {
    main.otherWindow(id, (r) => ({ ...r, tabs: ["user-tab", "theirs"], agentTab: "theirs" }));
    const read = main.browserTabs;
    // The window stalls reading the tabs until after the agent was told it failed.
    desktop.browserTabs = async (args: Record<string, unknown>) => {
      if (args.op === "get") {
        await new Promise((resolve) => {
          setTimeout(resolve, 60);
        });
      }
      return read(args);
    };
    const closed = await run("tabs", { action: "close", tab_id: "theirs" }, Date.now() + 20);
    expect(closed).toEqual({ ok: false, error: AGENT_ACTION_EXPIRED_ERROR });
    expect(desktop.browserClose).not.toHaveBeenCalled();
    expect(main.record(id)?.tabs).toEqual(["user-tab", "theirs"]);
  });

  it("keeps a tab another window navigated before the close reached main", async () => {
    main.otherWindow(id, (r) => ({
      ...r,
      tabs: ["user-tab", "theirs"],
      agentTab: "theirs",
      navs: { theirs: 1 },
    }));
    (desktop.browserClose as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      removed: false,
      superseded: true,
    });
    const closed = await run("tabs", { action: "close", tab_id: "theirs" });
    expect(desktop.browserClose).toHaveBeenCalledWith(browserViewId(id, "theirs"), undefined, {
      deadlineAt: expect.any(Number),
      expectedNavs: { tabId: "theirs", count: 1 },
    });
    expect(closed.ok).toBe(false);
    expect(closed.error).toMatch(/went to a new page while it was closing, so it was left open/);
    expect(main.record(id)?.tabs).toEqual(["user-tab", "theirs"]);
  });

  it("keeps the tab when main refuses a close that arrives late", async () => {
    main.otherWindow(id, (r) => ({ ...r, tabs: ["user-tab", "theirs"], agentTab: "theirs" }));
    (desktop.browserClose as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      ok: false,
      removed: false,
      expired: true,
    });
    const closed = await run("tabs", { action: "close", tab_id: "theirs" });
    expect(closed).toEqual({ ok: false, error: AGENT_ACTION_EXPIRED_ERROR });
    expect(main.record(id)?.tabs).toEqual(["user-tab", "theirs"]);
  });
});

describe("through the relay", () => {
  const WON = { ok: true, json: async () => ({ claimed: true, claim_token: "tok" }) };
  const relay = (action: string, args: Record<string, unknown>) => {
    renderHook(() => useBrowserAgentRelay(id), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>
      ),
    });
    emitBrowserActionRequest({ type: "browser_action_request", actionId: "a1", action, args }, id);
    return vi.waitFor(() => {
      const posted = authenticatedFetch.mock.calls.find((c) =>
        String(c[0]).includes("/action_result/"),
      );
      expect(posted).toBeDefined();
      return (JSON.parse((posted![1] as { body: string }).body) as { result: Result }).result;
    });
  };

  beforeEach(() => {
    authenticatedFetch.mockReset().mockResolvedValue(WON);
    setSessionHost(id, null);
    setSessionParent(id, null);
  });

  it("sends input with the issuing session's host", async () => {
    session("on", "remote-host");
    await relay("press_key", { key: "Enter" });
    expect(perform()).toHaveBeenLastCalledWith(id, "press_key", {
      key: "Enter",
      provenance: { sourceHostId: "remote-host" },
      deadlineAt: expect.any(Number),
    });
  });

  it("counts an action's time from before its claim, however late the claim's answer", async () => {
    const realNow = Date.now.bind(Date);
    let skew = 0;
    const now = vi.spyOn(Date, "now").mockImplementation(() => realNow() + skew);
    authenticatedFetch.mockImplementation(async (url: unknown) => {
      // The server took the claim at once; its answer arrives 29 s later.
      if (String(url).includes("/action_claim/")) skew = 29_000;
      return String(url).includes("/action_claim/") ? WON : { ok: true, json: async () => ({}) };
    });
    try {
      expect(await relay("hover", { selector: "#a" })).toEqual({
        ok: false,
        error: AGENT_ACTION_EXPIRED_ERROR,
      });
      expect(perform()).not.toHaveBeenCalled();
    } finally {
      now.mockRestore();
    }
  });
});

describe("pages the session's host can't read", () => {
  const hostOptions = { sessionHostProtocol: 1, sourceHostId: "source-host" };
  const refuseCheck = (viewId: string) =>
    perform().mockImplementation(async (view: string, action: string) =>
      action === "check_page" && view === viewId
        ? { ok: false, error: "Current session host is required to read this browser view" }
        : { ok: true, result: "{}" },
    );

  it("sends the session's host options with every action main performs", async () => {
    for (const [action, args] of [
      ["go_back", {}],
      ["hover", { ref: 1 }],
      ["click", { ref: 1, double_click: true }],
      ["tabs", { action: "select", tab_id: "theirs" }],
    ] as const) {
      main.otherWindow(id, (r) => ({ ...r, tabs: ["theirs"], agentTab: "theirs" }));
      (desktop.browserHasView as ReturnType<typeof vi.fn>).mockResolvedValue({ exists: true });
      perform().mockClear();
      // oxlint-disable-next-line no-await-in-loop -- One action at a time.
      await run(action, args, undefined, hostOptions);
      expect(perform().mock.calls.length, action).toBeGreaterThan(0);
      for (const call of perform().mock.calls) expect(call[3], action).toEqual(hostOptions);
    }
  });

  it("lists no address or title for a tab whose page it can't read", async () => {
    main.otherWindow(id, (r) => ({ ...r, tabs: ["open", "private"], agentTab: "open" }));
    (desktop.browserHasView as ReturnType<typeof vi.fn>).mockImplementation(async () => ({
      exists: true,
      url: "http://localhost:3000/secret",
      title: "Secret",
    }));
    refuseCheck(browserViewId(id, "private"));
    const listed = await run("tabs", { action: "list" });
    const tabs = listed.data?.tabs as { tab_id: string; url: string; title: string }[];
    expect(tabs.find((t) => t.tab_id === "private")).toMatchObject({ url: "", title: "" });
    expect(tabs.find((t) => t.tab_id === "open")).toMatchObject({
      url: "http://localhost:3000/secret",
    });
  });

  it("returns to its reserved tab when it closes its own, without checking the replacement", async () => {
    main.otherWindow(id, (r) => ({
      ...r,
      tabs: ["private", "mine"],
      selected: "private",
      agentTab: "mine",
    }));
    (desktop.browserHasView as ReturnType<typeof vi.fn>).mockResolvedValue({ exists: true });
    refuseCheck(browserViewId(id, "private"));
    const closed = await run("tabs", { action: "close" });
    expect(closed).toEqual({
      ok: true,
      data: { closed_tab_id: "mine", current_tab_id: AGENT_BROWSER_TAB_ID },
    });
    await run("snapshot");
    expect(relayDispatch.mock.lastCall?.[0]).toBe(browserViewId(id, AGENT_BROWSER_TAB_ID));
  });

  it("doesn't choose a tab whose page it couldn't check", async () => {
    main.otherWindow(id, (r) => ({ ...r, tabs: ["unknown"], selected: "unknown" }));
    (desktop.browserHasView as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("gone"));
    const selected = await run("tabs", { action: "select", tab_id: "unknown" });
    expect(selected.ok).toBe(false);
    expect(main.record(id)?.agentTab).toBeNull();
    expect(main.record(id)?.selected).toBe("unknown");
  });

  it("names no tab whose page changed after the view was read", async () => {
    main.otherWindow(id, (r) => ({ ...r, tabs: ["moving"], agentTab: "moving" }));
    const hasView = desktop.browserHasView as ReturnType<typeof vi.fn>;
    hasView
      .mockResolvedValueOnce({ exists: true, url: "http://localhost:3000/secret", title: "Secret" })
      .mockResolvedValue({ exists: true, url: "https://public.example/", title: "Public" });
    const listed = await run("tabs", { action: "list" });
    const tabs = listed.data?.tabs as { tab_id: string; url: string; title: string }[];
    expect(tabs.find((t) => t.tab_id === "moving")).toMatchObject({ url: "", title: "" });
  });

  it("checks each tab's page with the session's host, whatever the probed view needed", async () => {
    main.otherWindow(id, (r) => ({ ...r, tabs: ["local"], agentTab: "local" }));
    (desktop.browserHasView as ReturnType<typeof vi.fn>).mockResolvedValue({ exists: true });
    perform().mockClear();
    await run("tabs", { action: "list" });
    const checks = perform().mock.calls.filter((c) => c[1] === "check_page");
    expect(checks.length).toBeGreaterThan(0);
    for (const call of checks) {
      expect(call[3]).toEqual({ sessionHostProtocol: 1, sourceHostId: "source-host" });
      // Listing changes no tab's policy.
      expect(call[2]).not.toHaveProperty("select");
    }
  });
});
