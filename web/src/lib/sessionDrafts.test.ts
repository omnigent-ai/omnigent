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

  it("persists @-tagged files and sessions across a reload, dropping invalid entries", async () => {
    const text = "Ask @Alpha planning about @src/a.ts";
    const mentions = {
      files: [{ path: "src/a.ts", isDir: false, token: "@src/a.ts" }],
      sessions: [
        { id: "s_alpha", title: "Alpha planning", isSubagent: false, token: "@Alpha planning" },
      ],
    };
    const { setSessionDraft } = await import("./sessionDrafts");
    setSessionDraft("conversation", { text, files: [], mentions });
    const stored = JSON.parse(sessionStorage.getItem(key)!);
    expect(stored).toEqual({ conversation: { text, mentions } });

    stored.conversation.mentions.sessions.push({ id: 7 });
    sessionStorage.setItem(key, JSON.stringify(stored));
    vi.resetModules();
    const reloaded = await import("./sessionDrafts");
    expect(reloaded.getSessionDraft("conversation")).toEqual({ text, files: [], mentions });
  });

  it("restores comment-only drafts after reload and discards invalid or cross-session context", async () => {
    const comment = {
      id: "review",
      conversation_id: "conversation",
      path: "file.ts",
      body: "Check this",
      status: "draft",
      start_index: 0,
      end_index: 0,
      created_at: 1,
      updated_at: 1,
      created_by: null,
      anchor_content: null,
      anchor_metadata: {
        pr_url: "https://github.com/acme/app/pull/1",
        side: "LEFT",
        line: 12,
        start_line: null,
        head_sha: "a".repeat(40),
        base_sha: "b".repeat(40),
      },
    };
    sessionStorage.setItem(
      key,
      JSON.stringify({
        conversation: {
          text: "",
          reviewComments: [
            comment,
            null,
            { ...comment, conversation_id: "other" },
            { ...comment, anchor_metadata: {} },
          ],
        },
      }),
    );
    const drafts = await import("./sessionDrafts");
    expect(drafts.hasSessionDraft("conversation")).toBe(true);
    expect(drafts.getSessionDraft("conversation")?.reviewComments).toEqual([comment]);
    drafts.setSessionDraft("conversation", { text: "", files: [] });
    vi.resetModules();
    const reloaded = await import("./sessionDrafts");
    expect(reloaded.getSessionDraft("conversation")?.reviewComments).toEqual([comment]);
    const mentions = {
      files: [{ path: "file.ts", isDir: false, token: "@file.ts" }],
      sessions: [],
    };
    reloaded.setSessionDraft("conversation", { text: "Check @file.ts", files: [], mentions });
    vi.resetModules();
    const withMentions = await import("./sessionDrafts");
    expect(withMentions.getSessionDraft("conversation")).toEqual({
      text: "Check @file.ts",
      files: [],
      mentions,
      reviewComments: [comment],
    });
    withMentions.clearSessionDrafts();
    expect(sessionStorage.getItem(key)).toBeNull();
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

  it("keeps a draft's pasted chips in memory when they are its only tags", async () => {
    const { getSessionDraft, setSessionDraft, setSessionMentions } =
      await import("./sessionDrafts");
    setSessionDraft("conversation", { text: "@a.png", files: [] });
    const file = new File(["x"], "a.png", { type: "image/png" });
    setSessionMentions("conversation", {
      files: [],
      sessions: [],
      attachments: [{ token: "@a.png", file }],
    });
    expect(getSessionDraft("conversation")?.mentions?.attachments).toEqual([
      { token: "@a.png", file },
    ]);
  });

  it("keeps a pasted chip's text but not its file across a reload, as tray files are", async () => {
    const { setSessionDraft } = await import("./sessionDrafts");
    const file = new File(["x"], "pasted-text.txt", { type: "text/plain" });
    setSessionDraft("conversation", {
      text: "Read @pasted-text.txt",
      files: [],
      mentions: { files: [], sessions: [], attachments: [{ token: "@pasted-text.txt", file }] },
    });
    expect(sessionStorage.getItem(key)).not.toContain("attachments");
    vi.resetModules();
    const { getSessionDraft } = await import("./sessionDrafts");
    expect(getSessionDraft("conversation")).toEqual({
      text: "Read @pasted-text.txt",
      files: [],
    });
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

  it("remembers a draft was written even after it is emptied", async () => {
    const { clearSessionDrafts, sessionDraftRevision, sessionDraftWasWritten, setSessionDraft } =
      await import("./sessionDrafts");
    expect(sessionDraftWasWritten("conversation")).toBe(false);
    setSessionDraft("conversation", { text: "typed", files: [] });
    setSessionDraft("conversation", { text: "", files: [] });
    expect(sessionDraftWasWritten("conversation")).toBe(true);
    expect(sessionDraftRevision("conversation")).toBe(2);
    expect(sessionDraftWasWritten("other")).toBe(false);
    clearSessionDrafts();
    expect(sessionDraftWasWritten("conversation")).toBe(false);
    expect(sessionDraftRevision("conversation")).toBe(0);
  });

  // Failed-draft recovery merges two drafts' chips and tray files. Each row states the exact
  // result: unique filenames in send order (tray, then chips), chips that name their own file, and
  // typed text that keeps its meaning.
  type Chips = [string, File][];
  const recoveryRows: [
    string,
    { message: string; files: File[]; chips: Chips },
    { text: string; files: File[]; chips: Chips },
    { message: string; names: string[]; chipTokens: string[] },
  ][] = [
    [
      "a tray file that repeats a chip's name is renamed",
      { message: "", files: [], chips: [["@a.png", new File(["a"], "a.png")]] },
      { text: "see @a.png", files: [new File(["b"], "a.png")], chips: [] },
      { message: "see @a.png", names: ["a (2).png", "a.png"], chipTokens: ["@a.png"] },
    ],
    [
      "a chip that repeats another chip's name avoids a spelling already typed",
      {
        message: "use @image (2).png",
        files: [],
        chips: [["@image.png", new File(["a"], "image.png")]],
      },
      { text: "look @image.png", files: [], chips: [["@image.png", new File(["b"], "image.png")]] },
      {
        message: "use @image (2).png\n\nlook @image (3).png",
        names: ["image.png", "image (3).png"],
        chipTokens: ["@image.png", "@image (3).png"],
      },
    ],
  ];

  it.each(recoveryRows)(
    "recovers a failed draft with unique names: %s",
    async (_, original, temp, want) => {
      const { recoverFailedSessionDraft, setSessionDraft } = await import("./sessionDrafts");
      const { attachmentFilename } = await import("./attachments");
      const mentionsOf = (chips: Chips) => ({
        files: [],
        sessions: [],
        attachments: chips.map(([token, file]) => ({ token, file })),
      });
      setSessionDraft("temp:rows", {
        text: temp.text,
        files: temp.files,
        mentions: temp.chips.length > 0 ? mentionsOf(temp.chips) : undefined,
      });
      const recovered = recoverFailedSessionDraft(
        { message: original.message, files: original.files, mentions: mentionsOf(original.chips) },
        "temp:rows",
      );
      const chips = recovered.mentions?.attachments ?? [];
      const names = [...recovered.files, ...chips.map((chip) => chip.file)].map(attachmentFilename);
      expect(new Set(names).size).toBe(names.length);
      expect(names).toEqual(want.names);
      expect(chips.map((chip) => chip.token)).toEqual(want.chipTokens);
      for (const chip of chips) expect(chip.file.name).toBe(chip.token.slice(1));
      expect(recovered.message).toBe(want.message);
    },
  );

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

  it("keeps each recovered tag on its own target when both drafts used the same token", async () => {
    const { recoverFailedSessionDraft, setSessionDraft } = await import("./sessionDrafts");
    const { composerInlineMentions, expandMentionTokens } = await import("./composerMentions");
    // Two different sessions titled "Plan", tagged in the launcher and in the
    // temporary session; each composer gave its tag the plain "@Plan" token.
    const planA = { id: "s_aaaaaaaa", title: "Plan", isSubagent: false, token: "@Plan" };
    const planB = { id: "s_bbbbbbbb", title: "Plan", isSubagent: false, token: "@Plan" };
    setSessionDraft("temp:clash", {
      text: "and also @Plan",
      files: [],
      mentions: { files: [], sessions: [planB] },
    });

    const recovered = recoverFailedSessionDraft(
      { message: "read @Plan", files: [], mentions: { files: [], sessions: [planA] } },
      "temp:clash",
    );

    const inline = composerInlineMentions(recovered.mentions!, "claude-native");
    const sent = expandMentionTokens(recovered.message, inline);
    expect(sent).toBe(
      'read [Session: "Plan" id=s_aaaaaaaa subagent=no]\n\n' +
        'and also [Session: "Plan" id=s_bbbbbbbb subagent=no]',
    );
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
