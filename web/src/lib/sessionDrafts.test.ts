import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { serializeReplyDraft, type StoredReplyDraft } from "./replyDraft";

const key = "omnigent.sessionDrafts";

describe("session drafts", () => {
  beforeEach(() => {
    vi.resetModules();
    sessionStorage.clear();
  });
  afterEach(() => sessionStorage.clear());

  it("loads legacy string drafts as plain text without inferring cards", async () => {
    const text = "\nintro\n> quote\nlazy continuation\n\n";
    sessionStorage.setItem(key, JSON.stringify({ conversation: text }));
    const { getSessionDraft } = await import("./sessionDrafts");
    expect(getSessionDraft("conversation")).toEqual({ text, files: [] });
  });

  it("persists explicit cards and authored Markdown across a reload", async () => {
    const replyDraft: StoredReplyDraft = {
      version: 1,
      quotes: [{ before: "Notes:\n> authored\ncontinued\n\n\n", text: "Reply selection" }],
      text: "~~~markdown\n> example\n",
    };
    const text = serializeReplyDraft(replyDraft);
    const file = new File(["data"], "notes.txt", { type: "text/plain" });
    const { getSessionDraft, setSessionDraft } = await import("./sessionDrafts");
    setSessionDraft("conversation", { text, replyDraft, files: [file] });
    expect(getSessionDraft("conversation")?.files).toEqual([file]);
    expect(JSON.parse(sessionStorage.getItem(key)!)).toEqual({
      conversation: { text, replyDraft },
    });

    vi.resetModules();
    const reloaded = await import("./sessionDrafts");
    expect(reloaded.getSessionDraft("conversation")).toEqual({ text, replyDraft, files: [] });
    expect(reloaded.hasSessionDraft("conversation")).toBe(true);
    expect(reloaded.getSessionDraft("another")).toBeUndefined();
  });

  it("preserves fallback text when stored metadata is invalid or unsupported", async () => {
    const text = "> authored\ncontinued\n";
    sessionStorage.setItem(
      key,
      JSON.stringify({
        invalid: { text, replyDraft: { version: 1, quotes: [null], text: "" } },
        newer: { text, replyDraft: { version: 2, quotes: [], text: "" } },
        notText: { text: 42 },
        nullEntry: null,
      }),
    );
    const { getSessionDraft } = await import("./sessionDrafts");
    expect(getSessionDraft("invalid")).toEqual({ text, files: [] });
    expect(getSessionDraft("newer")).toEqual({ text, files: [] });
    expect(getSessionDraft("notText")).toBeUndefined();
    expect(getSessionDraft("nullEntry")).toBeUndefined();
  });

  it.each(["not json", "null", "[]", "42"])("ignores an invalid storage root: %s", async (raw) => {
    sessionStorage.setItem(key, raw);
    const { getSessionDraft } = await import("./sessionDrafts");
    expect(getSessionDraft("conversation")).toBeUndefined();
  });

  it("keeps plain drafts backwards-compatible and removes empty drafts", async () => {
    const { setSessionDraft, hasSessionDraft } = await import("./sessionDrafts");
    setSessionDraft("conversation", { text: "> typed\ncontinued\n", files: [] });
    expect(JSON.parse(sessionStorage.getItem(key)!)).toEqual({
      conversation: "> typed\ncontinued\n",
    });
    setSessionDraft("conversation", { text: "", files: [] });
    expect(hasSessionDraft("conversation")).toBe(false);
    expect(sessionStorage.getItem(key)).toBeNull();
  });

  it("recovers a failed temporary draft and ignores its late cleanup write", async () => {
    const { getSessionDraft, recoverFailedSessionDraft, setSessionDraft } =
      await import("./sessionDrafts");
    const originalFile = new File(["initial"], "initial.txt");
    const followUpFile = new File(["follow-up"], "follow-up.txt");
    const temporaryDraft = {
      text: "follow-up typed during startup",
      files: [followUpFile],
    };
    setSessionDraft("temp:failed", temporaryDraft);

    expect(
      recoverFailedSessionDraft(
        { message: "original create prompt", files: [originalFile], project: "docs" },
        "temp:failed",
      ),
    ).toEqual({
      message: "original create prompt\n\nfollow-up typed during startup",
      files: [originalFile, followUpFile],
      project: "docs",
    });
    expect(getSessionDraft("temp:failed")).toBeUndefined();

    setSessionDraft("temp:failed", temporaryDraft);
    expect(getSessionDraft("temp:failed")).toBeUndefined();
    expect(sessionStorage.getItem(key)).toBeNull();
  });

  it("keeps a recovered copy's provenance across a reload and knows whether its record is stored", async () => {
    const first = await import("./sessionDrafts");
    first.setSessionDraft("conv", { text: "delivered?", files: [], recoveredFrom: "sid_r" });
    first.recordUnsentMessage("sid_r", {
      conversationId: "conv",
      text: "delivered?",
      stableId: "sid_r",
    });
    vi.resetModules();
    const second = await import("./sessionDrafts");
    expect(second.getSessionDraft("conv")).toEqual({
      text: "delivered?",
      files: [],
      recoveredFrom: "sid_r",
    });
    expect(second.hasUnsentMessage("sid_r")).toBe(true);
    second.clearUnsentMessage("sid_r");
    expect(second.hasUnsentMessage("sid_r")).toBe(false);
  });
});

describe("unsent messages", () => {
  const unsentKey = "omnigent.unsentMessages";
  beforeEach(() => {
    vi.resetModules();
    sessionStorage.clear();
  });
  afterEach(() => sessionStorage.clear());

  it("keeps one record per send and clears only the acknowledged one", async () => {
    const { recordUnsentMessage, clearUnsentMessage } = await import("./sessionDrafts");
    recordUnsentMessage("sid_a", { conversationId: "conv", text: "first", stableId: "sid_a" });
    recordUnsentMessage("sid_b", { conversationId: "conv", text: "second", stableId: "sid_b" });
    recordUnsentMessage("sid_blank", { conversationId: "conv", text: "  " });
    clearUnsentMessage("sid_a");
    expect(JSON.parse(sessionStorage.getItem(unsentKey)!)).toEqual({
      sid_b: { conversationId: "conv", text: "second", stableId: "sid_b" },
    });
  });

  it("recovers a previous page's record once per page and keeps it stored until acknowledged", async () => {
    sessionStorage.setItem(
      unsentKey,
      JSON.stringify({
        sid_old: { conversationId: "conv", text: "from before the reload", stableId: "sid_old" },
        sid_other: { conversationId: "other", text: "not this chat" },
      }),
    );
    const { peekUnsentMessage, markUnsentRecovered, clearUnsentMessage } =
      await import("./sessionDrafts");
    const recovered = peekUnsentMessage("conv");
    expect(recovered).toEqual({
      recordId: "sid_old",
      conversationId: "conv",
      text: "from before the reload",
      stableId: "sid_old",
    });
    // Peeking does not consume; marking does — once per page, and the record
    // survives for the next reload.
    expect(peekUnsentMessage("conv")).toEqual(recovered);
    markUnsentRecovered("sid_old");
    expect(peekUnsentMessage("conv")).toBeUndefined();
    expect(Object.keys(JSON.parse(sessionStorage.getItem(unsentKey)!))).toEqual([
      "sid_old",
      "sid_other",
    ]);
    clearUnsentMessage("sid_old");
    expect(Object.keys(JSON.parse(sessionStorage.getItem(unsentKey)!))).toEqual(["sid_other"]);
  });

  it("acknowledges records whose ids the transcript already holds", async () => {
    sessionStorage.setItem(
      unsentKey,
      JSON.stringify({
        sid_sent: { conversationId: "conv", text: "delivered", stableId: "sid_sent" },
        sid_lost: { conversationId: "conv", text: "never landed", stableId: "sid_lost" },
      }),
    );
    const { acknowledgeUnsentMessages } = await import("./sessionDrafts");
    acknowledgeUnsentMessages(["msg_other", "sid_sent"]);
    expect(Object.keys(JSON.parse(sessionStorage.getItem(unsentKey)!))).toEqual(["sid_lost"]);
  });

  it("never offers a record written during this page", async () => {
    const { recordUnsentMessage, peekUnsentMessage } = await import("./sessionDrafts");
    recordUnsentMessage("sid_now", {
      conversationId: "conv",
      text: "in flight",
      stableId: "sid_now",
    });
    expect(peekUnsentMessage("conv")).toBeUndefined();
  });
});
