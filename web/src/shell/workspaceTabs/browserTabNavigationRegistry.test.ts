// The tab store with real browser navigation owners and a fake shell: Back and
// Forward across tabs load each page's recorded URL, closing lands through the
// owner, and a reload restores from what each view shows now. Every published
// state (each notification and each save) keeps the cursor on the selected tab.

import { afterEach, describe, expect, it, vi } from "vitest";

import { browserViewId } from "@/lib/browserTabs";

import {
  A,
  B,
  browserTab,
  C,
  files,
  placeOf,
  SESSION,
  setup,
  tick,
  W_VIEW,
} from "./__fixtures__/browserTabsStore";
import { REPLAY_WAIT_MS } from "./browserTabNavigation";
import { attachBrowserTabNavigation } from "./browserTabNavigationRegistry";
import { currentVisit } from "./workspaceHistory";
import { visitOf } from "./workspaceTabsStore";

afterEach(() => {
  vi.useRealTimers();
});

const web = (url: string) => ({ kind: "web" as const, url });

describe("browser tabs in the tab store: Back and Forward", () => {
  it("lands a browser tab on its recorded URL across tabs, publishing each step once", async () => {
    const { store, shell, incoherent } = setup({ views: { [W_VIEW]: { exists: true, url: A } } });
    await tick();
    shell.changeUrl(W_VIEW, B);
    store.select("F");
    const before = store.revision();
    await store.go("back");
    // One commit for the cursor move, the selection and the page's place.
    expect(store.revision()).toBe(before + 1);
    expect(placeOf(store)).toEqual(web(B));
    expect(shell.openOrNavigateCalls).toEqual([]);

    await store.go("back");
    expect(placeOf(store)).toEqual(web(A));
    expect(shell.openOrNavigateCalls).toEqual([{ viewKey: W_VIEW, url: A }]);
    shell.changeUrl(W_VIEW, A);
    expect(store.history.state.cursor).toBe(0);

    await store.go("forward");
    expect(shell.openOrNavigateCalls.at(-1)).toEqual({ viewKey: W_VIEW, url: B });
    expect(incoherent).toEqual([]);
  });

  it("closing the tab after a browser visit lands its page back through the owner", async () => {
    const tabs = [browserTab(A), files];
    const { store, shell, incoherent } = setup({
      tabs,
      selected: "F",
      timeline: [visitOf(tabs[0]), visitOf(files)],
      views: { [W_VIEW]: { exists: true, url: A } },
    });
    await tick();
    // While F is selected, W's page moves on by itself.
    shell.changeUrl(W_VIEW, B);
    await store.close("F");
    expect(store.selectedTabId()).toBe("W");
    expect(placeOf(store)).toEqual(web(A));
    expect(shell.openOrNavigateCalls).toEqual([{ viewKey: W_VIEW, url: A }]);
    expect(incoherent).toEqual([]);
  });

  it("disables stepping into a page still replaying, until its URL arrives", async () => {
    vi.useFakeTimers();
    const tabs = [browserTab(A), files];
    const { store, shell, incoherent } = setup({
      tabs,
      selected: "F",
      timeline: [visitOf(tabs[0]), visitOf(files)],
      views: { [W_VIEW]: { exists: true, url: B } },
    });
    await vi.advanceTimersByTimeAsync(0);
    // The page left /a while F was selected; Back reloads /a by URL.
    await store.go("back");
    expect(shell.openOrNavigateCalls).toEqual([{ viewKey: W_VIEW, url: A }]);
    store.select("F");
    // Selecting another tab ends the wait, as the user moved on.
    expect(store.isVisitBlocked(visitOf(store.findTab("W")!))).toBe(false);

    await store.go("back");
    expect(store.isVisitBlocked(visitOf(store.findTab("W")!))).toBe(true);
    shell.usePageControl(W_VIEW);
    expect(store.isVisitBlocked(visitOf(store.findTab("W")!))).toBe(false);

    await store.go("forward");
    await store.go("back");
    expect(store.isVisitBlocked(visitOf(store.findTab("W")!))).toBe(true);
    shell.changeUrl(W_VIEW, A);
    expect(store.isVisitBlocked(visitOf(store.findTab("W")!))).toBe(false);
    await vi.advanceTimersByTimeAsync(REPLAY_WAIT_MS);
    expect(incoherent).toEqual([]);
  });
});

describe("browser tabs in the tab store: restore and reopen", () => {
  it("lets a live view's page win over what was saved", async () => {
    const { store, incoherent } = setup({ views: { [W_VIEW]: { exists: true, url: B } } });
    await tick();
    expect(placeOf(store)).toEqual(web(B));
    expect(currentVisit(store.history.state)).toEqual({ tabId: "W", place: web(B) });
    expect(incoherent).toEqual([]);
  });

  it("forgets page entries a saved session still carries", async () => {
    const tabs = [browserTab(A, 0), files];
    const { store } = setup({
      tabs,
      selected: "F",
      timeline: [visitOf(tabs[0]), visitOf(files)],
      views: { [W_VIEW]: { exists: true, url: A } },
    });
    await tick();
    expect(store.findTab("W")?.entry).toBeUndefined();
    expect(store.history.state.visits[0]).toEqual({ tabId: "W", place: web(A) });
  });

  it("loads the saved URL in a new view only when the view is really gone", async () => {
    const { store, shell } = setup();
    await tick();
    expect(shell.openOrNavigateCalls).toEqual([{ viewKey: W_VIEW, url: A }]);
    expect(placeOf(store)).toEqual(web(A));
    expect(currentVisit(store.history.state)).toEqual({ tabId: "W", place: web(A) });
  });

  it("leaves a page with no saved URL blank, and a tab the shell can't probe as saved", async () => {
    const blank = setup({ tabs: [browserTab(undefined), files] });
    await tick();
    expect(blank.shell.openOrNavigateCalls).toEqual([]);

    const unknown = setup({ views: { [W_VIEW]: null } });
    await tick();
    expect(unknown.shell.openOrNavigateCalls).toEqual([]);
    expect(placeOf(unknown.store)).toEqual(web(A));
  });

  it("discards a probe that a navigation overtook", async () => {
    const { store, shell } = setup();
    shell.autoProbe = false;
    // The registry probed before autoProbe changed; probe again by reattaching.
    const detachAgain = attachBrowserTabNavigation(store, SESSION, shell);
    shell.changeUrl(W_VIEW, C);
    shell.probes.at(-1)!.resolve({ exists: false });
    await tick();
    expect(placeOf(store)).toEqual(web(C));
    detachAgain();
  });

  it("reopens a closed browser tab as a new view loading its last URL", async () => {
    const { store, shell } = setup({ views: { [W_VIEW]: { exists: true, url: A } } });
    await tick();
    shell.changeUrl(W_VIEW, B);
    await store.close("W");
    const reopened = store.reopenClosed()!;
    await tick();
    expect(store.findTab(reopened)?.place).toEqual(web(B));
    expect(shell.openOrNavigateCalls).toEqual([{ viewKey: browserViewId(SESSION, "v1"), url: B }]);
  });

  it("stops listening and ends pending landings when detached", async () => {
    const { store, shell, detach } = setup({
      views: { [W_VIEW]: { exists: true, url: B } },
      timeline: [{ tabId: "W", place: web(A) }, visitOf(browserTab(B))],
      tabs: [browserTab(B), files],
    });
    await tick();
    await store.go("back");
    detach();
    expect(shell.listenerCount()).toBe(0);
    shell.changeUrl(W_VIEW, C);
    expect(placeOf(store)).toEqual(web(A));
    // With no owner, Forward just shows the visit; there's no page to move.
    await store.go("forward");
    expect(placeOf(store)).toEqual(web(B));
  });
});
