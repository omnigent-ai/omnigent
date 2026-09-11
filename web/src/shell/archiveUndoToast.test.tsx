// Tests for the post-archive Undo pill. Contract: archives in quick succession
// merge into ONE pill whose count updates live, and Undo unarchives the whole
// merged batch through undoArchiveConversations. The batch is module state, so
// it's reset between cases and any leftover toast is dismissed.

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

/** Minimal Conversation rows keyed by id — the pill only needs id + count. */
const conv = (id: string): Conversation =>
  ({ id, object: "conversation", title: id, created_at: 0, updated_at: 0 }) as Conversation;

const convs = (...ids: string[]) => ids.map(conv);

function mountToaster() {
  render(
    <MemoryRouter>
      <Toaster />
    </MemoryRouter>,
  );
}

const show = (ids: string[]) => act(() => showArchiveUndoToast(queryClient, convs(...ids)));

beforeEach(() => {
  mocks.undoArchiveConversations.mockReset().mockResolvedValue(undefined);
  resetArchiveUndoBatchForTests();
  toast.dismiss();
});

afterEach(() => cleanup());

describe("showArchiveUndoToast", () => {
  it("uses singular copy for a single session", async () => {
    mountToaster();
    await show(["a"]);

    const pill = await screen.findByTestId("archive-undo-toast");
    expect(pill).toHaveTextContent("Archived 1 session.");
    // Never the literal "session(s)".
    expect(pill.textContent).not.toContain("session(s)");
  });

  it("uses plural copy and keeps one pill when archives merge", async () => {
    mountToaster();
    await show(["a"]);
    await show(["b", "c"]);

    // Still a single pill, now covering all three.
    const pills = await screen.findAllByTestId("archive-undo-toast");
    expect(pills).toHaveLength(1);
    expect(pills[0]).toHaveTextContent("Archived 3 sessions.");
  });

  it("de-dupes ids already in the batch", async () => {
    mountToaster();
    await show(["a"]);
    await show(["a", "b"]);

    expect(await screen.findByTestId("archive-undo-toast")).toHaveTextContent(
      "Archived 2 sessions.",
    );
  });

  it("undoes the whole merged batch and dismisses the pill", async () => {
    mountToaster();
    await show(["a"]);
    await show(["b", "c"]);

    const pill = await screen.findByTestId("archive-undo-toast");
    act(() => {
      fireEvent.click(within(pill).getByTestId("archive-undo-button"));
    });

    expect(mocks.undoArchiveConversations).toHaveBeenCalledTimes(1);
    expect(mocks.undoArchiveConversations).toHaveBeenCalledWith(queryClient, convs("a", "b", "c"));
    // The pill dismisses on Undo (sonner animates it out, so wait for removal).
    await waitFor(() => expect(screen.queryByTestId("archive-undo-toast")).not.toBeInTheDocument());
  });

  it("starts a fresh batch after an Undo", async () => {
    mountToaster();
    await show(["a", "b"]);
    const first = await screen.findByTestId("archive-undo-toast");
    act(() => {
      fireEvent.click(within(first).getByTestId("archive-undo-button"));
    });
    expect(mocks.undoArchiveConversations).toHaveBeenLastCalledWith(queryClient, convs("a", "b"));
    // Let the dismissed pill finish animating out before reusing the toast id.
    await waitFor(() => expect(screen.queryByTestId("archive-undo-toast")).not.toBeInTheDocument());

    // A later archive is its own batch, not appended to the undone one: it reads
    // "1 session" and undoing again restores only the new id.
    await show(["x"]);
    const second = await screen.findByTestId("archive-undo-toast");
    expect(second).toHaveTextContent("Archived 1 session.");
    act(() => {
      fireEvent.click(within(second).getByTestId("archive-undo-button"));
    });
    expect(mocks.undoArchiveConversations).toHaveBeenLastCalledWith(queryClient, convs("x"));
  });

  it("links to the archived-sessions settings page", async () => {
    mountToaster();
    await show(["a"]);

    const pill = await screen.findByTestId("archive-undo-toast");
    expect(within(pill).getByRole("link", { name: "View in Settings" })).toHaveAttribute(
      "href",
      "/settings/archived",
    );
  });
});
