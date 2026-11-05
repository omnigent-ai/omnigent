import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import { serializeReplyDraft, type StoredReplyDraft } from "./replyDraft";

const key = "omnigent.sessionDrafts";

describe("session drafts", () => {
  beforeEach(() => {
    vi.resetModules();
    sessionStorage.clear();
  });
  afterEach(() => sessionStorage.clear());
  afterEach(cleanup);

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
        badStartupIds: { text, startupInputIds: ["pending_first", 42] },
        noStartupIds: { text, startupInputIds: [] },
        badSentAs: { text, sentAs: ["stable_first", 42] },
      }),
    );
    const { getSessionDraft } = await import("./sessionDrafts");
    expect(getSessionDraft("invalid")).toEqual({ text, files: [] });
    expect(getSessionDraft("newer")).toEqual({ text, files: [] });
    expect(getSessionDraft("badStartupIds")).toEqual({ text, files: [] });
    expect(getSessionDraft("noStartupIds")).toEqual({ text, files: [] });
    expect(getSessionDraft("badSentAs")).toEqual({ text, files: [] });
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

  it("keeps the startup inputs and sends a draft holds across edits and a reload", async () => {
    const { addSessionDraftStartupInputs, getSessionDraft, setSessionDraft } =
      await import("./sessionDrafts");
    addSessionDraftStartupInputs("conversation", "first", [], [], ["stable_first"]);
    setSessionDraft("conversation", { text: "first, edited", files: [] });
    addSessionDraftStartupInputs("conversation", "second", ["pending_first", "pending_second"]);
    const held = {
      text: "first, edited",
      files: [],
      startupInputIds: ["pending_first", "pending_second"],
      sentAs: ["stable_first"],
    };
    expect(getSessionDraft("conversation")).toEqual(held);

    vi.resetModules();
    const reloaded = await import("./sessionDrafts");
    expect(reloaded.getSessionDraft("conversation")).toEqual(held);
    reloaded.setSessionDraft("conversation", { text: "", files: [] });
    // Later reads in this tab still learn which sends the cleared draft held.
    expect(reloaded.getDroppedDraftSends("conversation")).toEqual(["stable_first"]);
    reloaded.setSessionDraft("conversation", { text: "new", files: [] });
    expect(JSON.parse(sessionStorage.getItem(key)!)).toEqual({ conversation: "new" });
    reloaded.addSessionDraftStartupInputs("conversation", "new", ["pending_first"], [], ["sent"]);
    reloaded.setSessionDraft("conversation", {
      text: "new",
      files: [],
      startupInputIds: [],
      sentAs: [],
    });
    expect(reloaded.getSessionDraft("conversation")).toEqual({ text: "new", files: [] });
    expect(JSON.parse(sessionStorage.getItem(key)!)).toEqual({ conversation: "new" });
    reloaded.clearSessionDrafts();
    expect(reloaded.getDroppedDraftSends("conversation")).toEqual([]);
  });

  it("keeps a files-only draft's startup inputs in memory but not across a reload", async () => {
    const { addSessionDraftStartupInputs, getSessionDraft } = await import("./sessionDrafts");
    const file = new File(["data"], "chart.png", { type: "image/png" });
    addSessionDraftStartupInputs("conversation", "", ["pending_first"], [file]);
    expect(getSessionDraft("conversation")).toEqual({
      text: "",
      files: [file],
      startupInputIds: ["pending_first"],
    });
    expect(sessionStorage.getItem(key)).toBeNull();

    // Storage keeps only drafts with text, so a reload offers the input again.
    vi.resetModules();
    const reloaded = await import("./sessionDrafts");
    expect(reloaded.getSessionDraft("conversation")).toBeUndefined();
  });

  it("replaces the startup inputs and sends a draft holds without changing its text", async () => {
    const {
      getDroppedDraftSends,
      getSessionDraft,
      replaceSessionDraftStartupInputs,
      setSessionDraft,
    } = await import("./sessionDrafts");
    setSessionDraft("conversation", {
      text: "held",
      files: [],
      startupInputIds: ["pending_held"],
      sentAs: ["stable_held"],
    });
    replaceSessionDraftStartupInputs("conversation", "recalled", ["pending_first"]);
    expect(getSessionDraft("conversation")).toEqual({
      text: "held",
      files: [],
      startupInputIds: ["pending_first"],
    });
    // Replacing drops the old sends without recording them, so a denied send's re-read offers them again.
    expect(getDroppedDraftSends("conversation")).toEqual([]);
    replaceSessionDraftStartupInputs("conversation", "recalled", []);
    expect(JSON.parse(sessionStorage.getItem(key)!)).toEqual({ conversation: "held" });
    replaceSessionDraftStartupInputs("another", "recalled", ["pending_other"]);
    expect(getSessionDraft("another")).toEqual({
      text: "recalled",
      files: [],
      startupInputIds: ["pending_other"],
    });
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

  it("re-renders useSessionDraftIds only when the set of draft ids changes", async () => {
    const { setSessionDraft, useSessionDraftIds } = await import("./sessionDrafts");
    let renders = 0;
    const { result } = renderHook(() => {
      renders += 1;
      return useSessionDraftIds();
    });
    expect(result.current).toBe("");

    act(() => setSessionDraft("a", { text: "hi", files: [] }));
    expect(result.current).toBe("a");
    const afterAdd = renders;

    // Editing an existing draft's text leaves the id set unchanged, so no re-render.
    act(() => setSessionDraft("a", { text: "hello again", files: [] }));
    expect(renders).toBe(afterAdd);

    act(() => setSessionDraft("a", { text: "", files: [] }));
    expect(result.current).toBe("");
  });

  it("returns an empty snapshot without subscribing when disabled", async () => {
    const { setSessionDraft, useSessionDraftIds } = await import("./sessionDrafts");
    let renders = 0;
    const { result } = renderHook(() => {
      renders += 1;
      return useSessionDraftIds(false);
    });
    const before = renders;

    act(() => setSessionDraft("a", { text: "hi", files: [] }));
    expect(result.current).toBe("");
    expect(renders).toBe(before);
  });
});
