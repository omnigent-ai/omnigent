import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { FileTree, type FileTreeProps, useFileTree } from ".";
import { splitNameForTruncation } from "./MiddleTruncate";
import type { FileTreeModel } from "./model";
import type { FileTreeOptions } from "./types";

function renderTree(options: FileTreeOptions, props: Omit<FileTreeProps, "model"> = {}) {
  let model: FileTreeModel | undefined;
  function Harness() {
    model = useFileTree(options).model;
    return <FileTree model={model} aria-label="Files" {...props} />;
  }
  render(<Harness />);
  return model!;
}

const row = (path: string) =>
  document.querySelector<HTMLElement>(`[role="treeitem"][data-item-path="${path}"]`)!;

describe("row decorations", () => {
  it("shows each part in its color with the title, and repaints with the model", () => {
    const counts = new Map([["a.ts", 3]]);
    const model = renderTree(
      { paths: ["a.ts", "b.ts"] },
      {
        renderRowDecoration: ({ path }) => {
          const added = counts.get(path);
          return added
            ? {
                text: `+${added}`,
                title: `${added} added`,
                parts: [{ text: `+${added}`, color: "green" }],
              }
            : null;
        },
      },
    );
    const decoration = row("a.ts").querySelector('[data-item-section="decoration"]')!;
    expect(decoration.textContent).toBe("+3");
    expect(decoration.querySelector("[title]")?.getAttribute("title")).toBe("3 added");
    expect(decoration.querySelector<HTMLElement>("[title] > span")?.style.color).toBe("green");
    expect(row("b.ts").querySelector('[data-item-section="decoration"]')).toBeNull();

    counts.set("b.ts", 1);
    act(() => model.setGitStatus([]));
    expect(row("b.ts").querySelector('[data-item-section="decoration"]')?.textContent).toBe("+1");
  });
});

describe("row menu", () => {
  it("opens from a right-click at the pointer, focusing the row", () => {
    const onRowMenu = vi.fn();
    const model = renderTree({ paths: ["a.ts", "b.ts"] }, { onRowMenu });
    fireEvent.contextMenu(row("b.ts"), { clientX: 40, clientY: 60 });
    expect(onRowMenu).toHaveBeenCalledTimes(1);
    const [menuRow, anchor] = onRowMenu.mock.calls[0];
    expect(menuRow.path).toBe("b.ts");
    expect([anchor.x, anchor.y]).toEqual([40, 60]);
    expect(model.getFocusedPath()).toBe("b.ts");
    expect(document.activeElement).toBe(row("b.ts"));
  });

  it("opens for the focused row from Shift+F10 and the context menu key", () => {
    const onRowMenu = vi.fn();
    renderTree({ paths: ["a.ts", "b.ts"] }, { onRowMenu });
    act(() => row("a.ts").focus());
    fireEvent.keyDown(row("a.ts"), { key: "F10", shiftKey: true });
    fireEvent.keyDown(row("a.ts"), { key: "ContextMenu" });
    expect(onRowMenu.mock.calls.map(([menuRow]) => menuRow.path)).toEqual(["a.ts", "a.ts"]);
  });

  it("shows one actions button on the hovered row, which opens its menu without taking focus", () => {
    const onRowMenu = vi.fn();
    renderTree({ paths: ["a.ts", "b.ts"] }, { onRowMenu });
    expect(screen.queryByRole("button", { name: /^Actions for/ })).toBeNull();
    fireEvent.pointerMove(row("b.ts"));
    const trigger = screen.getByRole("button", { name: "Actions for b.ts" });
    expect(trigger.getAttribute("aria-haspopup")).toBe("menu");
    expect(fireEvent.mouseDown(trigger)).toBe(false);
    fireEvent.click(trigger);
    expect(onRowMenu.mock.calls[0][0].path).toBe("b.ts");
    fireEvent.pointerLeave(document.querySelector("[data-file-tree]")!);
    expect(screen.queryByRole("button", { name: /^Actions for/ })).toBeNull();
  });

  it("follows the focused row while the tree has focus", () => {
    renderTree({ paths: ["a.ts", "b.ts"] }, { onRowMenu: vi.fn() });
    act(() => row("a.ts").focus());
    expect(screen.getByRole("button", { name: "Actions for a.ts" })).toBeTruthy();
  });

  it("does nothing on right-click without a menu", () => {
    renderTree({ paths: ["a.ts"] });
    expect(fireEvent.contextMenu(row("a.ts"))).toBe(true);
    expect(document.querySelector('[data-type="context-menu-trigger"]')).toBeNull();
  });
});

describe("middle truncation", () => {
  it("splits before the extension, or in the middle without a short one", () => {
    expect(splitNameForTruncation("component.test.tsx")).toEqual(["component.test.", "tsx"]);
    expect(splitNameForTruncation("Makefile")).toEqual(["Make", "file"]);
    expect(splitNameForTruncation("archive.verylongextension")).toEqual([
      "archive.veryl",
      "ongextension",
    ]);
    expect(splitNameForTruncation("a.ts")).toEqual(["a.ts", ""]);
  });

  it("keeps the seam off a space, so it doesn't collapse", () => {
    const [start, end] = splitNameForTruncation("Hello world");
    expect(start.endsWith(" ") || end.startsWith(" ")).toBe(false);
    expect(start + end).toBe("Hello world");
  });

  it("renders a long name as a start and an end that each clip", () => {
    renderTree({ paths: ["component.test.tsx", "a.ts"] });
    const content = row("component.test.tsx").querySelector('[data-item-section="content"]')!;
    const parts = [...content.querySelectorAll('[data-truncate-content="visible"]')];
    expect(parts.map((part) => part.textContent)).toEqual(["component.test.", "tsx"]);
    expect(content.textContent).toBe("component.test.tsx");
    expect(row("a.ts").querySelectorAll('[data-truncate-content="visible"]')).toHaveLength(1);
  });
});
