import { afterEach, describe, expect, it } from "vitest";
import {
  type CommentCheckTarget,
  type CommentEditEntry,
  EMPTY_ENTRY,
  changeConflicted,
  changeFoundDeleted,
  changeMade,
  changeRefused,
  changeSent,
  changeUncertain,
  checkFailed,
  checkStarted,
  commentChangeHeld,
  commentEditKey,
  discardLocalText,
  editDirty,
  editText,
  keepMine,
  keptText,
  needsRecovery,
  saveBlocked,
  loadLatest,
  readCommentEdit,
  resetCommentEditsForTests,
  settleChange,
  startEditing,
  stopEditing,
  updateCommentEdit,
} from "./commentEditStore";

const CHECK: CommentCheckTarget = {
  prUrl: "https://github.com/acme/app/pull/42",
  repository: "acme/app",
  number: 42,
  commentId: "IC_1",
  kind: "issue",
  login: "mac-octo",
};

const editingFrom = (body: string, text = body): CommentEditEntry =>
  editText(startEditing(EMPTY_ENTRY, body), text);

/** An edit of "Before" to "After", sent and not confirmed. */
function unconfirmedEdit(): CommentEditEntry {
  const sent = changeSent(editingFrom("Before", "After"), "edit", "r1");
  return checkStarted(
    changeUncertain(sent, "r1", { sent: "After", expected: "Before", check: CHECK }),
    7,
  );
}

afterEach(resetCommentEditsForTests);

describe("editing a comment", () => {
  it("opens on the comment's text, and is dirty only once the text differs", () => {
    const opened = startEditing(EMPTY_ENTRY, "Before");
    expect(opened.editing).toEqual({ text: "Before", expected: "Before" });
    expect(editDirty(opened)).toBe(false);
    expect(editDirty(editText(opened, "After"))).toBe(true);
  });

  it("keeps a draft already open rather than starting over", () => {
    const draft = editingFrom("Before", "Half written");
    expect(startEditing(draft, "Before")).toBe(draft);
  });

  it("changes nothing while a change is out or unconfirmed", () => {
    const sent = changeSent(editingFrom("Before", "After"), "edit", "r1");
    expect(editText(sent, "Other")).toBe(sent);
    expect(stopEditing(sent)).toBe(sent);
    expect(startEditing(sent, "x")).toBe(sent);
    const unknown = unconfirmedEdit();
    expect(editText(unknown, "Other")).toBe(unknown);
    expect(commentChangeHeld(unknown)).toBe(true);
  });

  it("Keep mine saves over GitHub's text next time; Load latest starts again from it", () => {
    const sent = changeSent(editingFrom("Before", "Mine"), "edit", "r1");
    const conflicted = changeConflicted(sent, "r1", "Theirs");
    expect(conflicted.conflict).toEqual({ current: "Theirs" });
    expect(conflicted.pending).toBeNull();
    expect(keepMine(conflicted).editing).toEqual({ text: "Mine", expected: "Theirs" });
    expect(loadLatest(conflicted).editing).toEqual({ text: "Theirs", expected: "Theirs" });
  });

  it("settles only the change it was sent as", () => {
    const sent = changeSent(editingFrom("Before", "After"), "edit", "r1");
    expect(changeMade(sent, "r2")).toBe(sent);
    expect(changeRefused(sent, "r2", "no")).toBe(sent);
    expect(changeMade(sent, "r1")).toEqual(EMPTY_ENTRY);
    expect(changeRefused(sent, "r1", "No.")).toMatchObject({ pending: null, message: "No." });
    expect(changeRefused(sent, "r1", "No.").editing?.text).toBe("After");
  });

  it("keeps changed text when the comment turns out deleted, and only then", () => {
    const dirty = changeSent(editingFrom("Before", "After"), "edit", "r1");
    expect(changeFoundDeleted(dirty, "r1", "Gone.").tombstone).toEqual({ text: "After" });
    const deleting = changeSent(EMPTY_ENTRY, "delete", "r2");
    const gone = changeFoundDeleted(deleting, "r2", "This comment was deleted on GitHub.");
    expect(gone.tombstone).toBeNull();
    expect(gone.message).toBe("This comment was deleted on GitHub.");
    expect(discardLocalText(changeFoundDeleted(dirty, "r1", "Gone."))).toEqual(EMPTY_ENTRY);
  });
});

describe("a change GitHub didn't confirm", () => {
  it("is held with what was sent, never left waiting", () => {
    const sent = changeSent(editingFrom("Before", "After"), "edit", "r1");
    const held = changeUncertain(sent, "r1", { sent: "After", expected: "Before", check: CHECK });
    expect(held.pending).toBeNull();
    expect(held.unknown).toMatchObject({ change: "edit", sent: "After", expected: "Before" });
    expect(held.editing?.text).toBe("After");
  });

  it("was made when the comment now has the text sent", () => {
    expect(settleChange(unconfirmedEdit(), 7, { state: "present", body: "After" })).toEqual({
      settled: "edited",
    });
  });

  it("wasn't made when the comment still has its old text; the editor keeps the edit", () => {
    const outcome = settleChange(unconfirmedEdit(), 7, { state: "present", body: "Before" });
    expect(outcome?.settled).toBe("not-made");
    if (outcome?.settled !== "not-made") return;
    expect(outcome.entry.unknown).toBeNull();
    expect(outcome.entry.editing?.text).toBe("After");
    expect(commentChangeHeld(outcome.entry)).toBe(false);
  });

  it("offers Keep mine or Load latest when someone else changed the comment", () => {
    const outcome = settleChange(unconfirmedEdit(), 7, { state: "present", body: "Theirs" });
    expect(outcome?.settled).toBe("changed-elsewhere");
    if (outcome?.settled !== "changed-elsewhere") return;
    expect(outcome.entry.conflict).toEqual({ current: "Theirs" });
  });

  it("keeps the edit's text once the comment is found deleted", () => {
    const outcome = settleChange(unconfirmedEdit(), 7, { state: "absent" });
    expect(outcome?.settled).toBe("gone");
    if (outcome?.settled !== "gone") return;
    expect(outcome.entry.tombstone).toEqual({ text: "After" });
  });

  it("settles a delete as made only when the comment is gone", () => {
    const deleting = checkStarted(
      changeUncertain(changeSent(EMPTY_ENTRY, "delete", "r1"), "r1", { check: CHECK }),
      3,
    );
    expect(settleChange(deleting, 3, { state: "absent" })).toEqual({ settled: "deleted" });
    expect(settleChange(deleting, 3, { state: "present", body: "Still here" })?.settled).toBe(
      "not-made",
    );
  });

  it("listens only to its latest check", () => {
    const held = unconfirmedEdit();
    expect(settleChange(held, 6, { state: "present", body: "After" })).toBeNull();
    expect(checkFailed(held, 6)).toBe(held);
    const failed = checkFailed(held, 7);
    expect(failed).toMatchObject({ checking: null, checkFailed: true });
    expect(commentChangeHeld(failed)).toBe(true);
  });
});

describe("the store", () => {
  it("shares one entry per workspace user, PR and comment", () => {
    const mine = commentEditKey("user-1", CHECK.prUrl, "IC_1");
    updateCommentEdit(mine, (entry) => startEditing(entry, "Before"));
    expect(readCommentEdit(mine).editing?.text).toBe("Before");
    expect(readCommentEdit(commentEditKey("user-2", CHECK.prUrl, "IC_1"))).toBe(EMPTY_ENTRY);
    expect(readCommentEdit(commentEditKey("user-1", CHECK.prUrl, "IC_2"))).toBe(EMPTY_ENTRY);
  });

  it("forgets an entry with nothing left in it", () => {
    const key = commentEditKey("user-1", CHECK.prUrl, "IC_1");
    updateCommentEdit(key, (entry) => startEditing(entry, "Before"));
    updateCommentEdit(key, stopEditing);
    expect(readCommentEdit(key)).toBe(EMPTY_ENTRY);
  });
});

describe("dropping text kept for the person", () => {
  it("keeps an unconfirmed change held, without its text", () => {
    const sent = changeSent(editingFrom("Before", "After"), "edit", "r1");
    const held = changeUncertain(sent, "r1", { sent: "After", expected: "Before", check: CHECK });
    const dropped = discardLocalText(held);
    expect(dropped.editing).toBeNull();
    expect(commentChangeHeld(dropped)).toBe(true);
    expect(keptText(dropped)).toBeNull();
    // Found gone later, there's no text left to keep.
    const gone = settleChange(checkStarted(dropped, 9), 9, { state: "absent" });
    expect(gone?.settled === "gone" && gone.entry.tombstone).toBeNull();
    // Found saved, it still settles as made.
    expect(settleChange(checkStarted(dropped, 9), 9, { state: "present", body: "After" })).toEqual({
      settled: "edited",
    });
  });

  it("never drops a change on its way", () => {
    const sent = changeSent(editingFrom("Before", "After"), "edit", "r1");
    expect(discardLocalText(sent)).toBe(sent);
  });

  it("names which entries need a way back when their comment isn't shown", () => {
    expect(needsRecovery(editingFrom("Before"))).toBeNull();
    expect(needsRecovery(editingFrom("Before", "After"))).toBe("draft");
    expect(needsRecovery(changeSent(EMPTY_ENTRY, "delete", "r"))).toBe("pending");
    expect(needsRecovery(unconfirmedEdit())).toBe("unknown");
  });
});

describe("whether an edit can be saved", () => {
  it("can once the text is written and nothing is held", () => {
    expect(saveBlocked(editingFrom("Before", "After"))).toBeNull();
  });

  it("can't while GitHub's text changed and no choice was made", () => {
    const sent = changeSent(editingFrom("Before", "Mine"), "edit", "r1");
    const conflicted = changeConflicted(sent, "r1", "Theirs");
    expect(saveBlocked(conflicted)).toBe("Choose Keep Mine or Load Latest first");
    expect(saveBlocked(keepMine(conflicted))).toBeNull();
  });

  it("can't while a change is out or unconfirmed, nor with nothing written", () => {
    expect(saveBlocked(changeSent(editingFrom("Before", "After"), "edit", "r1"))).toBe(
      "Wait for the edit to save",
    );
    expect(saveBlocked(unconfirmedEdit())).toBe("Check this comment before changing it again");
    expect(saveBlocked(editingFrom("Before", "  "))).toBe("Write the comment first");
    expect(saveBlocked(EMPTY_ENTRY)).not.toBeNull();
  });
});
