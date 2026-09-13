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
});
