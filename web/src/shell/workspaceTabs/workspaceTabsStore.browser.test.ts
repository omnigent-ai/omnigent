// Browser tabs through the real tab store and navigation owners: links and
// navigations sent to an open browser tab load through its owner, a new view's
// first page, closing during a landing, and the timeline traces replayed end to
// end. Every published state keeps the cursor on the selected tab.

import { describe, expect, it, vi } from "vitest";

import { acceptBrowserPageSubmission, beginBrowserPageSubmission } from "@/lib/browserPageControls";
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
import { currentVisit } from "./workspaceHistory";
import type { Visit } from "./workspacePlace";
import { visitOf } from "./workspaceTabsStore";

const D = "https://x.test/d";
const web = (url: string) => ({ kind: "web" as const, url });
const visitAt = (url: string): Visit => ({ tabId: "W", place: web(url) });
const timelineOf = (visits: readonly Visit[]) =>
  visits.map((visit) =>
    visit.place.kind === "web"
      ? `${visit.tabId}:${visit.place.url ?? "blank"}`
      : `${visit.tabId}:${visit.place.kind}`,
  );
const liveOn = (url: string) => ({ views: { [W_VIEW]: { exists: true as const, url } } });

describe("navigations sent to an open browser tab", () => {
  it("a link aimed at the selected tab loads its page, and the page's URL event confirms it", async () => {
    const { store, shell, incoherent } = setup(liveOn(A));
    await tick();
    const result = await store.open(
      { place: web(B), tab: "W" },
      { opener: "user", disposition: "current" },
    );
    expect(result).toMatchObject({ ok: true, tabId: "W" });
    expect(placeOf(store)).toEqual(web(B));
    expect(currentVisit(store.history.state)).toEqual(visitAt(B));
    expect(shell.openOrNavigateCalls).toEqual([{ viewKey: W_VIEW, url: B }]);
    shell.changeUrl(W_VIEW, B);
    expect(timelineOf(store.history.state.visits)).toEqual([
      "W:https://x.test/a",
      "W:https://x.test/b",
    ]);
    expect(incoherent).toEqual([]);
  });

  it("an agent's open of a background tab loads it there without a step", async () => {
    const { store, shell, incoherent } = setup({
      tabs: [files, browserTab(A)],
      selected: "F",
      ...liveOn(A),
    });
    await tick();
    await store.open({ place: web(B), tab: "W" }, { opener: "agent" });
    expect(placeOf(store)).toEqual(web(B));
    expect(shell.openOrNavigateCalls).toEqual([{ viewKey: W_VIEW, url: B }]);
    shell.changeUrl(W_VIEW, B);
    expect(placeOf(store)).toEqual(web(B));
    expect(timelineOf(store.history.state.visits)).toEqual(["F:files"]);
    expect(incoherent).toEqual([]);
  });

  it("navigate and an agent's attention, once shown, load through the owner too", async () => {
    const { store, shell, incoherent } = setup(liveOn(A));
    await tick();
    await store.navigate("W", web(B));
    expect(shell.openOrNavigateCalls).toEqual([{ viewKey: W_VIEW, url: B }]);
    shell.changeUrl(W_VIEW, B);

    // The selected tab shows what an agent opened as attention until the user shows it.
    await store.open({ place: web(C), tab: "W" }, { opener: "agent" });
    expect(store.findTab("W")?.attention).toEqual({ place: web(C) });
    expect(shell.openOrNavigateCalls).toHaveLength(1);
    await store.showAttention("W");
    expect(placeOf(store)).toEqual(web(C));
    expect(shell.openOrNavigateCalls.at(-1)).toEqual({ viewKey: W_VIEW, url: C });
    shell.changeUrl(W_VIEW, C);
    expect(timelineOf(store.history.state.visits)).toEqual([
      "W:https://x.test/a",
      "W:https://x.test/b",
      "W:https://x.test/c",
    ]);
    expect(incoherent).toEqual([]);
  });

  it("opening the page a tab already shows loads nothing", async () => {
    const { store, shell } = setup(liveOn(A));
    await tick();
    await store.open({ place: web(A), tab: "W" }, { opener: "user", disposition: "current" });
    expect(shell.openOrNavigateCalls).toEqual([]);
    expect(placeOf(store)).toEqual(web(A));
  });
});

describe("an address typed in a browser tab's own URL bar", () => {
  const untrackedOnB = () =>
    setup({
      mode: "untracked",
      tabs: [browserTab(B), files],
      timeline: [visitAt(A), visitAt(B)],
      views: { [W_VIEW]: { exists: true, url: B } },
    });

  it("on a shell that reports no navigations, becomes the tab's page once the load is accepted", async () => {
    const { store, shell, incoherent } = setup({ mode: "untracked", ...liveOn(A) });
    await tick();
    // Typing takes the page over; the submission's accepted load says where it went.
    shell.usePageControl(W_VIEW);
    const submission = beginBrowserPageSubmission(W_VIEW)!;
    expect(placeOf(store)).toEqual(web(A));
    acceptBrowserPageSubmission(W_VIEW, submission, B);
    expect(placeOf(store)).toEqual(web(B));
    expect(timelineOf(store.history.state.visits)).toEqual([
      "W:https://x.test/a",
      "W:https://x.test/b",
    ]);

    // Back to it from another tab finds the page already there; reopening loads it.
    store.select("F");
    await store.go("back");
    expect(shell.openOrNavigateCalls).toEqual([]);
    await store.close("W");
    store.reopenClosed();
    await tick();
    expect(shell.openOrNavigateCalls).toEqual([{ viewKey: browserViewId(SESSION, "v1"), url: B }]);
    expect(incoherent).toEqual([]);
  });

  it("an acceptance arriving after a workspace Back changes nothing (no URL events)", async () => {
    const { store, shell, incoherent } = untrackedOnB();
    await tick();
    const submission = beginBrowserPageSubmission(W_VIEW)!;
    await store.go("back");
    await tick();
    expect(shell.openOrNavigateCalls).toEqual([{ viewKey: W_VIEW, url: A }]);
    acceptBrowserPageSubmission(W_VIEW, submission, C);
    expect(placeOf(store)).toEqual(web(A));
    expect(timelineOf(store.history.state.visits)).toEqual([
      "W:https://x.test/a",
      "W:https://x.test/b",
    ]);
    expect(store.history.state.cursor).toBe(0);
    expect(incoherent).toEqual([]);
  });

  it("an acceptance arriving after a workspace Back leaves the newer replay waiting", async () => {
    const { store, shell, incoherent } = setup({
      tabs: [browserTab(B), files],
      timeline: [visitAt(A), visitAt(B)],
      ...liveOn(B),
    });
    await tick();
    const submission = beginBrowserPageSubmission(W_VIEW)!;
    await store.go("back");
    const isWaiting = () => store.isVisitBlocked(visitOf(store.findTab("W")!));
    expect(isWaiting()).toBe(true);
    acceptBrowserPageSubmission(W_VIEW, submission, C);
    expect(isWaiting()).toBe(true);
    expect(placeOf(store)).toEqual(web(A));
    shell.changeUrl(W_VIEW, A);
    expect(isWaiting()).toBe(false);
    expect(store.history.state.cursor).toBe(0);
    expect(incoherent).toEqual([]);
  });

  it("an acceptance after a reload or an address edit still becomes the page the shell loaded", async () => {
    const { store, shell, incoherent } = untrackedOnB();
    await tick();
    const submission = beginBrowserPageSubmission(W_VIEW)!;
    // The shell issued the typed load; the user reloads before its result arrives.
    shell.usePageControl(W_VIEW);
    acceptBrowserPageSubmission(W_VIEW, submission, C);
    expect(placeOf(store)).toEqual(web(C));
    expect(timelineOf(store.history.state.visits)).toEqual([
      "W:https://x.test/a",
      "W:https://x.test/b",
      "W:https://x.test/c",
    ]);
    expect(incoherent).toEqual([]);
  });

  it("an acceptance for an older submission changes nothing", async () => {
    const { store } = untrackedOnB();
    await tick();
    const older = beginBrowserPageSubmission(W_VIEW)!;
    const newer = beginBrowserPageSubmission(W_VIEW)!;
    acceptBrowserPageSubmission(W_VIEW, older, C);
    expect(placeOf(store)).toEqual(web(B));
    acceptBrowserPageSubmission(W_VIEW, newer, D);
    expect(placeOf(store)).toEqual(web(D));
  });

  it("an acceptance arriving after only the selection moved still becomes the background page", async () => {
    const { store, incoherent } = setup({ mode: "untracked", ...liveOn(A) });
    await tick();
    const submission = beginBrowserPageSubmission(W_VIEW)!;
    store.select("F");
    acceptBrowserPageSubmission(W_VIEW, submission, B);
    expect(placeOf(store)).toEqual(web(B));
    // No step for a tab that isn't selected; selecting it records the page it loaded.
    expect(timelineOf(store.history.state.visits)).toEqual(["W:https://x.test/a", "F:files"]);
    store.select("W");
    expect(timelineOf(store.history.state.visits)).toEqual([
      "W:https://x.test/a",
      "F:files",
      "W:https://x.test/b",
    ]);
    expect(incoherent).toEqual([]);
  });

  it("on a shell that reports navigations, waits for the page's own URL event", async () => {
    const { store, shell } = setup(liveOn(A));
    await tick();
    acceptBrowserPageSubmission(W_VIEW, beginBrowserPageSubmission(W_VIEW)!, B);
    expect(placeOf(store)).toEqual(web(A));
    shell.changeUrl(W_VIEW, B);
    expect(placeOf(store)).toEqual(web(B));
  });
});

describe("a new view whose first page hasn't loaded", () => {
  it("keeps the tab and its load as they are, and takes the first URL event as the page", async () => {
    const { store, shell, incoherent } = setup({ views: { [W_VIEW]: { exists: true, url: "" } } });
    await tick();
    expect(shell.openOrNavigateCalls).toEqual([]);
    expect(placeOf(store)).toEqual(web(A));
    shell.changeUrl(W_VIEW, B);
    expect(placeOf(store)).toEqual(web(B));
    expect(currentVisit(store.history.state)).toEqual(visitAt(B));
    expect(incoherent).toEqual([]);
  });

  it("reopens its saved page when closed before that page loads", async () => {
    const { store, shell } = setup({ views: { [W_VIEW]: { exists: true, url: "" } } });
    await tick();
    await store.close("W");
    store.reopenClosed();
    await tick();
    expect(shell.openOrNavigateCalls.at(-1)).toEqual({
      viewKey: browserViewId(SESSION, "v1"),
      url: A,
    });
  });

  it("never stores an empty URL: an event with one is the canonical blank page", async () => {
    const { store, shell } = setup(liveOn(A));
    await tick();
    shell.changeUrl(W_VIEW, "");
    expect(placeOf(store)).toEqual({ kind: "web" });
    expect(currentVisit(store.history.state)?.place).toEqual({ kind: "web" });
  });
});

describe("closing a browser tab while a landing loads", () => {
  it("reopens the page it last showed while a replay waits", async () => {
    const { store, shell } = setup({
      tabs: [browserTab(B), files],
      timeline: [visitAt(A), visitAt(B)],
      ...liveOn(B),
    });
    await tick();
    await store.go("back");
    expect(shell.openOrNavigateCalls).toEqual([{ viewKey: W_VIEW, url: A }]);
    await store.close("W");
    store.reopenClosed();
    await tick();
    expect(shell.openOrNavigateCalls.at(-1)).toEqual({
      viewKey: browserViewId(SESSION, "v1"),
      url: B,
    });
  });
});

describe("timeline traces through the store", () => {
  it("10. page links are steps; Back onto the page showing loads nothing; Back again replays its URL", async () => {
    const { store, shell, incoherent } = setup(liveOn(A));
    await tick();
    shell.changeUrl(W_VIEW, B);
    store.select("F");
    await store.go("back");
    expect(shell.openOrNavigateCalls).toEqual([]);
    await store.go("back");
    expect(shell.openOrNavigateCalls).toEqual([{ viewKey: W_VIEW, url: A }]);
    expect(placeOf(store)).toEqual(web(A));
    shell.changeUrl(W_VIEW, A);
    expect(store.history.state.cursor).toBe(0);
    expect(incoherent).toEqual([]);
  });

  it("a replay whose load fails puts the cursor back on the page still showing", async () => {
    const { store, shell, incoherent } = setup({
      tabs: [browserTab(B), files],
      timeline: [visitAt(A), visitAt(B)],
      ...liveOn(B),
    });
    await tick();
    shell.openOrNavigate = () => Promise.reject(new Error("no window"));
    await store.go("back");
    await tick();
    expect(store.history.state.cursor).toBe(1);
    expect(placeOf(store)).toEqual(web(B));
    expect(incoherent).toEqual([]);
  });

  it("13. a page moving on its own in an unselected tab is no step until the tab is selected", async () => {
    const { store, shell, incoherent } = setup({
      tabs: [files, browserTab(A)],
      selected: "F",
      ...liveOn(A),
    });
    await tick();
    shell.changeUrl(W_VIEW, B);
    expect(timelineOf(store.history.state.visits)).toEqual(["F:files"]);
    store.select("W");
    expect(timelineOf(store.history.state.visits)).toEqual(["F:files", "W:https://x.test/b"]);
    expect(currentVisit(store.history.state)).toEqual({ tabId: "W", place: web(B) });
    expect(incoherent).toEqual([]);
  });

  it("14. Back replays the URL, and Back/Forward into the tab wait for its URL event", async () => {
    vi.useFakeTimers();
    try {
      const { store, shell, incoherent } = setup({
        tabs: [browserTab(B), files],
        timeline: [visitAt(A), visitAt(B)],
        ...liveOn(B),
      });
      await vi.advanceTimersByTimeAsync(0);
      await store.go("back");
      expect(shell.openOrNavigateCalls).toEqual([{ viewKey: W_VIEW, url: A }]);
      expect(await store.go("forward")).toMatchObject({ ok: false });
      shell.changeUrl(W_VIEW, A);
      expect(await store.go("forward")).toEqual({ ok: true });
      expect(shell.openOrNavigateCalls.at(-1)).toEqual({ viewKey: W_VIEW, url: B });
      expect(incoherent).toEqual([]);
    } finally {
      vi.useRealTimers();
    }
  });
});
