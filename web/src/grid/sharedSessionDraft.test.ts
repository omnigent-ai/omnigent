import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { Comment } from "@/hooks/useComments";
import { serializeReplyDraft, type StoredReplyDraft } from "@/lib/replyDraft";
import {
  clearSessionDrafts,
  getSessionDraft,
  setSessionDraft,
  type SessionDraft,
} from "@/lib/sessionDrafts";
import { gridCellComposerKey, useChatStore, type SideChatComposerDraft } from "@/store/chatStore";
import { useSharedSessionDraft } from "./sharedSessionDraft";

const sessionId = "conv_cell";
const cellKey = gridCellComposerKey(sessionId);
const initialComposers = useChatStore.getState().sideChatComposers;

const reviewComment = {
  id: "review",
  conversation_id: sessionId,
  path: "src/app.ts",
  body: "Rename this",
  status: "draft",
  start_index: 0,
  end_index: 0,
  created_at: 1,
  updated_at: 1,
  created_by: null,
  anchor_content: null,
  anchor_metadata: {
    pr_url: "https://github.example.com/acme/app/pull/1",
    side: "RIGHT",
    line: 4,
    head_sha: "a".repeat(40),
    base_sha: "b".repeat(40),
  },
} as Comment;

/** The cell's composer entry, as the cell's composer edits it. */
function cellComposer(): SideChatComposerDraft | undefined {
  return useChatStore.getState().sideChatComposers[cellKey];
}

/** Edit the cell's composer the way its textarea and attachment controls do. */
function editCell(next: Partial<SideChatComposerDraft>): void {
  act(() =>
    useChatStore.getState().updateSideChatComposer(cellKey, (current) => ({ ...current, ...next })),
  );
}

function seedDraft(draft: SessionDraft): void {
  setSessionDraft(sessionId, draft);
}

beforeEach(() => {
  clearSessionDrafts();
  useChatStore.setState({ sideChatComposers: {} });
});

afterEach(() => {
  clearSessionDrafts();
  useChatStore.setState({ sideChatComposers: initialComposers });
});

describe("a grid cell's shared draft", () => {
  it("starts the cell from the session draft's text, quote cards and attachments", () => {
    const file = new File(["notes"], "notes.txt", { type: "text/plain" });
    const replyDraft: StoredReplyDraft = {
      version: 1,
      quotes: [{ before: "Intro", text: "Quoted line" }],
      text: "Reply",
    };
    seedDraft({ text: serializeReplyDraft(replyDraft), replyDraft, files: [file] });

    renderHook(() => useSharedSessionDraft(sessionId));

    expect(cellComposer()).toMatchObject({ text: "Reply", files: [file] });
    expect(cellComposer()?.quotes).toEqual([
      expect.objectContaining({ before: "Intro", text: "Quoted line" }),
    ]);
  });

  it("writes the cell's text, quotes and attachments back to the session draft", () => {
    seedDraft({ text: "", files: [] });
    renderHook(() => useSharedSessionDraft(sessionId));
    const file = new File(["x"], "a.txt");

    editCell({ text: "Typed in the cell", files: [file] });
    expect(getSessionDraft(sessionId)).toMatchObject({
      text: "Typed in the cell",
      files: [file],
    });

    editCell({ quotes: [{ id: "q", before: "Above", text: "Quoted" }] });
    expect(getSessionDraft(sessionId)).toMatchObject({
      text: serializeReplyDraft({
        quotes: [{ before: "Above", text: "Quoted" }],
        text: "Typed in the cell",
      }),
      replyDraft: {
        version: 1,
        quotes: [{ before: "Above", text: "Quoted" }],
        text: "Typed in the cell",
      },
    });
  });

  it("keeps the session's review comments while the cell writes", () => {
    seedDraft({ text: "", files: [], reviewComments: [reviewComment] });
    renderHook(() => useSharedSessionDraft(sessionId));

    editCell({ text: "Address the comment" });

    expect(getSessionDraft(sessionId)).toMatchObject({
      text: "Address the comment",
      reviewComments: [reviewComment],
    });
  });

  it("keeps the cell's composer as it is when only the session's review comments change", () => {
    seedDraft({
      text: serializeReplyDraft({ quotes: [{ before: "Intro", text: "Quoted" }], text: "Reply" }),
      replyDraft: { version: 1, quotes: [{ before: "Intro", text: "Quoted" }], text: "Reply" },
      files: [],
    });
    renderHook(() => useSharedSessionDraft(sessionId));
    const composer = cellComposer();

    act(() => {
      const draft = getSessionDraft(sessionId)!;
      setSessionDraft(sessionId, { ...draft, reviewComments: [reviewComment] });
    });

    expect(cellComposer()).toBe(composer);
  });

  it("shows a session draft that another writer changes while the cell is mounted", () => {
    seedDraft({ text: "Shared draft", files: [] });
    renderHook(() => useSharedSessionDraft(sessionId));

    act(() => setSessionDraft(sessionId, { text: "Rewound prompt", files: [] }));
    expect(cellComposer()?.text).toBe("Rewound prompt");

    editCell({ text: "Rewound prompt, edited" });
    expect(getSessionDraft(sessionId)?.text).toBe("Rewound prompt, edited");
  });

  it("drops a tag once its token is deleted, so retyping the token stays literal", () => {
    seedDraft({
      text: "Read @src/app.ts first",
      files: [],
      mentions: {
        files: [{ path: "src/app.ts", isDir: false, token: "@src/app.ts" }],
        sessions: [],
      },
    });
    renderHook(() => useSharedSessionDraft(sessionId));

    editCell({ text: "Read the file first" });
    expect(getSessionDraft(sessionId)?.mentions).toBeUndefined();

    editCell({ text: "Read @src/app.ts first" });
    expect(getSessionDraft(sessionId)?.mentions).toBeUndefined();
  });

  it("keeps a tag while its token stays in the text", () => {
    seedDraft({
      text: "Read @src/app.ts",
      files: [],
      mentions: {
        files: [{ path: "src/app.ts", isDir: false, token: "@src/app.ts" }],
        sessions: [],
      },
    });
    renderHook(() => useSharedSessionDraft(sessionId));

    editCell({ text: "Read @src/app.ts carefully" });

    expect(getSessionDraft(sessionId)?.mentions?.files).toEqual([
      { path: "src/app.ts", isDir: false, token: "@src/app.ts" },
    ]);
  });

  it("keeps a tag found in a quote's preamble until its token is deleted from it", () => {
    const replyDraft: StoredReplyDraft = {
      version: 1,
      quotes: [{ before: "Read @src/app.ts first", text: "Quoted" }],
      text: "",
    };
    seedDraft({
      text: serializeReplyDraft(replyDraft),
      replyDraft,
      files: [],
      mentions: {
        files: [{ path: "src/app.ts", isDir: false, token: "@src/app.ts" }],
        sessions: [],
      },
    });
    renderHook(() => useSharedSessionDraft(sessionId));
    const [quote] = cellComposer()!.quotes!;

    editCell({ quotes: [{ ...quote!, before: "Read @src/app.ts carefully" }] });
    expect(getSessionDraft(sessionId)?.mentions?.files).toHaveLength(1);

    editCell({ quotes: [{ ...quote!, before: "Read the file first" }] });
    expect(getSessionDraft(sessionId)?.mentions).toBeUndefined();

    editCell({ quotes: [{ ...quote!, before: "Read @src/app.ts first" }] });
    expect(getSessionDraft(sessionId)?.mentions).toBeUndefined();
  });

  it("does not keep a tag whose token appears only in a quote's source text", () => {
    const replyDraft: StoredReplyDraft = {
      version: 1,
      quotes: [{ before: "Intro", text: "See @src/app.ts" }],
      text: "Reply",
    };
    seedDraft({
      text: serializeReplyDraft(replyDraft),
      replyDraft,
      files: [],
      mentions: {
        files: [{ path: "src/app.ts", isDir: false, token: "@src/app.ts" }],
        sessions: [],
      },
    });
    renderHook(() => useSharedSessionDraft(sessionId));

    editCell({ text: "Reply again" });

    expect(getSessionDraft(sessionId)?.mentions).toBeUndefined();
  });

  it("keeps the cell's focus request when another writer changes its draft", () => {
    seedDraft({ text: "Shared draft", files: [] });
    renderHook(() => useSharedSessionDraft(sessionId));
    editCell({ autoFocus: true });

    act(() => setSessionDraft(sessionId, { text: "Rewound prompt", files: [] }));

    expect(cellComposer()).toMatchObject({ text: "Rewound prompt", autoFocus: true });
  });

  it("re-reads a draft whose quote cards became plain text with the same serialized text", () => {
    const replyDraft: StoredReplyDraft = {
      version: 1,
      quotes: [{ before: "Intro", text: "Quoted" }],
      text: "Reply",
    };
    seedDraft({ text: serializeReplyDraft(replyDraft), replyDraft, files: [] });
    renderHook(() => useSharedSessionDraft(sessionId));

    act(() => setSessionDraft(sessionId, { text: serializeReplyDraft(replyDraft), files: [] }));

    expect(cellComposer()?.quotes ?? []).toEqual([]);
    expect(cellComposer()?.text).toBe(serializeReplyDraft(replyDraft));
  });

  it("empties the session draft and its tags when the cell's composer is cleared", () => {
    seedDraft({
      text: "Read @src/app.ts",
      files: [],
      mentions: {
        files: [{ path: "src/app.ts", isDir: false, token: "@src/app.ts" }],
        sessions: [],
      },
    });
    renderHook(() => useSharedSessionDraft(sessionId));

    act(() => useChatStore.getState().clearSideChatComposer(cellKey));

    expect(getSessionDraft(sessionId)).toBeUndefined();
  });

  it("leaves a sub-agent tab's own composer entry alone while the cell is mounted and after", () => {
    const subAgentDraft: SideChatComposerDraft = { text: "Sub-agent question", files: [] };
    useChatStore.setState({ sideChatComposers: { [sessionId]: subAgentDraft } });
    seedDraft({ text: "Shared draft", files: [] });

    const { unmount } = renderHook(() => useSharedSessionDraft(sessionId));
    editCell({ text: "Shared draft, edited" });
    expect(useChatStore.getState().sideChatComposers[sessionId]).toBe(subAgentDraft);

    unmount();

    expect(useChatStore.getState().sideChatComposers[sessionId]).toBe(subAgentDraft);
  });

  it("keeps a sub-agent tab's edits made while the cell is mounted", () => {
    seedDraft({ text: "Shared draft", files: [] });
    const { unmount } = renderHook(() => useSharedSessionDraft(sessionId));

    // The sub-agent's Workspace composer edits its own entry while the cell is open.
    act(() =>
      useChatStore
        .getState()
        .updateSideChatComposer(sessionId, (current) => ({ ...current, text: "Workspace edit" })),
    );
    unmount();

    expect(useChatStore.getState().sideChatComposers[sessionId]?.text).toBe("Workspace edit");
  });

  it("removes the cell's composer entry when the cell unmounts", () => {
    seedDraft({ text: "Shared draft", files: [] });

    const { unmount } = renderHook(() => useSharedSessionDraft(sessionId));
    expect(cellComposer()?.text).toBe("Shared draft");
    unmount();

    expect(cellComposer()).toBeUndefined();
    expect(getSessionDraft(sessionId)?.text).toBe("Shared draft");
  });
});
