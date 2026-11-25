import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  beginMuteSave,
  canFlagUnread,
  clearSessionMuteState,
  isConversationMuted,
  isInMutedProject,
  isSessionMuted,
  MUTE_REINDEX_GRACE_MS,
  muteReadMarker,
  MUTED_LABEL_KEY,
  onMuteRecheck,
  reconcileMuteLabel,
  setMutedProjects,
  settleMuteSave,
  showsUnreadFor,
  useMutedProjects,
} from "./sessionMute";
import { act, renderHook } from "@testing-library/react";
import { PROJECT_LABEL_KEY } from "./sessionListCache";

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  clearSessionMuteState();
  vi.useRealTimers();
});

const row = (muted: boolean, extra: Record<string, string> = {}) => ({
  id: "session",
  labels: { ...extra, ...(muted ? { [MUTED_LABEL_KEY]: "1" } : {}) } as Record<string, string>,
});

describe("session mute store", () => {
  it("reads the label until this tab saves, then keeps the saved preference over stale rows", () => {
    expect(isConversationMuted(row(true))).toBe(true);
    expect(isConversationMuted(row(false))).toBe(false);
    beginMuteSave("session", true);
    settleMuteSave("session", true);
    // A rollback elsewhere restoring an older row can't unmute it.
    expect(isConversationMuted(row(false))).toBe(true);
    expect(isConversationMuted({ id: "other", labels: {} })).toBe(false);
  });

  it.each(
    [false, true].flatMap((muted) =>
      (["before", "during", "in grace", "after grace"] as const).flatMap((read) =>
        [false, true].map((success) => ({ muted, read, success })),
      ),
    ),
  )(
    "reconciles a read started $read for a save of mute=$muted with success=$success",
    ({ muted, read, success }) => {
      const saved = success ? muted : !muted;
      let readMarker = read === "before" ? muteReadMarker() : -1;
      beginMuteSave("session", muted);
      if (read === "during") readMarker = muteReadMarker();
      settleMuteSave("session", saved);
      vi.advanceTimersByTime(
        read === "after grace" ? MUTE_REINDEX_GRACE_MS : MUTE_REINDEX_GRACE_MS - 1,
      );
      if (read === "in grace" || read === "after grace") readMarker = muteReadMarker();
      // Earlier reads, or a lagging index, report the pre-save state; a read after
      // the grace window reports what the server holds, here the opposite of the save.
      const fetched = row(read === "after grace" ? !saved : !muted, { other: "fresh" });
      const reconciled = reconcileMuteLabel(fetched, readMarker);
      const expected = read === "after grace" ? !saved : saved;
      expect(!!reconciled.labels[MUTED_LABEL_KEY]).toBe(expected);
      expect(reconciled.labels.other).toBe("fresh");
      expect(isConversationMuted(row(!expected))).toBe(expected);
    },
  );

  it("keeps the local value for any read while the save is in flight", () => {
    beginMuteSave("session", true);
    expect(reconcileMuteLabel(row(false), muteReadMarker()).labels[MUTED_LABEL_KEY]).toBe("1");
    expect(isConversationMuted(row(false))).toBe(true);
  });

  it("rechecks a session when its window ends, only if data disagreed after the save", () => {
    const rechecked: string[] = [];
    const unsubscribe = onMuteRecheck((id) => rechecked.push(id));
    beginMuteSave("session", true);
    // A read started during the save predates it, whether it lands before or after.
    reconcileMuteLabel(row(false), muteReadMarker());
    const duringSave = muteReadMarker();
    vi.advanceTimersByTime(1);
    settleMuteSave("session", true);
    expect(reconcileMuteLabel(row(false), duringSave).labels[MUTED_LABEL_KEY]).toBe("1");
    reconcileMuteLabel(row(true), muteReadMarker());
    vi.advanceTimersByTime(MUTE_REINDEX_GRACE_MS);
    expect(rechecked).toEqual([]);

    beginMuteSave("session", false);
    settleMuteSave("session", false);
    reconcileMuteLabel(row(true), muteReadMarker());
    reconcileMuteLabel(row(true), muteReadMarker());
    vi.advanceTimersByTime(MUTE_REINDEX_GRACE_MS - 1);
    expect(rechecked).toEqual([]);
    vi.advanceTimersByTime(1);
    expect(rechecked).toEqual(["session"]);

    unsubscribe();
    beginMuteSave("session", true);
    settleMuteSave("session", true);
    reconcileMuteLabel(row(false), muteReadMarker());
    vi.advanceTimersByTime(MUTE_REINDEX_GRACE_MS);
    expect(rechecked).toEqual(["session"]);
  });

  it("drops a pending recheck when a newer save starts", () => {
    const rechecked: string[] = [];
    const unsubscribe = onMuteRecheck((id) => rechecked.push(id));
    beginMuteSave("session", true);
    settleMuteSave("session", true);
    reconcileMuteLabel(row(false), muteReadMarker());
    beginMuteSave("session", false);
    vi.advanceTimersByTime(MUTE_REINDEX_GRACE_MS);
    expect(rechecked).toEqual([]);
    unsubscribe();
  });

  it("refuses an overlapping save for the same session but not another", () => {
    expect(beginMuteSave("session", true)).toBe(true);
    expect(beginMuteSave("session", false)).toBe(false);
    expect(isConversationMuted(row(false))).toBe(true);
    expect(beginMuteSave("other", true)).toBe(true);
    settleMuteSave("session", true);
    expect(beginMuteSave("session", false)).toBe(true);
  });

  it("leaves a row without labels, or one already agreeing, untouched", () => {
    beginMuteSave("session", true);
    const partial = { id: "session" };
    expect(reconcileMuteLabel(partial, muteReadMarker())).toBe(partial);
    const agreeing = row(true);
    expect(reconcileMuteLabel(agreeing, muteReadMarker())).toBe(agreeing);
  });

  it("hides unread state and refuses unread flags only for muted sessions", () => {
    expect(showsUnreadFor(row(false), true)).toBe(true);
    expect(showsUnreadFor(row(false), false)).toBe(false);
    expect(showsUnreadFor(row(true), true)).toBe(false);
    expect(canFlagUnread(row(false))).toBe(true);
    expect(canFlagUnread(row(true))).toBe(false);
  });
});

describe("project mute", () => {
  const VIEWER = "alice@example.com";
  const projects = [
    { id: "p-muted", name: "Factory", muted: true },
    { id: "p-loud", name: "Coordination", muted: false },
    // A label-only folder can't store a mute, so it never counts.
    { id: null, name: "Legacy", muted: true },
  ];

  it("mutes the viewer's sessions in a muted project's folder, by id or legacy label", () => {
    setMutedProjects(projects, VIEWER);
    const filed = { id: "a", labels: {}, project_id: "p-muted", owner: VIEWER };
    const labeled = { id: "b", labels: { [PROJECT_LABEL_KEY]: "Factory" }, owner: VIEWER };
    // Single-user rows carry no owner and read as the viewer's own.
    const unowned = { id: "c", labels: {}, project_id: "p-muted" };
    for (const session of [filed, labeled, unowned]) {
      expect(isInMutedProject(session)).toBe(true);
      expect(isConversationMuted(session)).toBe(true);
      expect(isSessionMuted(session)).toBe(false);
      expect(showsUnreadFor(session, true)).toBe(false);
      expect(canFlagUnread(session)).toBe(false);
    }
    // A collaborator's legacy label can name the viewer's project without filing into it.
    const shared = {
      id: "d",
      labels: { [PROJECT_LABEL_KEY]: "Factory" },
      owner: "bob@example.com",
    };
    const elsewhere = { id: "e", labels: {}, project_id: "p-loud", owner: VIEWER };
    const unfiled = { id: "f", labels: {}, owner: VIEWER };
    const legacy = { id: "g", labels: { [PROJECT_LABEL_KEY]: "Legacy" }, owner: VIEWER };
    for (const session of [shared, elsewhere, unfiled, legacy]) {
      expect(isConversationMuted(session)).toBe(false);
    }
  });

  it("re-renders subscribers only when the muted set or viewer changes", () => {
    const { result } = renderHook(() => useMutedProjects());
    expect(result.current).toEqual([]);
    act(() => setMutedProjects(projects, VIEWER));
    const first = result.current;
    expect(first).toEqual([{ id: "p-muted", name: "Factory" }]);
    act(() =>
      setMutedProjects(
        projects.map((project) => ({ ...project })),
        VIEWER,
      ),
    );
    expect(result.current).toBe(first);
    // Identity resolving later changes whose sessions count, so subscribers recheck.
    act(() => setMutedProjects(projects, "bob@example.com"));
    expect(result.current).not.toBe(first);
    act(() =>
      setMutedProjects(
        projects.map((project) => ({ ...project, muted: false })),
        VIEWER,
      ),
    );
    expect(result.current).toEqual([]);
    expect(isConversationMuted({ id: "a", labels: {}, project_id: "p-muted" })).toBe(false);
  });
});
