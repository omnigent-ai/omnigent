// Tests for the post-unpin Undo toast. Contract: the pill appears only once the
// unpin write lands, Undo re-pins at the session's previous pin value (its old
// Pinned slot), and a newer unpin replaces the pill instead of stacking.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { Toaster } from "@/components/ui/sonner";
import { PINNED_LABEL_KEY } from "@/lib/sessionListCache";
import { unpinWithUndo } from "./unpinUndoToast";

const writePin = vi.fn();

const row = (title: string, pin: string) => ({ title, labels: { [PINNED_LABEL_KEY]: pin } });

beforeEach(() => {
  writePin.mockReset().mockResolvedValue({});
  toast.dismiss();
  render(<Toaster />);
});

afterEach(() => {
  toast.dismiss();
  cleanup();
});

describe("unpinWithUndo", () => {
  it("unpins, then offers an Undo that re-pins at the previous pin value", async () => {
    act(() => unpinWithUndo(writePin, "conv_a", row("Release notes", "1700000000123")));

    expect(writePin).toHaveBeenCalledWith({ id: "conv_a", pinned: false });
    expect(await screen.findByText("Unpinned session")).toBeInTheDocument();
    expect(screen.getByText("Release notes")).toBeInTheDocument();

    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    });
    expect(writePin).toHaveBeenLastCalledWith({
      id: "conv_a",
      pinned: true,
      pinnedAt: 1700000000123,
    });
    await waitFor(() => expect(screen.queryByText("Unpinned session")).not.toBeInTheDocument());
  });

  it("offers nothing when the unpin is refused or fails", async () => {
    writePin.mockRejectedValueOnce(new Error("Still saving your pins."));
    act(() => unpinWithUndo(writePin, "conv_a", row("Release notes", "1700000000123")));

    // Let the rejected write settle; no pill should follow it.
    await act(async () => {});
    expect(screen.queryByText("Unpinned session")).not.toBeInTheDocument();
  });

  it("re-pins at the current time when the previous pin value is unknown", async () => {
    // A legacy localStorage pin has no server row, so there's no value to restore.
    act(() => unpinWithUndo(writePin, "conv_a", undefined));

    await screen.findByText("Unpinned session");
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    });
    expect(writePin).toHaveBeenLastCalledWith({ id: "conv_a", pinned: true, pinnedAt: undefined });
  });

  it("replaces the pill on a newer unpin, and Undo restores only that session", async () => {
    act(() => unpinWithUndo(writePin, "conv_a", row("First", "1")));
    await screen.findByText("First");
    act(() => unpinWithUndo(writePin, "conv_b", row("Second", "2")));
    await screen.findByText("Second");

    expect(screen.getAllByText("Unpinned session")).toHaveLength(1);
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    });
    expect(writePin).toHaveBeenLastCalledWith({ id: "conv_b", pinned: true, pinnedAt: 2 });
    expect(writePin).not.toHaveBeenCalledWith(
      expect.objectContaining({ id: "conv_a", pinned: true }),
    );
  });
});
