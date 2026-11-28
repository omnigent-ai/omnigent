// The edit surface as each card shows it: the editor in the body's place, the short notes
// about a change, and the confirmations before deleting or discarding.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { CommentEditActions } from "./commentActions";
import { type CommentEditEntry, EMPTY_ENTRY } from "./commentEditStore";
import { PullRequestCommentEditSurface } from "./PullRequestCommentEditSurface";
import type { CommentEditing } from "./usePullRequestCommentEditing";

// The PR editors' completion reads go through react-query, as in the app.
const queryClient = new QueryClient();

function actions(over: Partial<CommentEditActions> = {}): CommentEditActions {
  return {
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
  };
}

function editing(
  entry: Partial<CommentEditEntry> = {},
  over: Partial<CommentEditing> = {},
  actionsOver: Partial<CommentEditActions> = {},
): CommentEditing {
  return {
    actions: actions(actionsOver),
    entry: { ...EMPTY_ENTRY, ...entry },
    setText: vi.fn(),
    deleteAsked: false,
    setDeleteAsked: vi.fn(),
    confirmDelete: vi.fn(),
    discardAsked: false,
    setDiscardAsked: vi.fn(),
    confirmDiscard: vi.fn(),
    ...over,
  };
}

function show(state: CommentEditing, onOpenOnGitHub?: () => void) {
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <PullRequestCommentEditSurface editing={state} onOpenOnGitHub={onOpenOnGitHub}>
          <p>The comment</p>
        </PullRequestCommentEditSurface>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

afterEach(cleanup);

describe("the comment while it's edited", () => {
  it("shows the comment as it is until the editor opens", () => {
    show(editing());
    expect(screen.getByText("The comment")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Edit comment" })).toBeNull();
  });

  it("puts the editor in the comment's place, with Cancel and Save", () => {
    const state = editing(
      { editing: { text: "Draft", expected: "Before" } },
      {},
      { editing: true },
    );
    show(state);
    expect(screen.queryByText("The comment")).toBeNull();
    const box = screen.getByRole("textbox", { name: "Edit comment" }) as HTMLTextAreaElement;
    expect(box.value).toBe("Draft");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(state.actions!.save).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(state.actions!.cancel).toHaveBeenCalled();
  });

  it("can't be changed or sent while the edit saves", () => {
    show(
      editing(
        {
          editing: { text: "Draft", expected: "Before" },
          pending: { change: "edit", requestId: "r" },
        },
        {},
        { editing: true, saving: true, saveable: false },
      ),
    );
    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "Edit comment" })).toBeDisabled();
  });

  it("offers Keep Mine and Load Latest when GitHub's text changed", () => {
    const state = editing(
      { editing: { text: "Mine", expected: "Before" }, conflict: { current: "Theirs" } },
      {},
      { editing: true, conflict: true, saveable: false },
    );
    show(state);
    expect(
      screen.getByText("This comment changed on GitHub while you were editing."),
    ).toBeVisible();
    // Save waits for a choice, so it never silently overwrites theirs.
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Keep Mine" }));
    fireEvent.click(screen.getByRole("button", { name: "Load Latest" }));
    expect(state.actions!.keepMine).toHaveBeenCalled();
    expect(state.actions!.loadLatest).toHaveBeenCalled();
  });
});

describe("a change GitHub didn't confirm", () => {
  const unknown = {
    change: "edit" as const,
    requestId: "r",
    sent: "After",
    expected: "Before",
    check: {
      prUrl: "u",
      repository: "acme/app",
      number: 1,
      commentId: "IC_1",
      kind: "issue" as const,
      login: "mac-octo",
      reader: null,
    },
  };

  it("says so in one short line, with Check Again and Open on GitHub", () => {
    const open = vi.fn();
    const state = editing({ unknown }, {}, { unconfirmed: true });
    show(state, open);
    expect(screen.getByRole("status")).toHaveTextContent("GitHub didn’t confirm this change.");
    fireEvent.click(screen.getByRole("button", { name: "Check Again" }));
    fireEvent.click(screen.getByRole("button", { name: "Open on GitHub" }));
    expect(state.actions!.checkAgain).toHaveBeenCalled();
    expect(open).toHaveBeenCalled();
  });

  it("says when the comment couldn't be read just now", () => {
    show(editing({ unknown, checkFailed: true }, {}, { unconfirmed: true }));
    expect(screen.getByRole("status")).toHaveTextContent("couldn’t be read just now");
  });
});

describe("text kept after the comment was deleted", () => {
  it("shows it with Copy Text and Discard", () => {
    const state = editing({ tombstone: { text: "My edit" } }, {}, { kept: true });
    show(state);
    expect(
      screen.getByText("This comment was deleted on GitHub. Your edit wasn’t saved."),
    ).toBeVisible();
    expect(screen.getByText("My edit")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Copy Text" }));
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(state.actions!.copyKept).toHaveBeenCalled();
    expect(state.actions!.discardKept).toHaveBeenCalled();
  });
});

describe("the confirmations", () => {
  it("puts focus on Cancel before deleting, so Enter alone never deletes", async () => {
    const state = editing({}, { deleteAsked: true });
    show(state);
    const dialog = screen.getByRole("dialog", { name: "Delete this comment?" });
    expect(dialog).toHaveTextContent("It’s removed from the pull request on GitHub.");
    await waitFor(() => expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus());
    fireEvent.keyDown(screen.getByRole("button", { name: "Cancel" }), { key: "Enter" });
    expect(state.confirmDelete).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(state.confirmDelete).toHaveBeenCalled();
  });

  it("closes on Esc without deleting", () => {
    const state = editing({}, { deleteAsked: true });
    show(state);
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(state.setDeleteAsked).toHaveBeenCalledWith(false);
    expect(state.confirmDelete).not.toHaveBeenCalled();
  });

  it("asks before discarding changed text, with Keep Editing focused", async () => {
    const state = editing(
      { editing: { text: "Changed", expected: "Before" } },
      { discardAsked: true },
      { editing: true },
    );
    show(state);
    expect(
      screen.getByRole("dialog", { name: "Discard your changes to this comment?" }),
    ).toBeVisible();
    await waitFor(() => expect(screen.getByRole("button", { name: "Keep Editing" })).toHaveFocus());
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(state.confirmDiscard).toHaveBeenCalled();
  });
});
