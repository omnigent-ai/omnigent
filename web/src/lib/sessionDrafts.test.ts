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

  it("keeps a draft's text but not its tray files across a reload", async () => {
    const { setSessionDraft } = await import("./sessionDrafts");
    const file = new File(["x"], "pasted-text.txt", { type: "text/plain" });
    setSessionDraft("conversation", { text: "Read @pasted-text.txt", files: [file] });
    vi.resetModules();
    const { getSessionDraft } = await import("./sessionDrafts");
    expect(getSessionDraft("conversation")).toEqual({ text: "Read @pasted-text.txt", files: [] });
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

  // Failed-draft recovery merges two drafts' tags and tray files. A pasted chip has already left
  // its composer as a tray file, so it is one here. Recovery never edits typed text: each row's
  // message is what the drafts typed. Each row checks unique filenames in send order (tray files, in
  // order), and that every tagged token in the message still expands to its own target.
  interface Side {
    message: string;
    files: File[];
    sessions?: { token: string; id: string; title: string }[];
    fileTags?: { token: string; path: string }[];
  }
  const recoveryRows: [string, Side, Side, { message: string; names: string[] }][] = [
    [
      "a tray file that repeats a file's name is renamed",
      { message: "", files: [new File(["a"], "a.png")] },
      { message: "see @a.png", files: [new File(["b"], "a.png")] },
      { message: "see @a.png", names: ["a.png", "a (2).png"] },
    ],
    [
      "a tray file from each draft that repeats a name is renamed, and the typed text stays as typed",
      { message: "use @image (2).png", files: [new File(["a"], "image.png")] },
      { message: "look @image.png", files: [new File(["b"], "image.png")] },
      { message: "use @image (2).png\n\nlook @image.png", names: ["image.png", "image (2).png"] },
    ],
    [
      "two incoming files collide: the incoming one is renamed, and the typed text stays as typed",
      { message: "", files: [new File(["a"], "a.png")] },
      {
        message: "First @a.png then second @a (2).png",
        files: [new File(["b"], "a.png"), new File(["c"], "a (2).png")],
      },
      {
        message: "First @a.png then second @a (2).png",
        names: ["a.png", "a (3).png", "a (2).png"],
      },
    ],
    [
      "a renamed tray file leaves a workspace tag with the same name in place",
      { message: "", files: [new File(["a"], "README.md")] },
      {
        message: "Open @README.md",
        files: [new File(["b"], "README.md")],
        fileTags: [{ token: "@README.md", path: "README.md" }],
      },
      { message: "Open @README.md", names: ["README.md", "README (2).md"] },
    ],
    [
      "a renamed tray file leaves a longer session tag that contains its name as typed",
      { message: "", files: [new File(["a"], "a.png")] },
      {
        message: "Use @a.png and @a (2).png plan",
        files: [new File(["b"], "a.png")],
        sessions: [{ token: "@a (2).png plan", id: "s_plan", title: "a (2).png plan" }],
      },
      { message: "Use @a.png and @a (2).png plan", names: ["a.png", "a (2).png"] },
    ],
    [
      "a session tag that is a prefix of a renamed file's name is left as typed",
      { message: "", files: [new File(["a"], "a.png")] },
      {
        message: "Check @a and @a.png",
        files: [new File(["b"], "a.png")],
        sessions: [{ token: "@a", id: "s_a", title: "a" }],
      },
      { message: "Check @a and @a.png", names: ["a.png", "a (2).png"] },
    ],
  ];

  it.each(recoveryRows)(
    "recovers a failed draft with unique names: %s",
    async (_, original, temp, want) => {
      const { recoverFailedSessionDraft, setSessionDraft } = await import("./sessionDrafts");
      const { attachmentFilename } = await import("./attachments");
      const { composerInlineMentions, expandMentionTokens, findMentionTokens } =
        await import("./composerMentions");
      const mentionsOf = (side: Side) => ({
        files: (side.fileTags ?? []).map(({ token, path }) => ({ token, path, isDir: false })),
        sessions: (side.sessions ?? []).map((session) => ({ ...session, isSubagent: false })),
      });
      const tempHasMentions = (temp.sessions ?? []).length + (temp.fileTags ?? []).length > 0;
      setSessionDraft("temp:rows", {
        text: temp.message,
        files: temp.files,
        mentions: tempHasMentions ? mentionsOf(temp) : undefined,
      });
      const recovered = recoverFailedSessionDraft(
        { message: original.message, files: original.files, mentions: mentionsOf(original) },
        "temp:rows",
      );
      const names = recovered.files.map(attachmentFilename);
      expect(new Set(names).size).toBe(names.length);
      expect(names).toEqual(want.names);
      expect(recovered.message).toBe(want.message);
      // Every tagged token still in the message expands to its own target.
      const inline = composerInlineMentions(
        recovered.mentions ?? { files: [], sessions: [] },
        null,
      );
      const expanded = expandMentionTokens(recovered.message, inline);
      for (const mention of inline) {
        if (mention.marker === mention.token) continue;
        if (findMentionTokens(recovered.message, [mention.token]).length > 0) {
          expect(expanded).toContain(mention.marker);
        }
      }
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
