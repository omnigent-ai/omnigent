// Removable "@"-mention chips: file vs folder icon and label, full-path title
// behind the truncating label, optional line-range display, and removal by
// list index through the chip's accessibly-named button.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { MentionItem } from "@/lib/composerMentions";
import { ComposerMentionChips } from "./ComposerMentionChips";

afterEach(cleanup);

function renderChips(
  items: MentionItem[],
  opts?: { showLineRange?: boolean; onRemove?: (index: number) => void },
) {
  const onRemove = vi.fn();
  const utils = render(
    <ComposerMentionChips
      items={items}
      onRemove={opts?.onRemove ?? onRemove}
      showLineRange={opts?.showLineRange}
    />,
  );
  return { onRemove, ...utils };
}

describe("ComposerMentionChips", () => {
  it("renders nothing when no paths are tagged", () => {
    const { container } = renderChips([]);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders a file chip with the file icon, @path label, and full-path title", () => {
    const { container } = renderChips([{ path: "src/app.ts", isDir: false }]);
    const label = screen.getByText("@src/app.ts");
    expect(label).toHaveAttribute("title", "src/app.ts");
    expect(container.querySelector("svg.lucide-file-text")).not.toBeNull();
    expect(container.querySelector("svg.lucide-folder")).toBeNull();
  });

  it("renders a directory chip with the folder icon and a trailing slash", () => {
    const { container } = renderChips([{ path: "src", isDir: true }]);
    // The trailing slash marks the chip as a whole-folder attachment, matching
    // the "path/" form delivered in the send-time marker.
    const label = screen.getByText("@src/");
    expect(label).toHaveAttribute("title", "src/");
    expect(container.querySelector("svg.lucide-folder")).not.toBeNull();
    expect(container.querySelector("svg.lucide-file-text")).toBeNull();
  });

  it("keeps the full long path on the title while the label truncates", () => {
    const longPath = `very/deeply/nested/${"segment/".repeat(20)}final.ts`;
    renderChips([{ path: longPath, isDir: false }]);
    const label = screen.getByText(`@${longPath}`);
    expect(label).toHaveClass("truncate");
    expect(label).toHaveAttribute("title", longPath);
  });

  it("shows the line range on a ranged chip when range display is on", () => {
    renderChips([{ path: "docker-compose.yml", isDir: false, lineRange: { start: 2, end: 9 } }], {
      showLineRange: true,
    });
    expect(screen.getByText(":2-9")).toBeInTheDocument();
    // The title still names the exact span behind the truncated label.
    expect(screen.getByText("@docker-compose.yml")).toHaveAttribute(
      "title",
      "docker-compose.yml:2-9",
    );
  });

  it("hides the line range when range display is off", () => {
    renderChips([{ path: "docker-compose.yml", isDir: false, lineRange: { start: 2, end: 9 } }]);
    expect(screen.queryByText(":2-9")).not.toBeInTheDocument();
  });

  it("renders two ranges of the same file as distinct chips in list order", () => {
    renderChips(
      [
        { path: "a.ts", isDir: false, lineRange: { start: 2, end: 9 } },
        { path: "a.ts", isDir: false, lineRange: { start: 20, end: 30 } },
      ],
      { showLineRange: true },
    );
    // Both chips survive reconciliation because each range keys its own chip.
    expect(screen.getAllByText("@a.ts")).toHaveLength(2);
    expect(screen.getByText(":2-9")).toBeInTheDocument();
    expect(screen.getByText(":20-30")).toBeInTheDocument();
  });

  it("removes the clicked chip by its list index via its named button", () => {
    const { onRemove } = renderChips([
      { path: "a.ts", isDir: false },
      { path: "b.ts", isDir: false },
    ]);
    fireEvent.click(screen.getByRole("button", { name: "Remove b.ts" }));
    expect(onRemove).toHaveBeenCalledTimes(1);
    expect(onRemove).toHaveBeenCalledWith(1);
  });

  it("names the remove button after the path without the directory slash", () => {
    renderChips([{ path: "src", isDir: true }]);
    expect(screen.getByRole("button", { name: "Remove src" })).toBeInTheDocument();
  });
});
