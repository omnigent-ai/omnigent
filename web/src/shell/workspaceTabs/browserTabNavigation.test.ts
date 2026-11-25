import { afterEach, describe, expect, it, vi } from "vitest";

import {
  describeHistory,
  FakeBrowserBridge,
  HistoryWorkspace,
} from "./__fixtures__/historyWorkspace";
import { REPLAY_WAIT_MS, type BrowserTabNavigation, type LandResult } from "./browserTabNavigation";
import { recordStep } from "./workspaceHistory";
import { formatWorkspaceLink, parseWorkspaceLink } from "./workspaceLink";
import type { Visit } from "./workspacePlace";

const page = (path: string) => `https://x.test${path}`;

let checkedWorkspace: HistoryWorkspace | null = null;

function expectInvariant(ws: HistoryWorkspace, after: string): void {
  if (!ws.invariantHolds()) {
    throw new Error(`invariant broken after ${after}: ${describeHistory(ws.history)}`);
  }
}

/** `target` whose every method call, and every promise one returns, is followed by an invariant check. */
function checked<T extends object>(target: T, ws: HistoryWorkspace): T {
  return new Proxy(target, {
    get(object, key, receiver) {
      const value: unknown = Reflect.get(object, key, receiver);
      if (typeof value !== "function") return value;
      return (...args: unknown[]) => {
        const result: unknown = value.apply(object, args);
        expectInvariant(ws, String(key));
        if (!(result instanceof Promise)) return result;
        return result.then((settled: unknown) => {
          expectInvariant(ws, `${String(key)} settled`);
          return settled;
        });
      };
    },
  });
}

/** A workspace whose browser tab W shows `path`, selected, with a Files tab F after it. */
function browserWorkspace(mode: "replay" | "untracked", path: string) {
  const bridge = new FakeBrowserBridge(mode);
  const raw = new HistoryWorkspace({ id: "W", place: { kind: "web", url: page(path) } }, bridge);
  const owner = raw.attachBrowser("W", { url: page(path) });
  raw.tabs.push({ id: "F", place: { kind: "files", path: "a" } });
  checkedWorkspace = raw;
  return { ws: checked(raw, raw), bridge, owner: checked(owner, raw) };
}

function settle<T>(promise: Promise<T> | undefined): { result: T | undefined } {
  const box: { result: T | undefined } = { result: undefined };
  void promise?.then((value) => {
    box.result = value;
  });
  return box;
}

/** The user opens `path` from the tab's URL bar: a step, then a landing. */
function openFromUrlBar(ws: HistoryWorkspace, path: string): Visit {
  const visit: Visit = { tabId: "W", place: { kind: "web", url: page(path) } };
  ws.history = recordStep(ws.history, visit);
  return visit;
}

/** Holds the bridge's acknowledgement of loading `path` until the returned function runs. */
function holdLoadOf(bridge: FakeBrowserBridge, path: string): () => void {
  let release = () => {};
  bridge.openOrNavigate = (viewKey, url) => {
    bridge.openOrNavigateCalls.push({ viewKey, url });
    if (url !== page(path)) return Promise.resolve();
    return new Promise<void>((resolve) => {
      release = resolve;
    });
  };
  return () => release();
}

/** Every visit and the tab's place survive a round trip through the link grammar. */
function expectCanonical(ws: HistoryWorkspace): void {
  for (const place of [...ws.history.visits.map((visit) => visit.place), ws.tab("W").place]) {
    const parsed = parseWorkspaceLink(formatWorkspaceLink({ place }));
    expect(parsed.ok && parsed.link.place).toEqual(place);
  }
}

afterEach(() => {
  vi.useRealTimers();
  if (checkedWorkspace) expectInvariant(checkedWorkspace, "the test");
  checkedWorkspace = null;
});

describe("browser pages in the timeline, replay mode", () => {
  it("14. landing on another page replays it; Back/Forward into the tab wait for its URL event", async () => {
    const { ws, bridge, owner } = browserWorkspace("replay", "/a");
    owner.onUrlChanged({ viewKey: "view-W", url: page("/b") });
    ws.select("F");
    expect(describeHistory(ws.history)).toBe("[W:/a, W:/b, F:a*]");
    await ws.back();
    expect(bridge.openOrNavigateCalls).toEqual([]);

    const replay = await ws.back();
    expect(bridge.openOrNavigateCalls).toEqual([{ viewKey: "view-W", url: page("/a") }]);
    expect(ws.canGoForward()).toBe(false);
    expect(owner.isWaitingForReplay()).toBe(true);

    owner.onUrlChanged({ viewKey: "view-W", url: page("/a-redirected") });
    expect(await replay.landing).toBe("landed");
    expect(describeHistory(ws.history)).toBe("[W:/a-redirected*, W:/b, F:a]");
    expect(ws.canGoForward()).toBe(true);
    expect(ws.invariantHolds()).toBe(true);
  });

  it("a URL event with the committed URL (title, load) is not a step", () => {
    const { ws, owner } = browserWorkspace("replay", "/a");
    owner.onUrlChanged({ viewKey: "view-W", url: page("/a") });
    expect(describeHistory(ws.history)).toBe("[W:/a*]");
  });

  it.each([
    ["the user acts in the tab", (owner: BrowserTabNavigation) => owner.userActed()],
    ["the tab is deselected", (owner: BrowserTabNavigation) => owner.deselected()],
  ])("the wait ends when %s, and later URL events are ordinary", async (_name, end) => {
    const { ws, owner } = browserWorkspace("replay", "/a");
    owner.onUrlChanged({ viewKey: "view-W", url: page("/b") });
    const replay = await ws.back();
    end(owner);
    expect(await replay.landing).toBe("superseded");
    expect(owner.isWaitingForReplay()).toBe(false);
    owner.onUrlChanged({ viewKey: "view-W", url: page("/c") });
    expect(describeHistory(ws.history)).toBe("[W:/a, W:/c*]");
  });

  it("the wait ends after 15 seconds without claiming a failure", async () => {
    vi.useFakeTimers();
    const { ws, owner } = browserWorkspace("replay", "/a");
    owner.onUrlChanged({ viewKey: "view-W", url: page("/b") });
    const replay = await ws.back();
    const landing = settle(replay.landing);
    await vi.advanceTimersByTimeAsync(REPLAY_WAIT_MS - 1);
    expect(owner.isWaitingForReplay()).toBe(true);
    await vi.advanceTimersByTimeAsync(1);
    expect(landing.result).toBe<LandResult>("superseded");
    expect(describeHistory(ws.history)).toBe("[W:/a*, W:/b]");
  });

  it("closing the tab ends the wait", async () => {
    const { ws, owner } = browserWorkspace("replay", "/a");
    owner.onUrlChanged({ viewKey: "view-W", url: page("/b") });
    const replay = await ws.back();
    await ws.close("W");
    expect(await replay.landing).toBe("stale");
    expect(owner.isWaitingForReplay()).toBe(false);
  });

  it("a click during a replay is taken as its landing (documented limit)", async () => {
    const { ws, owner } = browserWorkspace("replay", "/a");
    owner.onUrlChanged({ viewKey: "view-W", url: page("/b") });
    const replay = await ws.back();
    owner.onUrlChanged({ viewKey: "view-W", url: page("/clicked") });
    expect(await replay.landing).toBe("landed");
    expect(describeHistory(ws.history)).toBe("[W:/clicked*, W:/b]");
  });

  it("a replay whose load can't be issued fails", async () => {
    const { ws, bridge, owner } = browserWorkspace("replay", "/a");
    owner.onUrlChanged({ viewKey: "view-W", url: page("/b") });
    bridge.openOrNavigate = () => Promise.reject(new Error("no window"));
    const replay = await ws.back();
    expect(await replay.landing).toBe("failed");
  });
});

describe("browser pages in the timeline, no URL events", () => {
  it("landing takes the requested URL on acknowledgement, and page clicks add nothing", async () => {
    const { ws, bridge, owner } = browserWorkspace("untracked", "/a");
    // Opening /b from the URL bar is the only way this client learns a new page.
    expect(await owner.land(openFromUrlBar(ws, "/b"))).toBe("landed");
    owner.onUrlChanged({ viewKey: "view-W", url: page("/c") });
    expect(describeHistory(ws.history)).toBe("[W:/a, W:/b*]");
    bridge.openOrNavigateCalls.length = 0;

    const back = await ws.back();
    expect(await back.landing).toBe("landed");
    expect(bridge.openOrNavigateCalls).toEqual([{ viewKey: "view-W", url: page("/a") }]);
  });

  it("only the newest of two quick landings sets the page", async () => {
    const { ws, bridge, owner } = browserWorkspace("untracked", "/a");
    const releaseFirst = holdLoadOf(bridge, "/b");
    const first = owner.land(openFromUrlBar(ws, "/b"));
    const second = owner.land(openFromUrlBar(ws, "/c"));
    expect(await second).toBe("landed");
    releaseFirst();
    expect(await first).toBe("superseded");
    expect(ws.tab("W").place).toEqual({ kind: "web", url: page("/c") });
  });

  it("landing on the page still showing while another load is in flight loads it again", async () => {
    const { ws, bridge, owner } = browserWorkspace("untracked", "/a");
    const releaseFirst = holdLoadOf(bridge, "/b");
    const toB = owner.land(openFromUrlBar(ws, "/b"));
    const back = await ws.back();
    expect(bridge.openOrNavigateCalls.map((call) => call.url)).toEqual([page("/b"), page("/a")]);
    expect(await back.landing).toBe("landed");
    releaseFirst();
    expect(await toB).toBe("superseded");
    expect(ws.tab("W").place).toEqual({ kind: "web", url: page("/a") });
  });

  it("a load that can't be issued fails and shows the page that is really there", async () => {
    const { ws, bridge, owner } = browserWorkspace("untracked", "/a");
    bridge.openOrNavigate = () => Promise.reject(new Error("no window"));
    expect(await owner.land(openFromUrlBar(ws, "/b"))).toBe("failed");
    expect(describeHistory(ws.history)).toBe("[W:/a*, W:/b]");
    expect(ws.tab("W").place).toEqual({ kind: "web", url: page("/a") });
  });

  it("a load that fails after a newer one started is superseded, and one after close is stale", async () => {
    const { ws, bridge, owner } = browserWorkspace("untracked", "/a");
    let rejectFirst = (_error: Error) => {};
    bridge.openOrNavigate = (_viewKey, url) =>
      url === page("/b")
        ? new Promise<void>((_resolve, reject) => {
            rejectFirst = reject;
          })
        : Promise.resolve();
    const first = owner.land(openFromUrlBar(ws, "/b"));
    expect(await owner.land(openFromUrlBar(ws, "/c"))).toBe("landed");
    rejectFirst(new Error("no window"));
    expect(await first).toBe("superseded");
    expect(ws.tab("W").place).toEqual({ kind: "web", url: page("/c") });

    const third = owner.land(openFromUrlBar(ws, "/b"));
    await ws.close("W");
    rejectFirst(new Error("no window"));
    expect(await third).toBe("stale");
  });
});

describe("blank pages", () => {
  it("a blank page lands without loading anything while the view is blank", async () => {
    const { ws, bridge } = browserWorkspace("replay", "/a");
    const blank = ws.attachBrowser("B");
    ws.openSelected({ id: "B", place: { kind: "web" } });
    expect(await blank.land({ tabId: "B", place: { kind: "web" } })).toBe("landed");
    expect(bridge.openOrNavigateCalls).toEqual([]);
  });

  it("replay mode records a blank page as the canonical blank place", async () => {
    const { ws, owner } = browserWorkspace("replay", "/a");
    owner.onUrlChanged({ viewKey: "view-W", url: "about:blank" });
    expect(describeHistory(ws.history)).toBe("[W:/a, W*]");
    const back = await ws.back();
    owner.onUrlChanged({ viewKey: "view-W", url: page("/a") });
    expect(await back.landing).toBe("landed");
    const forward = await ws.forward();
    owner.onUrlChanged({ viewKey: "view-W", url: "about:blank" });
    expect(await forward.landing).toBe("landed");
    expect(describeHistory(ws.history)).toBe("[W:/a, W*]");
    expectCanonical(ws);
  });

  it("a failed landing reconciles onto a blank page's canonical visit", async () => {
    const { ws, bridge, owner } = browserWorkspace("replay", "/a");
    owner.onUrlChanged({ viewKey: "view-W", url: "about:blank" });
    bridge.openOrNavigate = () => Promise.reject(new Error("no window"));
    const back = await ws.back();
    expect(await back.landing).toBe("failed");
    expect(describeHistory(ws.history)).toBe("[W:/a, W*]");
    expect(ws.tab("W").place).toEqual({ kind: "web" });
    expectCanonical(ws);
  });
});

describe("landings racing each other", () => {
  it("a title event of the page being left doesn't end a replay", async () => {
    const { ws, owner } = browserWorkspace("replay", "/a");
    owner.onUrlChanged({ viewKey: "view-W", url: page("/b") });
    const replay = await ws.back();
    owner.onUrlChanged({ viewKey: "view-W", url: page("/b") });
    expect(owner.isWaitingForReplay()).toBe(true);
    expect(describeHistory(ws.history)).toBe("[W:/a*, W:/b]");

    owner.onUrlChanged({ viewKey: "view-W", url: page("/a") });
    expect(await replay.landing).toBe("landed");
    expect(describeHistory(ws.history)).toBe("[W:/a*, W:/b]");
    expect(ws.canGoForward()).toBe(true);
  });

  it("an older replay's page arriving during a newer replay isn't its landing", async () => {
    const { ws, owner } = browserWorkspace("replay", "/a");
    owner.onUrlChanged({ viewKey: "view-W", url: page("/b") });
    owner.onUrlChanged({ viewKey: "view-W", url: page("/c") });
    // Back is disabled while a replay waits, so the second landing comes from the store (a restore).
    ws.history = { ...ws.history, cursor: 1 };
    const toB = owner.land(ws.history.visits[1]!);
    ws.history = { ...ws.history, cursor: 0 };
    const toA = owner.land(ws.history.visits[0]!);
    expect(await toB).toBe("superseded");
    owner.onUrlChanged({ viewKey: "view-W", url: page("/b") });
    expect(owner.isWaitingForReplay()).toBe(true);
    owner.onUrlChanged({ viewKey: "view-W", url: page("/a") });
    expect(await toA).toBe("landed");
  });

  it("while a landing loads, the tab shows the visit it asked for", async () => {
    const { ws, owner } = browserWorkspace("replay", "/a");
    owner.onUrlChanged({ viewKey: "view-W", url: page("/b") });
    await ws.back();
    expect(ws.tab("W").place).toEqual({ kind: "web", url: page("/a") });
    expect(owner.isWaitingForReplay()).toBe(true);
  });
});
