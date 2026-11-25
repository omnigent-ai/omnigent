// The desktop shell adapter with a real navigation owner: which mode each shell
// gets, and that a load still waiting for the session's host is never issued once
// the owner no longer wants it (closed, superseded, the user acted, timed out).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BrowserTabNavigation, REPLAY_WAIT_MS } from "./browserTabNavigation";
import {
  desktopBrowserNavigationMode,
  desktopBrowserNavigationShell,
} from "./desktopBrowserNavigation";
import { historyStartingAt } from "./workspaceHistory";

const hostLookups: { resolve: (hostId: string | null) => void }[] = [];
vi.mock("@/lib/fetchSessionHostId", () => ({
  fetchSessionHostId: () =>
    new Promise<string | null>((resolve) => {
      hostLookups.push({ resolve });
    }),
}));

const VIEW = "browser-tab:s1:w";
const A = "https://x.test/a";
const B = "https://x.test/b";

function setBridge(bridge: Record<string, unknown> | undefined) {
  const target = window as unknown as Record<string, unknown>;
  if (bridge) target.omnigentDesktop = { kind: "electron", ...bridge };
  else delete target.omnigentDesktop;
}

const replayShell = () => ({
  browserOpenOrNavigate: vi.fn().mockResolvedValue({ ok: true }),
  onBrowserUrlChanged: vi.fn(() => () => {}),
  browserHasView: vi.fn().mockResolvedValue({ exists: false }),
});

function owner() {
  const shell = desktopBrowserNavigationShell()!;
  let history = historyStartingAt({ tabId: "W", place: { kind: "web", url: B } });
  return new BrowserTabNavigation("W", VIEW, shell, {
    isSelected: () => true,
    history: () => history,
    apply: (change) => {
      if (change.history) history = change.history;
    },
  });
}

const flush = () => vi.advanceTimersByTimeAsync(0);

beforeEach(() => {
  vi.useFakeTimers();
  hostLookups.length = 0;
});

afterEach(() => {
  vi.useRealTimers();
  setBridge(undefined);
});

describe("the desktop shell's page history mode", () => {
  it("replays with URL events, is untracked without them, and is absent without a browser", () => {
    // A shell that also offers entry moves still replays by URL.
    setBridge({
      browserOpenOrNavigate: vi.fn(),
      browserGoToEntry: vi.fn(),
      onBrowserUrlChanged: vi.fn(),
    });
    expect(desktopBrowserNavigationMode()).toBe("replay");
    setBridge({ browserOpenOrNavigate: vi.fn() });
    expect(desktopBrowserNavigationMode()).toBe("untracked");
    setBridge(undefined);
    expect(desktopBrowserNavigationShell()).toBeNull();
  });
});

describe("a load waiting for the session's host", () => {
  it("is issued with the host once it resolves, while still wanted", async () => {
    const bridge = replayShell();
    setBridge(bridge);
    void owner().land({ tabId: "W", place: { kind: "web", url: A } });
    hostLookups[0]!.resolve("host_1");
    await flush();
    expect(bridge.browserOpenOrNavigate).toHaveBeenCalledWith(
      VIEW,
      A,
      undefined,
      expect.objectContaining({ force: true, sourceHostId: "host_1" }),
    );
  });

  it("is never issued after the tab closed", async () => {
    const bridge = replayShell();
    setBridge(bridge);
    const navigation = owner();
    void navigation.land({ tabId: "W", place: { kind: "web", url: A } });
    navigation.dispose();
    hostLookups[0]!.resolve("host_1");
    await flush();
    expect(bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
  });

  it("is never issued after a newer landing superseded it; only the newer one loads", async () => {
    const bridge = replayShell();
    setBridge(bridge);
    const navigation = owner();
    void navigation.land({ tabId: "W", place: { kind: "web", url: A } });
    void navigation.land({ tabId: "W", place: { kind: "web", url: B } });
    hostLookups[1]!.resolve("host_1");
    hostLookups[0]!.resolve("host_1");
    await flush();
    expect(bridge.browserOpenOrNavigate.mock.calls.map((call) => call[1])).toEqual([B]);
  });

  it("is never issued after the user used the page's own controls, or the wait timed out", async () => {
    const bridge = replayShell();
    setBridge(bridge);
    const navigation = owner();
    void navigation.land({ tabId: "W", place: { kind: "web", url: A } });
    navigation.userActed();
    hostLookups[0]!.resolve(null);
    await flush();

    void navigation.land({ tabId: "W", place: { kind: "web", url: A } });
    await vi.advanceTimersByTimeAsync(REPLAY_WAIT_MS);
    hostLookups[1]!.resolve(null);
    await flush();
    expect(bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
  });

  it("on a shell without URL events, isn't issued after the user took over the page", async () => {
    const bridge = { browserOpenOrNavigate: vi.fn().mockResolvedValue({ ok: true }) };
    setBridge(bridge);
    const navigation = owner();
    const landed = navigation.land({ tabId: "W", place: { kind: "web", url: A } });
    navigation.userActed();
    hostLookups[0]!.resolve("host_1");
    await expect(landed).resolves.toBe("superseded");
    expect(bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
  });

  it("on a shell without URL events, is still issued after only the selection moved", async () => {
    const bridge = { browserOpenOrNavigate: vi.fn().mockResolvedValue({ ok: true }) };
    setBridge(bridge);
    const navigation = owner();
    const landed = navigation.land({ tabId: "W", place: { kind: "web", url: A } });
    navigation.deselected();
    hostLookups[0]!.resolve("host_1");
    await expect(landed).resolves.toBe("landed");
    expect(bridge.browserOpenOrNavigate.mock.calls.map((call) => call[1])).toEqual([A]);
  });

  it("on a shell without URL events, a closed tab's load isn't issued either", async () => {
    const bridge = { browserOpenOrNavigate: vi.fn().mockResolvedValue({ ok: true }) };
    setBridge(bridge);
    const navigation = owner();
    const landed = navigation.land({ tabId: "W", place: { kind: "web", url: A } });
    navigation.dispose();
    hostLookups[0]!.resolve(null);
    await expect(landed).resolves.toBe("stale");
    expect(bridge.browserOpenOrNavigate).not.toHaveBeenCalled();
  });
});
