import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { publishShownReview, resetShownReviewsForTests } from "@/lib/reviewFileLinks";
import {
  sessionChoiceCount,
  workingCopyComparisonsAt,
  workspaceKeyOf,
} from "@/lib/workingCopyComparisons";

import type { WorkspaceTab } from "./workspaceTab";
import { saveWorkspaceTabsSession } from "./workspaceTabsPersistence";
import {
  existingWorkspaceTabsSession,
  openWorkspaceFile,
  openWorkspaceFileLink,
  openWorkspaceLink,
  resetWorkspaceTabsSessionsForTests,
  showsOnlyEmptyNewTab,
  workspaceTabsSession,
} from "./workspaceTabsSessions";

vi.mock("sonner", () => ({ toast: { error: vi.fn(), warning: vi.fn() } }));

const FILES_TAB: WorkspaceTab = {
  id: "files",
  place: { kind: "files" },
  role: "files",
  openedBy: "restore",
};
const A_TAB: WorkspaceTab = {
  id: "a",
  place: { kind: "files", path: "a.md" },
  openedBy: "restore",
};

/** Saved tabs for session s1, as an earlier run left them; `selected` defaults to the last. */
function seedTabs(tabs: WorkspaceTab[], selected: WorkspaceTab = tabs.at(-1)!) {
  saveWorkspaceTabsSession("s1", {
    tabs,
    selectedTabId: selected.id,
    timeline: [{ tabId: selected.id, place: selected.place }],
    cursor: 0,
    closed: [],
  });
}

function fileTab(path: string): WorkspaceTab {
  const tab = workspaceTabsSession("s1")
    .store.tabs()
    .find(
      (candidate): candidate is WorkspaceTab =>
        !("unknown" in candidate) &&
        candidate.place.kind === "files" &&
        candidate.place.path === path,
    );
  if (!tab) throw new Error(`no tab shows ${path}`);
  return tab;
}

beforeEach(() => {
  seedTabs([FILES_TAB, A_TAB]);
});

afterEach(() => {
  resetWorkspaceTabsSessionsForTests();
  window.localStorage.clear();
});

describe("openWorkspaceFile", () => {
  it("opens a diff link in the Changes review at its lines, leaving a dirty editor be", async () => {
    const session = workspaceTabsSession("s1");
    const tab = fileTab("a.md");
    const guard = vi.fn().mockResolvedValue("cancel");
    session.registerLeaveGuard(tab.id, guard);

    const result = await openWorkspaceFile("s1", "a.md", { view: "diff", line: 4 }, "link");

    expect(result.ok).toBe(true);
    const review = result.ok ? session.store.findTab(result.tabId) : undefined;
    expect(review?.place).toEqual({ kind: "changes", path: "a.md", line: { start: 4 } });
    expect(session.fileRequest(result.ok ? result.tabId : "")?.position).toEqual({ line: 4 });
    expect(guard).not.toHaveBeenCalled();
    expect(session.store.findTab(tab.id)?.place).toEqual({ kind: "files", path: "a.md" });
  });

  it("shows a file in the selected Files tab while it has no file open", async () => {
    const session = workspaceTabsSession("s1");
    const opened = await session.store.open({ place: { kind: "files" } }, { opener: "user" });
    const filesTabId = opened.ok ? opened.tabId : "";
    const tabCount = session.store.tabs().length;

    const result = await openWorkspaceFile("s1", "b.md", undefined, "user");

    expect(result).toMatchObject({ ok: true, tabId: filesTabId });
    expect(session.store.findTab(filesTabId)?.place).toEqual({ kind: "files", path: "b.md" });
    expect(session.store.tabs()).toHaveLength(tabCount);
  });

  it("opens a new tab when the selected Files tab already shows a file", async () => {
    const session = workspaceTabsSession("s1");
    const opened = await session.store.open({ place: { kind: "files" } }, { opener: "user" });
    const filesTabId = opened.ok ? opened.tabId : "";
    await openWorkspaceFile("s1", "b.md", undefined, "user");

    const result = await openWorkspaceFile("s1", "c.md", undefined, "user");

    expect(result.ok && result.tabId).not.toBe(filesTabId);
    expect(session.store.findTab(filesTabId)?.place).toEqual({ kind: "files", path: "b.md" });
  });

  it("reuses the tab already showing a file instead of opening another", async () => {
    const tab = fileTab("a.md");
    const result = await openWorkspaceFile("s1", "a.md", { line: 4 }, "user");
    expect(result).toMatchObject({ ok: true, tabId: tab.id, created: false });
  });

  it("opens a comment's thread on the file itself, even when the link also asks for its diff", async () => {
    const session = workspaceTabsSession("s1");
    const tab = fileTab("a.md");

    const result = await openWorkspaceFile("s1", "a.md", { view: "diff", commentId: "c1" }, "link");

    expect(result).toMatchObject({ ok: true, tabId: tab.id });
    expect(session.store.findTab(tab.id)?.place).toEqual({
      kind: "files",
      path: "a.md",
      comment: "c1",
    });
  });
});

describe("workspaceTabsSession", () => {
  it("starts from the saved tabs on first use", () => {
    const { store } = workspaceTabsSession("s1");
    expect(store.selectedTab()?.place).toEqual({ kind: "files", path: "a.md" });
  });

  it("restores the saved tabs on the next run", () => {
    const first = workspaceTabsSession("s1");
    first.store.select(first.store.tabs()[0].id);
    const saved = first.store.serialize();
    resetWorkspaceTabsSessionsForTests();
    expect(workspaceTabsSession("s1").store.serialize()).toEqual(saved);
  });
});

describe("existingWorkspaceTabsSession", () => {
  it("never creates tabs for a session the sidebar hasn't shown", () => {
    window.localStorage.clear();
    expect(existingWorkspaceTabsSession("s1")).toBeUndefined();
    expect(window.localStorage.getItem("omnigent:workspace-tabs")).toBeNull();
    workspaceTabsSession("s1");
    expect(existingWorkspaceTabsSession("s1")).toBeDefined();
  });
});

describe("close other tabs with a browser view among them", () => {
  /** Tabs [Files, A, W] with A selected, as in the reviewer's probe. */
  async function setUp() {
    seedTabs(
      [
        FILES_TAB,
        A_TAB,
        {
          id: "w",
          place: { kind: "web" },
          binding: { kind: "browser-view", viewKey: "w" },
          openedBy: "restore",
        },
      ],
      A_TAB,
    );
    const session = workspaceTabsSession("s1");
    const { store } = session;
    const keep = store.tabs().find((tab) => "role" in tab && tab.role === "files")!;
    const web = store
      .tabs()
      .find((tab) => "binding" in tab && tab.binding?.kind === "browser-view")!;
    const a = fileTab("a.md");
    for (const tab of store.tabs()) {
      // oxlint-disable-next-line no-await-in-loop -- closing fixture tabs one by one
      if (![keep.id, web.id, a.id].includes(tab.id)) await store.close(tab.id);
    }
    store.select(a.id);
    expect(store.tabs().map((tab) => tab.id)).toEqual([keep.id, a.id, web.id]);
    return { session, keep, web, a };
  }

  it("publishes A gone at once, with one notification, while W's view is still releasing", async () => {
    const { session, keep, a } = await setUp();
    const notified = vi.fn();
    session.store.subscribe(notified);
    let releasing = false;
    const release = () =>
      new Promise<boolean>(() => {
        releasing = true;
      });

    await expect(
      session.store.closeOthers(keep.id, (tab) =>
        tab.binding?.kind === "browser-view" ? release : undefined,
      ),
    ).resolves.toEqual({ ok: true });

    expect(releasing).toBe(true);
    expect(notified).toHaveBeenCalledTimes(1);
    expect(session.store.tabs().map((tab) => tab.id)).toEqual([keep.id]);
    expect(session.store.selectedTabId()).toBe(keep.id);
    expect(session.store.findTab(a.id)).toBeUndefined();
  });

  it("asks a dirty A once; Keep editing closes nothing", async () => {
    const { session, keep, web, a } = await setUp();
    const guard = vi.fn().mockResolvedValue("cancel");
    session.registerLeaveGuard(a.id, guard);
    const release = vi.fn();
    await expect(session.store.closeOthers(keep.id, () => release)).resolves.toEqual({
      ok: false,
      cancelled: true,
    });
    expect(guard).toHaveBeenCalledTimes(1);
    expect(release).not.toHaveBeenCalled();
    expect(session.store.tabs().map((tab) => tab.id)).toEqual([keep.id, a.id, web.id]);
  });

  it("closes a dirty A once its edits are discarded", async () => {
    const { session, keep, a } = await setUp();
    const unregister = session.registerLeaveGuard(a.id, async () => {
      unregister();
      return "leave";
    });
    await expect(session.store.closeOthers(keep.id, () => undefined)).resolves.toEqual({
      ok: true,
    });
    expect(session.store.findTab(a.id)).toBeUndefined();
  });

  it("bumps the revision when a tab gains or loses unsaved edits", () => {
    const session = workspaceTabsSession("s1");
    const a = fileTab("a.md");
    const before = session.store.revision();
    const unregister = session.registerLeaveGuard(a.id, async () => "leave");
    expect(session.store.revision()).toBe(before + 1);
    unregister();
    expect(session.store.revision()).toBe(before + 2);
  });
});

describe("showing the tab sidebar", () => {
  it("starts a session with no saved tabs on one empty New tab", () => {
    window.localStorage.clear();
    const { store } = workspaceTabsSession("fresh");
    expect(store.tabs()).toHaveLength(1);
    expect(store.selectedTab()).toMatchObject({ place: { kind: "new" } });
    expect(showsOnlyEmptyNewTab("fresh")).toBe(true);
  });

  it("isn't showing only a New tab once a session has tabs", () => {
    expect(showsOnlyEmptyNewTab("s1")).toBe(false);
  });

  it("is back on one empty New tab after the last tab closes", async () => {
    const { store } = workspaceTabsSession("s1");
    for (const tab of [...store.tabs()]) {
      // oxlint-disable-next-line no-await-in-loop -- one close at a time, as a user does
      await store.close(tab.id);
    }
    expect(store.tabs()).toHaveLength(1);
    expect(showsOnlyEmptyNewTab("s1")).toBe(true);
  });
});

describe("openWorkspaceLink", () => {
  it("gives the review a link lands on the link's lines, and no earlier citation without them", async () => {
    const session = workspaceTabsSession("s1");
    const cited = {
      place: { kind: "changes", path: "a.ts", line: { start: 150, end: 151 } },
    } as const;
    const review = await openWorkspaceLink("s1", cited, { opener: "user" });
    if (!review.ok) throw new Error("the review didn't open");
    expect(session.fileRequest(review.tabId)?.position).toEqual({ line: 150, endLine: 151 });

    // The reader moves to the file, then asks for its changes with nothing cited.
    await session.store.open({ place: { kind: "files", path: "a.ts" } }, { opener: "user" });
    const reused = await openWorkspaceLink(
      "s1",
      { place: { kind: "changes", path: "a.ts" } },
      { opener: "user" },
    );
    expect(reused).toMatchObject({ ok: true, tabId: review.tabId });
    expect(session.fileRequest(review.tabId)?.position).toBeUndefined();
  });

  it("leaves a request alone when the open is refused", async () => {
    const session = workspaceTabsSession("s1");
    const review = await openWorkspaceLink(
      "s1",
      { place: { kind: "changes", path: "a.ts", line: { start: 4 } } },
      { opener: "user" },
    );
    if (!review.ok) throw new Error("the review didn't open");
    vi.spyOn(session.store, "open").mockResolvedValueOnce({ ok: false, stale: true });
    await openWorkspaceLink("s1", { place: { kind: "changes", path: "a.ts" } }, { opener: "user" });
    expect(session.fileRequest(review.tabId)?.position).toEqual({ line: 4 });
  });
});

describe("openWorkspaceFileLink", () => {
  async function selectChanges() {
    const session = workspaceTabsSession("s1");
    await session.store.open({ place: { kind: "changes" } }, { opener: "user" });
    return session.store.selectedTab()!;
  }
  afterEach(() => resetShownReviewsForTests());

  function expectFilesAt(path: string, line: number) {
    expect(workspaceTabsSession("s1").store.selectedTab()).toMatchObject({
      place: { kind: "files", path, line: { start: line } },
    });
  }

  it("keeps a file the review on screen shows in it, at the cited line, each time", async () => {
    const session = workspaceTabsSession("s1");
    const changes = await selectChanges();
    const comparison = { kind: "saved", intent: "branch-point" } as const;
    await session.store.navigate(changes.id, { kind: "changes", comparison });
    publishShownReview("s1", {}, { state: "listed", paths: ["src/a.py"] });

    await openWorkspaceFileLink("s1", "src/a.py", { line: 100 }, "user");
    expect(session.store.selectedTab()).toMatchObject({
      id: changes.id,
      place: { kind: "changes", path: "src/a.py", line: { start: 100 }, comparison },
    });
    const first = session.fileRequest(changes.id)?.position;
    expect(first).toEqual({ line: 100 });

    // The same line cited again is a new request, so the review brings it back.
    await openWorkspaceFileLink("s1", "src/a.py", { line: 100 }, "user");
    expect(session.fileRequest(changes.id)?.position).toEqual({ line: 100 });
    expect(session.fileRequest(changes.id)?.position).not.toBe(first);
  });

  it("opens a comment's thread on the file, not in the review that shows the file", async () => {
    const session = workspaceTabsSession("s1");
    const changes = await selectChanges();
    publishShownReview("s1", {}, { state: "listed", paths: ["src/a.py"] });

    await openWorkspaceFileLink("s1", "src/a.py", { commentId: "c1" }, "link");

    expect(session.store.selectedTab()).toMatchObject({
      place: { kind: "files", path: "src/a.py", comment: "c1" },
    });
    expect(session.store.findTab(changes.id)?.place).toEqual({ kind: "changes" });
  });

  it("opens a file the review doesn't show in Files, at the line", async () => {
    const session = workspaceTabsSession("s1");
    const changes = await selectChanges();
    publishShownReview("s1", {}, { state: "listed", paths: ["src/a.py"] });

    await openWorkspaceFileLink("s1", "src/other.py", { line: 7 }, "user");
    expectFilesAt("src/other.py", 7);
    expect(session.store.findTab(changes.id)?.place).toEqual({ kind: "changes" });
  });

  it("opens the file in Files when no review is on screen, or it can't compare", async () => {
    await selectChanges();
    await openWorkspaceFileLink("s1", "src/a.py", { line: 100 }, "user");
    expectFilesAt("src/a.py", 100);

    await selectChanges();
    publishShownReview("s1", {}, { state: "unavailable" });
    await openWorkspaceFileLink("s1", "src/b.py", { line: 3 }, "user");
    expectFilesAt("src/b.py", 3);
  });

  it("waits for a pending comparison, then keeps a file it includes at the line", async () => {
    const session = workspaceTabsSession("s1");
    const changes = await selectChanges();
    const owner = {};
    publishShownReview("s1", owner, { state: "pending" });

    const opening = openWorkspaceFileLink("s1", "src/a.py", { line: 12 }, "user");
    // Unknown membership isn't membership: nothing moves while the comparison settles.
    expect(session.store.findTab(changes.id)?.place).toEqual({ kind: "changes" });
    publishShownReview("s1", owner, { state: "listed", paths: ["src/a.py"] });
    await opening;
    expect(session.store.selectedTab()).toMatchObject({
      id: changes.id,
      place: { kind: "changes", path: "src/a.py", line: { start: 12 } },
    });
  });

  it("waits for a pending comparison, then opens a file it leaves out in Files at the line", async () => {
    const session = workspaceTabsSession("s1");
    const changes = await selectChanges();
    const owner = {};
    publishShownReview("s1", owner, { state: "pending" });

    const opening = openWorkspaceFileLink("s1", "src/a.py", { line: 12 }, "user");
    publishShownReview("s1", owner, { state: "listed", paths: ["src/other.py"] });
    await opening;
    expectFilesAt("src/a.py", 12);
    expect(session.store.findTab(changes.id)?.place).toEqual({ kind: "changes" });
  });

  it("follows the latest link clicked while the comparison settles", async () => {
    const session = workspaceTabsSession("s1");
    const changes = await selectChanges();
    const owner = {};
    publishShownReview("s1", owner, { state: "pending" });

    const older = openWorkspaceFileLink("s1", "src/a.py", { line: 12 }, "user");
    const newer = openWorkspaceFileLink("s1", "src/a.py", { line: 40 }, "user");
    publishShownReview("s1", owner, { state: "listed", paths: ["src/a.py"] });
    await expect(older).resolves.toEqual({ ok: false, stale: true });
    await expect(newer).resolves.toMatchObject({ ok: true });
    expect(session.store.selectedTab()).toMatchObject({
      id: changes.id,
      place: { kind: "changes", path: "src/a.py", line: { start: 40 } },
    });
  });

  it("drops the link when the person moves on while the comparison settles", async () => {
    const session = workspaceTabsSession("s1");
    await selectChanges();
    const owner = {};
    publishShownReview("s1", owner, { state: "pending" });

    const opening = openWorkspaceFileLink("s1", "src/a.py", { line: 12 }, "user");
    await session.store.open({ place: { kind: "search" } }, { opener: "user" });
    publishShownReview("s1", owner, { state: "listed", paths: ["src/a.py"] });
    await expect(opening).resolves.toEqual({ ok: false, stale: true });
    expect(session.store.selectedTab()?.place).toEqual({ kind: "search" });
  });
});

describe("a file's cited lines", () => {
  async function openCited(path: string, line: number) {
    const result = await openWorkspaceFile("s1", path, { line }, "user");
    if (!result.ok) throw new Error(`${path} didn't open`);
    return result.tabId;
  }

  it("stop applying once the tab navigates to another file", async () => {
    const session = workspaceTabsSession("s1");
    const tabId = await openCited("b.md", 50);
    expect(session.fileRequest(tabId)?.position).toEqual({ line: 50 });

    await session.store.navigate(tabId, { kind: "files", path: "c.md", line: { start: 3 } });

    expect(session.fileRequest(tabId)).toBeUndefined();
    expect(session.store.findTab(tabId)?.place).toEqual({
      kind: "files",
      path: "c.md",
      line: { start: 3 },
    });
  });

  it("give Back and Forward the visit's own lines, not a newer citation", async () => {
    const session = workspaceTabsSession("s1");
    const tabId = await openCited("b.md", 50);
    await session.store.navigate(tabId, { kind: "files", path: "c.md" });
    await openCited("c.md", 80);
    expect(session.fileRequest(tabId)?.position).toEqual({ line: 80 });

    await session.store.go("back");

    expect(session.store.findTab(tabId)?.place).toEqual({
      kind: "files",
      path: "b.md",
      line: { start: 50 },
    });
    expect(session.fileRequest(tabId)).toBeUndefined();
    await session.store.go("forward");
    expect(session.store.findTab(tabId)?.place).toMatchObject({ kind: "files", path: "c.md" });
    expect(session.fileRequest(tabId)).toBeUndefined();
  });

  it("are forgotten when the tab closes", async () => {
    const session = workspaceTabsSession("s1");
    const tabId = await openCited("b.md", 50);
    await session.store.close(tabId);
    expect(session.fileRequest(tabId)).toBeUndefined();
  });

  it("still apply while the tab stays on the cited place", async () => {
    const session = workspaceTabsSession("s1");
    const tabId = await openCited("b.md", 50);
    session.store.select(fileTab("a.md").id);
    session.store.select(tabId);
    expect(session.fileRequest(tabId)?.position).toEqual({ line: 50 });
  });
});

describe("a tab's landing", () => {
  it("counts a comparison chosen in a folder's review as the session's choice", async () => {
    const { store } = workspaceTabsSession("s1");
    workingCopyComparisonsAt(".worktrees/feature", workspaceKeyOf("host_1", "/repo")).chose("s1");
    await store.open({ place: { kind: "pulls" } }, { opener: "user" });
    expect(store.landing(store.selectedTabId()!)?.choices).toBe(sessionChoiceCount("s1"));
    expect(sessionChoiceCount("s1")).toBeGreaterThan(0);
  });
});
