import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { FileCommandTarget } from "./target";
import { FileCommandMenuAt, type FileRowOpens } from "./useFileCommandMenu";

const TARGET: FileCommandTarget = {
  relativePath: "src/a.ts",
  absolutePath: null,
  conversationId: null,
  hostId: null,
  directory: false,
  deleted: false,
};

afterEach(() => {
  document.body.replaceChildren();
});

/** Waits a frame, as the menu does before it decides where focus goes. */
const nextFrame = () =>
  new Promise<void>((resolve) => {
    requestAnimationFrame(() => resolve());
  });

/** A row with focus that opened its menu, and a field elsewhere a command may focus. */
function openMenu(opens: FileRowOpens = {}) {
  const onRestoreFocus = vi.fn(() => screen.getByRole("button", { name: "src/a.ts" }).focus());
  let openIt = () => {};
  function Harness() {
    const [open, setOpen] = useState(false);
    openIt = () => setOpen(true);
    return (
      <QueryClientProvider client={queryClient}>
        <button type="button">src/a.ts</button>
        <input aria-label="Elsewhere" />
        {open && (
          <FileCommandMenuAt
            target={TARGET}
            point={{ x: 10, y: 20 }}
            onClose={() => setOpen(false)}
            onRestoreFocus={onRestoreFocus}
            opens={opens}
          />
        )}
      </QueryClientProvider>
    );
  }
  const queryClient = new QueryClient();
  render(<Harness />);
  const row = screen.getByRole("button", { name: "src/a.ts" });
  // As from Shift+F10: the row has focus, then its menu opens.
  act(() => row.focus());
  act(() => openIt());
  return { row, onRestoreFocus };
}

describe("FileCommandMenuAt focus", () => {
  it("returns focus to the row on Escape, so arrow keys keep working", async () => {
    const { row, onRestoreFocus } = openMenu({ onOpenInNewTab: () => {} });
    const menu = await screen.findByRole("menu");
    fireEvent.keyDown(menu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(row).toHaveFocus());
    expect(onRestoreFocus).toHaveBeenCalledTimes(1);
  });

  it("leaves focus where a command moved it", async () => {
    const elsewhere = () => screen.getByRole("textbox", { name: "Elsewhere" });
    const { onRestoreFocus } = openMenu({ onOpenInNewTab: () => elsewhere().focus() });
    fireEvent.click(await screen.findByRole("menuitem", { name: /new tab/i }));
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await act(nextFrame);
    expect(elsewhere()).toHaveFocus();
    expect(onRestoreFocus).not.toHaveBeenCalled();
  });

  it("returns focus to the row after a command that leaves it nowhere", async () => {
    const { row } = openMenu({ onOpenInNewTab: () => {} });
    fireEvent.click(await screen.findByRole("menuitem", { name: /new tab/i }));
    await waitFor(() => expect(row).toHaveFocus());
  });

  it("leaves focus where a click outside put it", async () => {
    const { onRestoreFocus } = openMenu({ onOpenInNewTab: () => {} });
    await screen.findByRole("menu");
    const elsewhere = screen.getByRole("textbox", { name: "Elsewhere" });
    fireEvent.pointerDown(elsewhere);
    elsewhere.focus();
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await act(nextFrame);
    expect(elsewhere).toHaveFocus();
    expect(onRestoreFocus).not.toHaveBeenCalled();
  });
});
