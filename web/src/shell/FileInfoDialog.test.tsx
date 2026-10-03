import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { afterEach, expect, it, vi } from "vitest";

const { copyTextMock } = vi.hoisted(() => ({ copyTextMock: vi.fn(() => Promise.resolve()) }));
vi.mock("@/lib/clipboard", () => ({ copyText: copyTextMock }));

import { FileInfoDialog } from "./FileInfoDialog";
import type { FileRowInfo } from "./FileRowActions";

function EscapeDrawer({ info }: { info: FileRowInfo }) {
  const [open, setOpen] = useState(true);
  const [dialogOpen, setDialogOpen] = useState(true);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [returnFocus, setReturnFocus] = useState<HTMLElement | null>(null);

  useLayoutEffect(() => setReturnFocus(triggerRef.current), []);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, []);

  return (
    <div data-testid="drawer" data-state={open ? "open" : "closed"}>
      {open && (
        <>
          <button ref={triggerRef} type="button">
            Open info
          </button>
          <FileInfoDialog
            info={dialogOpen ? info : null}
            onOpenChange={setDialogOpen}
            returnFocus={returnFocus}
          />
        </>
      )}
    </div>
  );
}

function DetachedTriggerHarness() {
  const [info, setInfo] = useState<FileRowInfo | null>({
    name: "notes.txt",
    path: "notes.txt",
    kind: "file",
    bytes: 1,
  });
  const [showTrigger, setShowTrigger] = useState(true);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [returnFocus, setReturnFocus] = useState<HTMLElement | null>(null);

  useLayoutEffect(() => setReturnFocus(triggerRef.current), []);

  return (
    <>
      <button type="button" role="tab" aria-selected="true" tabIndex={0}>
        Files
      </button>
      {showTrigger && (
        <button ref={triggerRef} type="button">
          Open info
        </button>
      )}
      <FileInfoDialog
        info={info}
        onOpenChange={(open) => {
          if (!open) {
            setInfo(null);
            setShowTrigger(false);
          }
        }}
        returnFocus={returnFocus}
      />
    </>
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("shows only available client-side file metadata and copies its canonical path", async () => {
  const user = userEvent.setup();
  const fetchSpy = vi.spyOn(globalThis, "fetch");
  const info: FileRowInfo = {
    name: "same name # Ω.txt",
    path: "deep/same name # Ω.txt",
    kind: "file",
    bytes: null,
    modifiedAt: null,
    status: "deleted",
    linesAdded: null,
    linesRemoved: 3,
    lastKnown: true,
  };
  const onOpenChange = vi.fn();
  render(<FileInfoDialog info={info} onOpenChange={onOpenChange} returnFocus={null} />);

  expect(screen.getByRole("dialog", { name: "File info (last known)" })).toBeInTheDocument();
  expect(screen.getByText("same name # Ω.txt", { selector: "dd" })).toBeInTheDocument();
  expect(screen.getByText("deep/same name # Ω.txt")).toBeInTheDocument();
  expect(screen.getByText("file", { selector: "dd" })).toBeInTheDocument();
  expect(screen.getAllByText("Not available")).toHaveLength(2);
  expect(screen.getByText("Deleted (last known)")).toBeInTheDocument();
  expect(screen.getByText("3", { selector: "dd" })).toBeInTheDocument();
  expect(screen.queryByText(/mode|owner|target|mime|children|recursive/i)).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Copy path: same name # Ω.txt" }));
  expect(copyTextMock).toHaveBeenCalledWith("deep/same name # Ω.txt");
  expect(fetchSpy).not.toHaveBeenCalled();
});

it("uses Not available for unknown folder metadata", () => {
  render(
    <FileInfoDialog
      info={{ name: "empty", path: "/tmp/empty", kind: "folder", modifiedAt: null }}
      onOpenChange={vi.fn()}
      returnFocus={null}
    />,
  );
  expect(screen.getByRole("dialog", { name: "Folder info" })).toBeInTheDocument();
  expect(screen.getAllByText("Not available")).toHaveLength(2);
  expect(screen.queryByText(/size/i)).not.toBeInTheDocument();
});

it("closes Info on Escape without dismissing its containing drawer", async () => {
  const user = userEvent.setup();
  render(<EscapeDrawer info={{ name: "notes.txt", path: "notes.txt", kind: "file", bytes: 1 }} />);

  screen.getByRole("dialog").focus();
  await user.keyboard("{Escape}");

  expect(screen.getByTestId("drawer")).toHaveAttribute("data-state", "open");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Open info" })).toHaveFocus();
});

it("returns focus to the Files tab when the Info opener has been removed", async () => {
  const user = userEvent.setup();
  render(<DetachedTriggerHarness />);

  await user.click(screen.getByRole("button", { name: "Close" }));

  expect(screen.getByRole("tab", { name: "Files" })).toHaveFocus();
});
