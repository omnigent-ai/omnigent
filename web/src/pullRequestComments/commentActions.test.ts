import { describe, expect, it, vi } from "vitest";
import {
  type CommentActionTarget,
  type CommentEditActions,
  commentActionRows,
  commentActionRowsKey,
  commentReactionActions,
  commentPermalink,
} from "./commentActions";

const PR_URL = "https://github.com/acme/app/pull/7";
const COMMENT_URL = `${PR_URL}#issuecomment-1`;

function target(over: Partial<CommentActionTarget> = {}): CommentActionTarget {
  return {
    permalink: COMMENT_URL,
    quotableBody: "Looks good",
    quoteDestination: { quote: vi.fn() },
    copyLink: vi.fn(),
    openLink: vi.fn(),
    ...over,
  };
}

const labels = (from: CommentActionTarget) =>
  commentActionRows(from).map((row) => row.definition.menuLabel);

describe("a comment's actions", () => {
  it("offers Quote Reply, Copy Link and Open in Browser, in that order", () => {
    expect(labels(target())).toEqual(["Quote Reply", "Copy Link", "Open in Browser"]);
  });

  it("names each comment action on its own in ⌘K", () => {
    expect(commentActionRows(target()).map((row) => row.definition.paletteLabel)).toEqual([
      "Quote Reply",
      "Copy Comment Link",
      "Open Comment in Browser",
    ]);
  });

  it("runs each action on this comment", () => {
    const actions = target();
    for (const row of commentActionRows(actions)) row.run();
    expect(actions.quoteDestination!.quote).toHaveBeenCalledWith("Looks good");
    expect(actions.copyLink).toHaveBeenCalledWith(COMMENT_URL);
    expect(actions.openLink).toHaveBeenCalledWith(COMMENT_URL);
  });

  it("leads with Show in Changes for a conversation's comment where the page offers it", () => {
    const showInChanges = vi.fn();
    const rows = commentActionRows(target({ showInChanges }));
    expect(rows.map((row) => row.definition.menuLabel)).toEqual([
      "Show in Changes",
      "Quote Reply",
      "Copy Link",
      "Open in Browser",
    ]);
    expect(rows[0]!.definition).toMatchObject({
      paletteLabel: "Show Conversation in Changes",
      movesFocus: true,
    });
    rows[0]!.run();
    expect(showInChanges).toHaveBeenCalledTimes(1);
    // A comment that's already in Changes, or not in a conversation, offers none.
    expect(labels(target({ showInChanges: null }))).not.toContain("Show in Changes");
  });

  it("offers Add Reaction… after Quote Reply where the viewer may react, opening the picker", () => {
    const openPicker = vi.fn();
    const rows = commentActionRows(target({ reactions: commentReactionActions({ openPicker }) }));
    expect(rows.map((row) => row.definition.menuLabel)).toEqual([
      "Quote Reply",
      "Add Reaction…",
      "Copy Link",
      "Open in Browser",
    ]);
    const add = rows[1]!;
    expect(add.definition).toMatchObject({
      paletteLabel: "Add Reaction to Comment…",
      movesFocus: true,
    });
    add.run();
    expect(openPicker).toHaveBeenCalledTimes(1);
    // Counts only, or no reactions in the read: no row.
    expect(
      labels(target({ reactions: commentReactionActions({ openPicker: null }) })),
    ).not.toContain("Add Reaction…");
    expect(labels(target({ reactions: null }))).not.toContain("Add Reaction…");
  });

  it("offers no link actions for a comment without a link of its own", () => {
    expect(labels(target({ permalink: null }))).toEqual(["Quote Reply"]);
  });

  it("offers no Quote Reply for a hidden or empty comment, or where there's no box", () => {
    expect(labels(target({ quotableBody: null }))).toEqual(["Copy Link", "Open in Browser"]);
    expect(labels(target({ quoteDestination: null }))).toEqual(["Copy Link", "Open in Browser"]);
  });

  it("offers nothing, so no ⋯ shows, when it has neither", () => {
    expect(labels(target({ permalink: null, quoteDestination: null }))).toEqual([]);
  });

  it("shows Quote Reply unavailable, with why, while the box's reply posts", () => {
    const [quote] = commentActionRows(
      target({ quoteDestination: { blockedReason: "Wait for the reply to post", quote: vi.fn() } }),
    );
    expect(quote.disabledReason).toBe("Wait for the reply to post");
  });

  it("describes the rows by what's offered and why any can't run", () => {
    const ready = commentActionRowsKey(commentActionRows(target()));
    const blocked = commentActionRowsKey(
      commentActionRows(target({ quoteDestination: { blockedReason: "Wait", quote: vi.fn() } })),
    );
    expect(ready).not.toBe(blocked);
    expect(commentActionRowsKey(commentActionRows(target()))).toBe(ready);
  });
});

describe("a comment's own link", () => {
  it("is the comment's http(s) link", () => {
    expect(commentPermalink(COMMENT_URL, PR_URL)).toBe(COMMENT_URL);
  });

  it("is none for a review's body, which links to the pull request itself", () => {
    expect(commentPermalink(PR_URL, PR_URL)).toBeNull();
  });

  it("is none for a missing link or one that isn't http(s)", () => {
    expect(commentPermalink(null, PR_URL)).toBeNull();
    expect(commentPermalink("javascript:alert(1)", PR_URL)).toBeNull();
  });
});

describe("Edit and Delete among a comment's actions", () => {
  const edit = (over: Partial<CommentEditActions> = {}): CommentEditActions => ({
    canEdit: true,
    canDelete: true,
    editing: false,
    saving: false,
    saveable: true,
    conflict: false,
    unconfirmed: false,
    canCheck: true,
    checking: false,
    kept: false,
    hasKeptText: false,
    start: vi.fn(),
    save: vi.fn(),
    cancel: vi.fn(),
    keepMine: vi.fn(),
    loadLatest: vi.fn(),
    askDelete: vi.fn(),
    checkAgain: vi.fn(),
    copyKept: vi.fn(),
    discardKept: vi.fn(),
    ...over,
  });
  const palette = (from: CommentActionTarget) =>
    commentActionRows(from).map((row) => row.definition.paletteLabel);

  it("puts Edit first and Delete… last, set apart as destructive", () => {
    const rows = commentActionRows(target({ edit: edit() }));
    expect(rows.map((row) => row.definition.menuLabel)).toEqual([
      "Edit",
      "Quote Reply",
      "Copy Link",
      "Open in Browser",
      "Delete…",
    ]);
    expect(rows.at(-1)?.definition.destructive).toBe(true);
  });

  it("shows both unavailable, with why, while a change is held", () => {
    const rows = commentActionRows(
      target({ edit: edit({ blockedReason: "Check this comment before changing it again" }) }),
    );
    for (const id of ["edit", "delete"]) {
      expect(rows.find((row) => row.definition.id === id)?.disabledReason).toBe(
        "Check this comment before changing it again",
      );
    }
  });

  it("offers Cancel in ⌘K while editing, and the conflict's two choices", () => {
    expect(palette(target({ edit: edit({ editing: true, conflict: true }) }))).toEqual([
      "Quote Reply",
      "Copy Comment Link",
      "Open Comment in Browser",
      "Delete Comment…",
      "Cancel Comment Edit",
      "Keep My Comment Edit",
      "Load Latest Comment",
    ]);
  });

  it("offers Check Again only where this Mac can read the comment as the account that changed it", () => {
    expect(palette(target({ edit: edit({ unconfirmed: true }) }))).toContain("Check Comment Again");
    expect(palette(target({ edit: edit({ unconfirmed: true, canCheck: false }) }))).not.toContain(
      "Check Comment Again",
    );
  });

  it("offers only Copy and Discard for text kept after the comment was deleted", () => {
    const kept = commentActionRows(target({ edit: edit({ kept: true, hasKeptText: true }) }));
    expect(kept.map((row) => row.definition.paletteLabel)).toEqual([
      "Quote Reply",
      "Copy Comment Link",
      "Open Comment in Browser",
      "Copy Unsaved Comment Text",
      "Discard Unsaved Comment Text",
    ]);
    expect(kept.filter((row) => row.definition.paletteOnly)).toHaveLength(2);
  });
});

describe("a change GitHub didn't confirm, among the rows", () => {
  it("offers Copy and Discard for its text, whether or not Check Again can run", () => {
    const rows = (over: Partial<CommentEditActions>) =>
      commentActionRows(
        target({
          edit: {
            canEdit: true,
            canDelete: true,
            editing: false,
            saving: false,
            saveable: false,
            conflict: false,
            unconfirmed: true,
            canCheck: false,
            checking: false,
            kept: false,
            hasKeptText: true,
            start: vi.fn(),
            save: vi.fn(),
            cancel: vi.fn(),
            keepMine: vi.fn(),
            loadLatest: vi.fn(),
            askDelete: vi.fn(),
            checkAgain: vi.fn(),
            copyKept: vi.fn(),
            discardKept: vi.fn(),
            ...over,
          },
        }),
      ).map((row) => row.definition.paletteLabel);
    expect(rows({})).toEqual(
      expect.arrayContaining(["Copy Unsaved Comment Text", "Discard Unsaved Comment Text"]),
    );
    expect(rows({})).not.toContain("Check Comment Again");
    expect(rows({ canCheck: true })).toContain("Check Comment Again");
    expect(rows({ hasKeptText: false })).not.toContain("Copy Unsaved Comment Text");
  });
});
