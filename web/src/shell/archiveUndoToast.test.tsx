// Tests for the post-archive Undo toast. Contract: archives in quick succession
// merge into ONE toast whose count updates live, Undo unarchives the whole merged
// batch via undoArchiveConversations, and "View archived" navigates to Settings.
// The batch is module state, so it's reset between cases and leftover toasts are
// dismissed.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { QueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import type { Conversation } from "@/hooks/useConversations";

const mocks = vi.hoisted(() => ({ undoArchiveConversations: vi.fn() }));

// archiveUndoToast only pulls undoArchiveConversations from this module; a
// partial mock keeps the toast logic isolated from cache/network behavior.
vi.mock("@/hooks/useConversations", () => ({
  undoArchiveConversations: mocks.undoArchiveConversations,
}));

import { showArchiveUndoToast, resetArchiveUndoBatchForTests } from "./archiveUndoToast";
import { Toaster } from "@/components/ui/sonner";

const queryClient = new QueryClient();

/** Minimal Conversation rows keyed by id — the toast only needs id + count. */
const conv = (id: string): Conversation =>
  ({ id, object: "conversation", title: id, created_at: 0, updated_at: 0 }) as Conversation;

const convs = (...ids: string[]) => ids.map(conv);

let navigate: ReturnType<typeof vi.fn>;

function mountToaster() {
  render(
    <MemoryRouter>
      <Toaster />
    </MemoryRouter>,
  );
}

const show = (ids: string[]) =>
  act(() =>
    showArchiveUndoToast(
      queryClient,
      convs(...ids),
      navigate as unknown as Parameters<typeof showArchiveUndoToast>[2],
    ),
  );

beforeEach(() => {
  mocks.undoArchiveConversations.mockReset().mockResolvedValue(undefined);
  resetArchiveUndoBatchForTests();
  navigate = vi.fn();
  toast.dismiss();
});

afterEach(() => cleanup());

describe("showArchiveUndoToast", () => {
  it("uses singular copy for a single session", async () => {
    mountToaster();
    await show(["a"]);

    expect(await screen.findByText("Archived 1 session")).toBeInTheDocument();
    // Never the literal "session(s)".
    expect(screen.queryByText(/session\(s\)/)).not.toBeInTheDocument();
  });

  it("uses plural copy and keeps one toast when archives merge", async () => {
    mountToaster();
    await show(["a"]);
    await show(["b", "c"]);

    // Still a single toast, now covering all three.
    const titles = await screen.findAllByText(/^Archived \d+ sessions?$/);
    expect(titles).toHaveLength(1);
    expect(titles[0]).toHaveTextContent("Archived 3 sessions");
  });

  it("de-dupes ids already in the batch", async () => {
    mountToaster();
    await show(["a"]);
    await show(["a", "b"]);

    expect(await screen.findByText("Archived 2 sessions")).toBeInTheDocument();
  });

  it("undoes the whole merged batch and dismisses the toast", async () => {
    mountToaster();
    await show(["a"]);
    await show(["b", "c"]);

    await screen.findByText("Archived 3 sessions");
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    });

    expect(mocks.undoArchiveConversations).toHaveBeenCalledTimes(1);
    expect(mocks.undoArchiveConversations).toHaveBeenCalledWith(queryClient, convs("a", "b", "c"));
    // The toast dismisses on Undo (sonner animates it out, so wait for removal).
    await waitFor(() => expect(screen.queryByText(/^Archived/)).not.toBeInTheDocument());
  });

  it("starts a fresh batch after an Undo", async () => {
    mountToaster();
    await show(["a", "b"]);
    await screen.findByText("Archived 2 sessions");
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    });
    expect(mocks.undoArchiveConversations).toHaveBeenLastCalledWith(queryClient, convs("a", "b"));
    // Let the dismissed toast finish animating out before reusing the toast id.
    await waitFor(() => expect(screen.queryByText(/^Archived/)).not.toBeInTheDocument());

    // A later archive is its own batch, not appended to the undone one: it reads
    // "1 session" and undoing again restores only the new id.
    await show(["x"]);
    await screen.findByText("Archived 1 session");
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    });
    expect(mocks.undoArchiveConversations).toHaveBeenLastCalledWith(queryClient, convs("x"));
  });

  it("navigates to the archived-sessions settings page via View archived", async () => {
    mountToaster();
    await show(["a"]);

    await screen.findByText("Archived 1 session");
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "View archived" }));
    });
    expect(navigate).toHaveBeenCalledWith("/settings/archived");
  });
});
