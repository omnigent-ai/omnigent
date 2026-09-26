import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { HostFilesystemEntry } from "@/hooks/useHostFilesystem";
import { WorkspacePickerEntry } from "./WorkspacePickerEntry";

const folder: HostFilesystemEntry = {
  name: "projects",
  path: "/Users/ajay/projects",
  type: "directory",
  bytes: null,
  modified_at: 0,
};

const file: HostFilesystemEntry = {
  name: "README.md",
  path: "/Users/ajay/README.md",
  type: "file",
  bytes: 1024,
  modified_at: 0,
};

describe("WorkspacePickerEntry", () => {
  it("opens directory entries", () => {
    const onOpen = vi.fn();
    render(<WorkspacePickerEntry entry={folder} onOpen={onOpen} />);

    fireEvent.click(screen.getByRole("button", { name: "projects" }));

    expect(onOpen).toHaveBeenCalledWith(folder.path);
  });

  it("renders files as disabled entries", () => {
    const onOpen = vi.fn();
    render(<WorkspacePickerEntry entry={file} onOpen={onOpen} />);

    const row = screen.getByRole("button", { name: "README.md" });
    expect(row).toBeDisabled();
    fireEvent.click(row);
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("supports default and compact dimensions", () => {
    const { rerender } = render(<WorkspacePickerEntry entry={folder} onOpen={() => undefined} />);
    const row = screen.getByRole("button", { name: "projects" });
    expect(row).toHaveClass("min-h-11", "px-5", "py-2");

    rerender(<WorkspacePickerEntry entry={folder} onOpen={() => undefined} variant="compact" />);
    expect(row).toHaveClass("h-7", "gap-2", "rounded-md", "px-2", "py-[3px]", "text-ui");
  });

  it("matches the compact modal file-row treatment", () => {
    render(<WorkspacePickerEntry entry={file} onOpen={() => undefined} variant="compact" />);

    const row = screen.getByRole("button", { name: "README.md" });
    expect(row).toHaveClass("cursor-default", "text-muted-foreground");
    expect(row).not.toHaveClass("cursor-not-allowed", "opacity-55", "hover:bg-muted");
    const icon = row.querySelector('[data-file-type="document"]');
    expect(icon).toHaveAttribute("data-tag-color", "blue");
    expect(icon).toHaveClass("size-4", "text-blue-500", "dark:text-blue-400");
    expect(screen.getByText("README.md")).toHaveClass("flex-1", "truncate");
    expect(row.querySelector('[data-lucide="chevron-right"]')).toBeNull();
  });
});
