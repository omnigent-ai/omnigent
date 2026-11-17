// The session's browser tabs while the agent tools keep them
// in main (tabStore.ts, agentTabs.ts), as the rail and browser_tabs see them.
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
// Sessions here were created with the advanced browser tools.
vi.mock("@/lib/sessionsApi", () => ({
  getSessionSlim: async (id: string) => ({ id, labels: { "omnigent.browser_agent_tools": "on" } }),
}));
import { emitBrowserActionRequest } from "@/lib/browserActionBus";
import { writeOpenLinksInApp } from "@/lib/linkOpenPreferences";
import { maybeOpenLinkInApp } from "@/lib/openLinkInApp";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import {
  AGENT_BROWSER_TAB_ID,
  agentBrowserTabId,
  browserViewId,
  openAgentBrowserTab,
  readBrowserTabsState,
  useBrowserTabs,
} from "@/hooks/useBrowserTabs";
import { AGENT_ACTION_EXPIRED_ERROR } from "./deadline";
import { fakeMainTabs, type FakeMainTabs } from "./fakeMainTabs";
import { writeBrowserAgentToolsEnabled } from "./preferences";
import { dispatchAgentBrowserAction, SHOWN_ELSEWHERE_NOTE } from "./relayActions";
import { changeTabs, loadTabs, RESERVED_TAB_ID } from "./tabStore";

interface Result {
  ok: boolean;
  error?: string;
  data?: Record<string, unknown>;
}

let main: FakeMainTabs;
let desktop: Record<string, unknown>;

beforeEach(() => {
  writeBrowserAgentToolsEnabled(true);
  main = fakeMainTabs();
  desktop = {
    browserTabs: main.browserTabs,
    onBrowserTabsChanged: main.onBrowserTabsChanged,
    browserClose: vi.fn().mockResolvedValue({ ok: true, removed: true }),
    browserHasView: vi.fn().mockResolvedValue({ exists: false }),
    // Main's answers to actions (here, check_page: the page is one agents may open).
    browserPerformAction: vi.fn().mockResolvedValue({ ok: true, result: "{}" }),
  };
  Object.assign(window, { omnigentDesktop: desktop });
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  Reflect.deleteProperty(window, "omnigentDesktop");
});

/** Run a browser_tabs (or other) action as the relay does, with `dispatch`
 *  standing in for the relay's own (navigate answers ok unless told otherwise). */
function relay(
  id: string,
  action: string,
  args: Record<string, unknown>,
  dispatch = vi.fn().mockResolvedValue({ ok: true }),
  deadlineAt?: number,
): Promise<Result> {
  return dispatchAgentBrowserAction(
    dispatch,
    id,
    action,
    args,
    desktop,
    null,
    null,
    false,
    deadlineAt === undefined ? {} : { deadlineAt },
  );
}

async function newTab(id: string): Promise<string> {
  let opened: Result = { ok: false };
  await act(async () => {
    opened = await relay(id, "tabs", { action: "new" });
  });
  expect(opened.ok).toBe(true);
  return String(opened.data?.tab_id);
}

/** Another window commits `change` just before this window's next write reaches main. */
function beforeNextUpdate(id: string, change: Parameters<FakeMainTabs["otherWindow"]>[1]): void {
  const browserTabs = main.browserTabs;
  let pendingChange = true;
  desktop.browserTabs = async (args: Record<string, unknown>) => {
    if (args.op === "update" && pendingChange) {
      pendingChange = false;
      main.otherWindow(id, change);
    }
    return browserTabs(args);
  };
}

describe("the rail, with the tools on", () => {
  it("keeps a tab and its page when an agent navigates it while the close starts", async () => {
    const { result } = renderHook(() => useBrowserTabs("s-close"));
    act(() => result.current.add());
    const tab = result.current.selected!;
    await vi.waitFor(() => expect(main.record("s-close")?.tabs).toEqual([tab]));
    // Main refuses a close whose navigation count is out of date, as browserTabs.js does.
    const browserClose = vi.fn(
      async (
        _viewId: string,
        _reason?: string,
        opts?: { expectedNavs?: { tabId: string; count: number } },
      ) =>
        opts?.expectedNavs &&
        (main.record("s-close")?.navs[opts.expectedNavs.tabId] ?? 0) !== opts.expectedNavs.count
          ? { ok: false, removed: false, superseded: true }
          : { ok: true, removed: true },
    );
    desktop.browserClose = browserClose;
    let outcome: unknown;
    await act(async () => {
      outcome = await result.current.close(tab, {
        beforeClose: async () => {
          main.otherWindow("s-close", (r) => ({ ...r, navs: { ...r.navs, [tab]: 1 } }));
        },
      });
    });
    expect(browserClose).toHaveBeenCalledWith(browserViewId("s-close", tab), undefined, {
      expectedNavs: { tabId: tab, count: 0 },
    });
    expect(outcome).toBe("kept");
    expect(result.current.tabs).toEqual([tab]);
  });

  it("keeps the newer saved tabs when an older answer arrives after them", async () => {
    await loadTabs("s-late");
    const browserTabs = main.browserTabs;
    desktop.browserTabs = async (args: Record<string, unknown>) => {
      const answer = await browserTabs(args);
      if (args.op !== "update") return answer;
      // Another window changes the tabs (and saves them) before this answer is read.
      main.otherWindow("s-late", (r) => ({
        ...r,
        tabs: [...r.tabs, "theirs"],
        selected: "theirs",
      }));
      writeSessionWorkspaceState("s-late", {
        openBrowsers: main.record("s-late")!.tabs,
        selectedBrowserId: "theirs",
      });
      return answer;
    };
    const { record } = await changeTabs("s-late", (r) => ({
      ...r,
      tabs: [...r.tabs, "mine"],
      selected: "mine",
    }));
    expect(record.tabs).toEqual(["mine", "theirs"]);
    expect(readSessionWorkspaceState("s-late")).toMatchObject({
      openBrowsers: ["mine", "theirs"],
      selectedBrowserId: "theirs",
    });
  });

  it("keeps its tabs in main, and shows another window's change", async () => {
    const { result } = renderHook(() => useBrowserTabs("s-rail"));
    act(() => result.current.add());
    const mine = result.current.selected!;
    // In main, and saved for the next launch too.
    await vi.waitFor(() =>
      expect(readSessionWorkspaceState("s-rail").openBrowsers).toEqual([mine]),
    );
    expect(main.record("s-rail")?.tabs).toEqual([mine]);
    act(() =>
      main.otherWindow("s-rail", (r) => ({
        ...r,
        tabs: [...r.tabs, "theirs"],
        selected: "theirs",
      })),
    );
    expect(result.current.tabs).toEqual([mine, "theirs"]);
    expect(result.current.selected).toBe("theirs");
  });

  it("keeps both tabs when another window added one first", async () => {
    const { result } = renderHook(() => useBrowserTabs("s-race"));
    await act(() => loadTabs("s-race"));
    beforeNextUpdate("s-race", (r) => ({ ...r, tabs: [...r.tabs, "from-b"] }));
    act(() => result.current.add());
    const mine = result.current.selected!;
    await vi.waitFor(() => expect(main.record("s-race")?.tabs).toEqual(["from-b", mine]));
    expect(result.current.tabs).toEqual(["from-b", mine]);
    expect(result.current.selected).toBe(mine);
  });

  it("starts main from the saved workspace, and leaves it alone while the tools are off", async () => {
    writeSessionWorkspaceState("s-saved", { openBrowsers: ["a", "b"], selectedBrowserId: "b" });
    renderHook(() => useBrowserTabs("s-saved"));
    await vi.waitFor(() => expect(main.record("s-saved")?.tabs).toEqual(["a", "b"]));
    expect(main.record("s-saved")?.selected).toBe("b");

    writeBrowserAgentToolsEnabled(false);
    const before = main.updates.length;
    const { result } = renderHook(() => useBrowserTabs("s-off"));
    act(() => result.current.add());
    expect(readSessionWorkspaceState("s-off").openBrowsers).toEqual([result.current.selected]);
    expect(main.updates.length).toBe(before);
    expect(main.record("s-off")).toBeUndefined();
  });

  it("goes back to the saved tabs when main resets them (the switch went off)", async () => {
    const { result } = renderHook(() => useBrowserTabs("s-reset"));
    act(() => result.current.add());
    const tab = result.current.selected!;
    await vi.waitFor(() =>
      expect(readSessionWorkspaceState("s-reset").openBrowsers).toEqual([tab]),
    );
    writeBrowserAgentToolsEnabled(false);
    await act(() => main.browserTabs({ op: "reset" }));
    expect(readBrowserTabsState("s-reset").tabs).toEqual([tab]);
  });
});

describe("the agent's tab", () => {
  it("follows browser_tabs, not the user's own tabs, and comes back on navigate", async () => {
    const { result } = renderHook(() => useBrowserTabs("s-agent"));
    expect(agentBrowserTabId("s-agent")).toBe(AGENT_BROWSER_TAB_ID);
    const agentTab = await newTab("s-agent");
    expect(agentBrowserTabId("s-agent")).toBe(agentTab);
    expect(result.current.selected).toBe(agentTab);
    act(() => result.current.add());
    expect(agentBrowserTabId("s-agent")).toBe(agentTab);
    act(() =>
      emitBrowserActionRequest(
        { type: "browser_action_request", actionId: "nav", action: "navigate", args: {} },
        "s-agent",
      ),
    );
    expect(result.current.selected).toBe(agentTab);
    expect((await relay("s-agent", "tabs", { action: "select", tab_id: "missing" })).ok).toBe(
      false,
    );
  });

  it("opens in-app links in a new tab, not the agent's current one", async () => {
    const browserOpenOrNavigate = vi.fn().mockResolvedValue({ ok: true });
    Object.assign(desktop, { kind: "electron", browserOpenOrNavigate });
    writeOpenLinksInApp(true);
    const { result } = renderHook(() => useBrowserTabs("s-link"));
    const agentTab = await newTab("s-link");
    expect(maybeOpenLinkInApp("s-link", "https://example.com/")).toBe(true);
    await vi.waitFor(() => expect(result.current.selected).not.toBe(agentTab));
    const linkTab = result.current.selected!;
    expect(linkTab).not.toBe(AGENT_BROWSER_TAB_ID);
    expect(browserOpenOrNavigate).toHaveBeenCalledWith(
      browserViewId("s-link", linkTab),
      "https://example.com/",
      undefined,
      {
        agent: true,
      },
    );
  });

  it.each(["rail", "agent"] as const)(
    "settles on the right tab after a closed agent tab, not on later selections (%s close)",
    async (closer) => {
      const id = `s-close-${closer}`;
      const { result } = renderHook(() => useBrowserTabs(id));
      act(() => result.current.add());
      const left = result.current.selected!;
      const agentTab = await newTab(id);
      act(() => result.current.add());
      const right = result.current.selected!;
      act(() => result.current.select(agentTab));
      await act(async () => {
        if (closer === "rail") await result.current.close(agentTab);
        else await relay(id, "tabs", { action: "close", tab_id: agentTab });
      });
      await vi.waitFor(() => expect(main.record(id)?.tabs).toEqual([left, right]));
      // A close in the rail adopts the tab shown instead; the agent's own goes back to its reserved tab.
      const after = closer === "rail" ? left : AGENT_BROWSER_TAB_ID;
      expect(agentBrowserTabId(id)).toBe(after);
      act(() => result.current.select(right));
      await vi.waitFor(() => expect(main.record(id)?.selected).toBe(right));
      expect(agentBrowserTabId(id)).toBe(after);
    },
  );

  it("moves off the reserved tab when it closes, though agents never chose it", async () => {
    const { result } = renderHook(() => useBrowserTabs("s-reserved"));
    act(() => result.current.add());
    const userTab = result.current.selected!;
    act(() => openAgentBrowserTab("s-reserved"));
    await act(() => result.current.close(AGENT_BROWSER_TAB_ID));
    await vi.waitFor(() => expect(main.record("s-reserved")?.tabs).toEqual([userTab]));
    expect(agentBrowserTabId("s-reserved")).toBe(userTab);
    expect(RESERVED_TAB_ID).toBe(AGENT_BROWSER_TAB_ID);
  });

  it("keeps a tab navigated, in any window, after its close began", async () => {
    const finishing: ((r: { ok: boolean; removed: boolean }) => void)[] = [];
    const finish = (r: { ok: boolean; removed: boolean }) => finishing.forEach((f) => f(r));
    desktop.browserClose = () =>
      new Promise((resolve) => {
        finishing.push(resolve);
      });
    const { result } = renderHook(() => useBrowserTabs("s-late"));
    const first = await newTab("s-late");
    const second = await newTab("s-late");
    const closing = relay("s-late", "tabs", { action: "close", tab_id: second });
    const railClosing = result.current.close(first);
    await vi.waitFor(() => expect(finishing).toHaveLength(2));
    // The agent navigates both tabs again, one from another window.
    act(() => openAgentBrowserTab("s-late", second));
    act(() =>
      main.otherWindow("s-late", (r) => ({
        ...r,
        navs: { ...r.navs, [first]: (r.navs[first] ?? 0) + 1 },
      })),
    );
    await act(async () => {
      finish({ ok: true, removed: true });
      await Promise.all([closing, railClosing]);
    });
    await vi.waitFor(() => expect(main.updates.at(-1)?.next).toBeDefined());
    expect(main.record("s-late")?.tabs).toEqual([first, second]);
    expect(agentBrowserTabId("s-late")).toBe(second);
  });
});

describe("browser_tabs new", () => {
  const waitingNavigate = () => {
    let answer: (r: Result) => void = () => {};
    const dispatch = vi.fn(
      async (_viewId: string, action: string, args: Record<string, unknown>) =>
        action === "navigate" && String(args.url).includes("slow")
          ? new Promise<Result>((resolve) => {
              answer = resolve;
            })
          : { ok: true },
    );
    return { dispatch, answer: (r: Result) => answer(r) };
  };

  it("isn't the agent's tab until its page is accepted, and a select meanwhile stays shown", async () => {
    const id = "s-loading";
    const first = await newTab(id);
    const { dispatch, answer } = waitingNavigate();
    const opening = relay(id, "tabs", { action: "new", url: "https://slow.example/" }, dispatch);
    await vi.waitFor(() => expect(dispatch).toHaveBeenCalled());
    expect(agentBrowserTabId(id)).toBe(first);
    await relay(id, "navigate", { url: "https://example.com/" }, dispatch);
    expect(dispatch.mock.lastCall?.slice(0, 2)).toEqual([browserViewId(id, first), "navigate"]);
    expect((await relay(id, "tabs", { action: "select", tab_id: first })).ok).toBe(true);
    answer({ ok: true });
    const created = String((await opening).data?.tab_id);
    expect(agentBrowserTabId(id)).toBe(created);
    expect(main.record(id)?.selected).toBe(first);
  });

  it("leaves no tab when its first page is refused, keeping a select made meanwhile", async () => {
    const id = "s-refused";
    const other = await newTab(id);
    const { dispatch, answer } = waitingNavigate();
    const opening = relay(id, "tabs", { action: "new", url: "https://slow.example/" }, dispatch);
    await vi.waitFor(() => expect(dispatch).toHaveBeenCalled());
    expect((await relay(id, "tabs", { action: "select", tab_id: other })).ok).toBe(true);
    answer({ ok: false, error: "blocked: internal host" });
    expect(await opening).toEqual({ ok: false, error: "blocked: internal host" });
    expect(main.record(id)).toMatchObject({ tabs: [other], selected: other, agentTab: other });
  });

  it("stays for the user when their navigation took its first page, the agent where it was", async () => {
    const id = "s-taken";
    const first = await newTab(id);
    // Main cancelled the agent's first page for the user's typed address.
    Object.assign(desktop, {
      browserOpenOrNavigate: vi.fn().mockResolvedValue({ ok: false, superseded: true }),
    });
    // The relay's dispatch passes on only ok and error.
    const dispatch = vi.fn(
      async (viewId: string, _action: string, args: Record<string, unknown>, d: object) => {
        const open = (d as { browserOpenOrNavigate: (...a: unknown[]) => Promise<Result> })
          .browserOpenOrNavigate;
        const r = await open(viewId, args.url);
        return { ok: r.ok, error: "navigation superseded or closed" };
      },
    );
    const r = await relay(id, "tabs", { action: "new", url: "https://x.example/" }, dispatch);
    expect(r.ok).toBe(false);
    expect(main.record(id)?.tabs).toHaveLength(2);
    expect(agentBrowserTabId(id)).toBe(first);
  });

  it("refuses past 20 tabs", async () => {
    main.otherWindow("s-full", (r) => ({
      ...r,
      tabs: Array.from({ length: 20 }, (_, i) => `t${i}`),
    }));
    const r = await relay("s-full", "tabs", { action: "new" });
    expect(r).toEqual({ ok: false, error: expect.stringMatching(/already has 20 tabs/) });
    expect(main.record("s-full")?.tabs).toHaveLength(20);
  });

  it("changes nothing when the deadline passes before main gets the change", async () => {
    await loadTabs("s-deadline");
    const before = main.record("s-deadline");
    // The change reaches main only after the action's deadline.
    const browserTabs = main.browserTabs;
    desktop.browserTabs = async (args: Record<string, unknown>) => {
      if (args.op === "update") {
        await new Promise((resolve) => {
          setTimeout(resolve, 30);
        });
      }
      return browserTabs(args);
    };
    const r = await relay("s-deadline", "tabs", { action: "new" }, undefined, Date.now() + 15);
    expect(r).toEqual({ ok: false, error: AGENT_ACTION_EXPIRED_ERROR });
    expect(main.record("s-deadline")).toEqual(before);
  });

  it("adds both of two windows' new tabs", async () => {
    const id = "s-two";
    await loadTabs(id);
    beforeNextUpdate(id, (r) => ({ ...r, tabs: [...r.tabs, "from-b"] }));
    const mine = await newTab(id);
    expect(main.record(id)?.tabs).toEqual(["from-b", mine]);
    expect(agentBrowserTabId(id)).toBe(mine);
  });
});

describe("browser_tabs list", () => {
  it("names each tab's page wherever it is, and says when it's in another window", async () => {
    const id = "s-list";
    const here = await newTab(id);
    const there = await newTab(id);
    desktop.browserHasView = vi.fn(async (viewId: string) =>
      viewId === browserViewId(id, here)
        ? { exists: true, url: "https://here.example/", title: "Here" }
        : { exists: false, elsewhere: true, url: "https://there.example/", title: "There" },
    );
    const listed = await relay(id, "tabs", { action: "list" });
    expect(listed.data).toEqual({
      tabs: [
        { tab_id: here, url: "https://here.example/", title: "Here", current: false },
        { tab_id: there, url: "https://there.example/", title: "There", current: true },
      ],
      current_tab_id: there,
    });
    const selected = await relay(id, "tabs", { action: "select", tab_id: there });
    expect(selected.data).toMatchObject({
      shown_in_other_window: true,
      note: SHOWN_ELSEWHERE_NOTE,
    });
  });

  it("doesn't select a tab showing a page agents in this session can't open", async () => {
    const id = "s-internal";
    const mine = await newTab(id);
    const users = await newTab(id);
    await relay(id, "tabs", { action: "select", tab_id: mine });
    desktop.browserHasView = vi
      .fn()
      .mockResolvedValue({ exists: true, url: "http://127.0.0.1:8080/" });
    const perform = desktop.browserPerformAction as ReturnType<typeof vi.fn>;
    perform.mockImplementation(async (_viewId: string, action: string) =>
      action === "check_page"
        ? {
            ok: false,
            error:
              "this tab shows a page agents in this session can't open (loopback), so it wasn't selected",
          }
        : { ok: true, result: "{}" },
    );
    const refused = await relay(id, "tabs", { action: "select", tab_id: users });
    expect(refused).toEqual({
      ok: false,
      error:
        "this tab shows a page agents in this session can't open (loopback), so it wasn't selected",
    });
    // Selection asks main to give the tab this session's policy once it passes.
    expect(perform).toHaveBeenCalledWith(browserViewId(id, users), "check_page", {
      provenance: { sourceHostId: null },
      select: true,
      deadlineAt: expect.any(Number),
    });
    expect(agentBrowserTabId(id)).toBe(mine);
  });
});

describe("a change made while an earlier one is still on its way to main", () => {
  it("applies a selection another window's unseen change has made look like no change", async () => {
    const id = "s-unseen";
    main.otherWindow(id, (r) => ({ ...r, tabs: ["a", "b"], selected: "a" }));
    await loadTabs(id);
    // Another window selects b; its broadcast hasn't reached this window.
    const resumePushes = main.holdPushes();
    main.otherWindow(id, (r) => ({ ...r, selected: "b" }));
    const { applied } = await changeTabs(id, (r) => ({ ...r, selected: "a" }));
    resumePushes();
    expect(applied).toBe(true);
    expect(main.record(id)?.selected).toBe("a");
  });

  it("keeps the latest selection when it returns to the one main still has", async () => {
    const id = "s-aba";
    main.otherWindow(id, (r) => ({ ...r, tabs: ["a", "b"], selected: "a" }));
    await loadTabs(id);
    // Main takes this window's writes in order, but its answer to the first
    // (and the broadcast of it) arrives late.
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const answer = main.browserTabs;
    const resumePushes = main.holdPushes();
    let held = false;
    desktop.browserTabs = async (args: Record<string, unknown>) => {
      const result = await answer(args);
      if (args.op === "update" && !held) {
        held = true;
        await gate;
      }
      return result;
    };
    const toB = changeTabs(id, (r) => ({ ...r, selected: "b" }));
    const backToA = changeTabs(id, (r) => ({ ...r, selected: "a" }));
    release();
    resumePushes();
    await Promise.all([toB, backToA]);
    expect(main.record(id)?.selected).toBe("a");
  });
});
