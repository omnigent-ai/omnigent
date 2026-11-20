import { act, render, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReviewTextEvent } from "@/components/diff-review/useReviewTextLayer";
import type * as CommentsModule from "@/hooks/useComments";
import type { Comment } from "@/hooks/useComments";
import { useChatStore } from "@/store/chatStore";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import type { ActiveSelection } from "./codeViewerHelpers";
import {
  actionsOnlyCommentContext,
  useReviewCommentLayer,
  type ReviewCommentContext,
} from "./useReviewCommentLayer";

const sessionComments = vi.hoisted(() => ({ list: [] as Comment[], read: [] as unknown[] }));
vi.mock("@/hooks/useComments", async (importOriginal) => ({
  ...(await importOriginal<typeof CommentsModule>()),
  // Like the real hook, a missing session reads nothing.
  useComments: (sessionId: string | undefined) => {
    sessionComments.read.push(sessionId);
    return { data: sessionId ? sessionComments.list : undefined };
  },
}));
const permission = vi.hoisted(() => ({ canEdit: true }));
vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: () => permission.canEdit }));

function comment(id: string, path: string, start: number, end: number): Comment {
  return {
    id,
    path,
    conversation_id: "conv",
    body: "",
    status: "draft",
    start_index: start,
    end_index: end,
    anchor_content: null,
    created_at: 0,
    created_by: null,
  } as Comment;
}

const lines: ReadonlyMap<string, readonly string[]> = new Map([
  ["a.ts", ["const a = 1;\n", "const b = 2;\n"]],
  ["b.ts", ["export const name = 'x';\n"]],
]);
const otherComment = comment("other", "b.ts", 13, 17);

interface HookProps {
  activeSelection: ActiveSelection | null;
  currentLines?: ReadonlyMap<string, readonly string[]>;
}

function setup(overrides: Partial<ReviewCommentContext> = {}) {
  const onSetActiveSelection = vi.fn();
  const pendingBodyRef = { current: "" };
  const context = (activeSelection: ActiveSelection | null): ReviewCommentContext => ({
    currentComments: [],
    activeSelection,
    onSetActiveSelection,
    canCommentOn: () => true,
    pendingBodyRef,
    ...overrides,
  });
  const hook = renderHook(
    ({ activeSelection, currentLines = lines }: HookProps) =>
      useReviewCommentLayer({
        conversationId: "conv",
        currentPath: "a.ts",
        context: context(activeSelection),
        currentLines,
      }),
    { initialProps: { activeSelection: null } as HookProps },
  );
  const send = (event: ReviewTextEvent) => act(() => hook.result.current.onTextEvent(event));
  return { ...hook, onSetActiveSelection, pendingBodyRef, send };
}

function paletteLabels(): string[] {
  return Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) =>
    group.commands.map((command) => command.label),
  );
}

const selection: ReviewTextEvent = {
  kind: "select",
  path: "a.ts",
  start: 6,
  end: 7,
  position: { left: 10, top: 20 },
};

beforeEach(() => {
  sessionComments.list = [otherComment];
  sessionComments.read = [];
  permission.canEdit = true;
});
afterEach(() => useChatStore.setState({ sessionHarness: null }));

describe("useReviewCommentLayer", () => {
  it("highlights comments in every file", () => {
    const { result } = setup();
    expect(result.current.highlights).toEqual([
      { path: "b.ts", start: 13, end: 17, active: false },
    ]);
  });

  it("focuses the comment a click lands on, in whichever file it is", () => {
    const { send, onSetActiveSelection } = setup();
    send({ kind: "click", path: "b.ts", offset: 14 });
    expect(onSetActiveSelection).toHaveBeenCalledWith(
      "b.ts",
      expect.objectContaining({ comment_id: "other", start_index: 13, end_index: 17 }),
    );
  });

  it("clears the active comment on a click elsewhere, unless a draft is in progress", () => {
    const { send, rerender, onSetActiveSelection, pendingBodyRef } = setup();
    rerender({ activeSelection: { start_index: 0, end_index: 5, anchor_content: "const" } });
    pendingBodyRef.current = "half-written";
    send({ kind: "click", path: "a.ts", offset: 20 });
    expect(onSetActiveSelection).not.toHaveBeenCalled();
    pendingBodyRef.current = "";
    send({ kind: "click" });
    expect(onSetActiveSelection).toHaveBeenCalledWith("a.ts", null);
  });

  it("offers Add comment on a selection and anchors the comment to its text", () => {
    const { send, result, onSetActiveSelection } = setup();
    send(selection);
    const { getByRole, queryByRole } = render(result.current.actions as React.ReactElement);
    expect(queryByRole("button", { name: "Attach to agent" })).toBeNull();
    act(() => {
      getByRole("button", { name: "Add comment" }).dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
    });
    expect(onSetActiveSelection).toHaveBeenCalledWith("a.ts", {
      start_index: 6,
      end_index: 7,
      anchor_content: "a",
    });
    expect(result.current.actions).toBeNull();
  });

  it("offers nothing without edit permission or where the viewer can't comment", () => {
    permission.canEdit = false;
    const readOnly = setup();
    readOnly.send(selection);
    expect(readOnly.result.current.actions).toBeNull();
    permission.canEdit = true;
    const elsewhere = setup({ canCommentOn: (path) => path !== "a.ts" });
    elsewhere.send(selection);
    expect(elsewhere.result.current.actions).toBeNull();
  });

  it("hides the actions for whitespace-only selections and when the selection clears", () => {
    const { send, result } = setup();
    send({ ...selection, start: 12, end: 13 });
    expect(result.current.actions).toBeNull();
    send(selection);
    expect(result.current.actions).not.toBeNull();
    send({ kind: "clear" });
    expect(result.current.actions).toBeNull();
  });

  it("attaches the selected lines to the agent on native sessions", () => {
    useChatStore.setState({ sessionHarness: "claude-native" });
    const addComposerAttachment = vi.spyOn(useChatStore.getState(), "addComposerAttachment");
    const { send, result } = setup();
    send({ ...selection, start: 0, end: 13 });
    const { getByRole } = render(result.current.actions as React.ReactElement);
    act(() => {
      getByRole("button", { name: "Attach to agent" }).dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
    });
    expect(addComposerAttachment).toHaveBeenCalledWith({
      path: "a.ts",
      isDir: false,
      lineRange: { start: 1, end: 1 },
    });
  });

  it("hides the actions on scroll but keeps the selection for the command palette", () => {
    const { send, result } = setup();
    send(selection);
    send({ kind: "scroll" });
    expect(result.current.actions).toBeNull();
    expect(paletteLabels()).toContain("Add comment");
    send({ kind: "clear" });
    expect(paletteLabels()).not.toContain("Add comment");
  });

  it("withdraws a selection whose file changed before either action ran", () => {
    useChatStore.setState({ sessionHarness: "claude-native" });
    const addComposerAttachment = vi
      .spyOn(useChatStore.getState(), "addComposerAttachment")
      .mockClear();
    const { send, result, rerender, onSetActiveSelection } = setup();
    send(selection);
    expect(paletteLabels()).toEqual(["Add comment", "Attach to agent"]);
    const refreshed = new Map(lines);
    refreshed.set("a.ts", ["// added\n", "const a = 1;\n", "const b = 2;\n"]);
    rerender({ activeSelection: null, currentLines: refreshed });
    expect(result.current.actions).toBeNull();
    expect(paletteLabels()).toEqual([]);
    expect(onSetActiveSelection).not.toHaveBeenCalled();
    expect(addComposerAttachment).not.toHaveBeenCalled();
  });

  it("brings a comment made active elsewhere into view", () => {
    const { result, rerender } = setup();
    expect(result.current.revealTarget).toBeNull();
    rerender({ activeSelection: { start_index: 15, end_index: 20, anchor_content: "b = 2" } });
    expect(result.current.revealTarget).toEqual({ path: "a.ts", line: 2, align: "nearest" });
  });
});

describe("useReviewCommentLayer with actions only", () => {
  // Stored comments in the current file and another, which this mode must not show or open.
  beforeEach(() => {
    sessionComments.list = [comment("here", "a.ts", 0, 5), otherComment];
  });

  function actionsOnly() {
    const onAddComment = vi.fn();
    const onSetActiveSelection = vi.fn();
    const layer = setup({ storedComments: false, onAddComment, onSetActiveSelection });
    return { ...layer, onAddComment, onSetActiveSelection };
  }

  it("neither reads nor highlights the session's comments in any file", () => {
    const { result } = actionsOnly();
    expect(result.current.highlights).toEqual([]);
    expect(sessionComments.read.length).toBeGreaterThan(0);
    expect(sessionComments.read.every((sessionId) => sessionId === undefined)).toBe(true);
  });

  it("opens no stored comment on a click, in this file or another", () => {
    const { send, onSetActiveSelection } = actionsOnly();
    send({ kind: "click", path: "a.ts", offset: 2 });
    send({ kind: "click", path: "b.ts", offset: 14 });
    send({ kind: "click" });
    expect(onSetActiveSelection).not.toHaveBeenCalled();
  });

  it("sends Add comment to its handler with the file text, not to the active selection", () => {
    const { send, result, onAddComment, onSetActiveSelection } = actionsOnly();
    send(selection);
    const { getByRole } = render(result.current.actions as React.ReactElement);
    act(() => {
      getByRole("button", { name: "Add comment" }).dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
    });
    expect(onAddComment).toHaveBeenCalledWith(
      "a.ts",
      { start_index: 6, end_index: 7, anchor_content: "a" },
      "const a = 1;\nconst b = 2;\n",
    );
    expect(onSetActiveSelection).not.toHaveBeenCalled();
    expect(result.current.actions).toBeNull();
  });

  it("still attaches the selected lines to the agent on native sessions", () => {
    useChatStore.setState({ sessionHarness: "claude-native" });
    const addComposerAttachment = vi
      .spyOn(useChatStore.getState(), "addComposerAttachment")
      .mockClear();
    const { send, result } = actionsOnly();
    send({ ...selection, start: 0, end: 13 });
    const { getByRole } = render(result.current.actions as React.ReactElement);
    act(() => {
      getByRole("button", { name: "Attach to agent" }).dispatchEvent(
        new MouseEvent("click", { bubbles: true }),
      );
    });
    expect(addComposerAttachment).toHaveBeenCalledWith({
      path: "a.ts",
      isDir: false,
      lineRange: { start: 1, end: 1 },
    });
  });

  it("withdraws a pending selection's actions and commands when commenting stops", () => {
    const context = actionsOnlyCommentContext({ canCommentOn: () => true, onAddComment: vi.fn() });
    const { result, rerender } = renderHook(
      ({ commenting }: { commenting: boolean }) =>
        useReviewCommentLayer({
          conversationId: "conv",
          currentPath: "a.ts",
          context: commenting ? context : undefined,
          currentLines: lines,
        }),
      { initialProps: { commenting: true } },
    );
    act(() => result.current.onTextEvent(selection));
    expect(result.current.actions).not.toBeNull();
    expect(paletteLabels()).toContain("Add comment");
    rerender({ commenting: false });
    expect(result.current.actions).toBeNull();
    expect(paletteLabels()).not.toContain("Add comment");
  });

  it("brings no selection into view", () => {
    const { result, rerender } = actionsOnly();
    rerender({ activeSelection: { start_index: 15, end_index: 20, anchor_content: "b = 2" } });
    expect(result.current.revealTarget).toBeNull();
  });

  it("is what actionsOnlyCommentContext makes", () => {
    const onAddComment = vi.fn();
    const canCommentOn = () => true;
    expect(actionsOnlyCommentContext({ canCommentOn, onAddComment })).toMatchObject({
      currentComments: [],
      activeSelection: null,
      storedComments: false,
      canCommentOn,
      onAddComment,
    });
  });
});
