import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  beginMuteSave,
  canFlagUnread,
  clearSessionMuteState,
  isConversationMuted,
  MUTE_REINDEX_GRACE_MS,
  muteReadMarker,
  MUTED_LABEL_KEY,
  reconcileMuteLabel,
  settleMuteSave,
  showsUnreadFor,
} from "./sessionMute";

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
