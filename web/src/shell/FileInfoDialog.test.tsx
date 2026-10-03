import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

const { copyTextMock } = vi.hoisted(() => ({ copyTextMock: vi.fn(() => Promise.resolve()) }));
vi.mock("@/lib/clipboard", () => ({ copyText: copyTextMock }));

import { FileInfoDialog } from "./FileInfoDialog";
import type { FileRowInfo } from "./FileRowActions";

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
