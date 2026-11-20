import { act, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { FILE_TREE_ITEM_HEIGHT, FileTree, type FileTreeModel, useFileTree } from ".";
import type { FileTreeOptions } from "./types";

beforeAll(() => {
  (
    globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }
  ).IS_REACT_ACT_ENVIRONMENT = true;
});

let root: Root | undefined;
let host: HTMLElement | undefined;

afterEach(async () => {
  await act(async () => root?.unmount());
  host?.remove();
  root = undefined;
  host = undefined;
});

async function renderTree(
  options: FileTreeOptions,
): Promise<{ model: FileTreeModel; tree: HTMLElement }> {
  host = document.body.appendChild(document.createElement("div"));
  root = createRoot(host);
  let model: FileTreeModel | undefined;
  function Harness() {
    model = useFileTree(options).model;
    return <FileTree model={model} aria-label="Files" />;
  }
  await act(async () => root!.render(<Harness />));
  return { model: model!, tree: host.querySelector<HTMLElement>("[data-file-tree]")! };
}

function row(tree: HTMLElement, path: string): HTMLButtonElement | null {
  return tree.querySelector<HTMLButtonElement>(`[data-item-path="${path}"]`);
}

function renderedPaths(tree: HTMLElement): string[] {
  return [...tree.querySelectorAll<HTMLElement>("[role=treeitem]")].map(
    (element) => element.dataset.itemPath!,
  );
}

async function press(target: Element, key: string, init: KeyboardEventInit = {}) {
  await act(async () => {
    target.dispatchEvent(
      new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init }),
    );
  });
}

async function click(target: HTMLElement, init: MouseEventInit = {}) {
  await act(async () => {
    target.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, ...init }));
  });
}

const PATHS = ["src/app.ts", "src/lib/util.ts", "README.md", "package.json"];

describe("FileTree", () => {
  it("renders rows as tree items with ARIA state", async () => {
    const { tree } = await renderTree({ paths: PATHS, initialSelectedPaths: ["README.md"] });

    expect(tree.querySelector("[role=tree]")?.getAttribute("aria-label")).toBe("Files");
    expect(renderedPaths(tree)).toEqual(["src/", "package.json", "README.md"]);
    expect(row(tree, "src/")?.getAttribute("aria-expanded")).toBe("false");
    expect(row(tree, "README.md")?.getAttribute("aria-selected")).toBe("true");
    expect(row(tree, "README.md")?.getAttribute("aria-level")).toBe("1");
    // Only the focused row is a Tab stop.
    expect([...tree.querySelectorAll("[tabindex='0']")]).toEqual([row(tree, "README.md")]);
  });

  it("opens directories and selects files on click", async () => {
    const onSelectionChange = vi.fn();
    const { tree } = await renderTree({ paths: PATHS, onSelectionChange });

    await click(row(tree, "src/")!);
    expect(row(tree, "src/")?.getAttribute("aria-expanded")).toBe("true");
    expect(renderedPaths(tree)).toEqual([
      "src/",
      "src/lib/",
      "src/app.ts",
      "package.json",
      "README.md",
    ]);

    await click(row(tree, "src/app.ts")!);
    expect(onSelectionChange).toHaveBeenLastCalledWith(["src/app.ts"]);
    expect(document.activeElement).toBe(row(tree, "src/app.ts"));
  });

  it("extends and toggles the selection with modifier clicks", async () => {
    const { model, tree } = await renderTree({ paths: PATHS, initialSelectedPaths: ["src/"] });

    await click(row(tree, "README.md")!, { shiftKey: true });
    expect(model.getSelectedPaths()).toEqual(["src/", "package.json", "README.md"]);
    expect(row(tree, "src/")?.getAttribute("aria-expanded")).toBe("false");

    await click(row(tree, "package.json")!, { metaKey: true });
    expect(model.getSelectedPaths()).toEqual(["src/", "README.md"]);
  });

  it("moves DOM focus with the arrow keys and opens directories with Right", async () => {
    const { tree } = await renderTree({ paths: PATHS });
    act(() => row(tree, "src/")!.focus());

    await press(document.activeElement!, "ArrowRight");
    expect(row(tree, "src/")?.getAttribute("aria-expanded")).toBe("true");
    await press(document.activeElement!, "ArrowRight");
    expect(document.activeElement).toBe(row(tree, "src/lib/"));
    await press(document.activeElement!, "ArrowDown");
    expect(document.activeElement).toBe(row(tree, "src/app.ts"));
    await press(document.activeElement!, "ArrowLeft");
    expect(document.activeElement).toBe(row(tree, "src/"));
    await press(document.activeElement!, "ArrowLeft");
    expect(row(tree, "src/")?.getAttribute("aria-expanded")).toBe("false");
    await press(document.activeElement!, "End");
    expect(document.activeElement).toBe(row(tree, "README.md"));
    await press(document.activeElement!, "Home");
    expect(document.activeElement).toBe(row(tree, "src/"));
  });

  it("leaves unhandled keys to the page", async () => {
    const { tree } = await renderTree({ paths: PATHS });
    const event = new KeyboardEvent("keydown", {
      key: "f",
      metaKey: true,
      bubbles: true,
      cancelable: true,
    });

    act(() => {
      row(tree, "src/")!.dispatchEvent(event);
    });

    expect(event.defaultPrevented).toBe(false);
  });

  it("does not take focus when the host moves the selection", async () => {
    const outside = document.body.appendChild(document.createElement("input"));
    const { model, tree } = await renderTree({ paths: PATHS });
    outside.focus();

    await act(async () => model.getItem("README.md")?.select());
    await act(async () => model.scrollToPath("README.md", { focus: false }));

    expect(row(tree, "README.md")?.getAttribute("aria-selected")).toBe("true");
    expect(document.activeElement).toBe(outside);
    outside.remove();
  });

  it("keeps focus in the host's search field when a row is clicked while searching", async () => {
    const search = document.body.appendChild(document.createElement("input"));
    const { model, tree } = await renderTree({ paths: PATHS });
    search.focus();
    await act(async () => model.setSearch("util"));

    const mouseDown = new MouseEvent("mousedown", { bubbles: true, cancelable: true });
    act(() => {
      row(tree, "src/lib/util.ts")!.dispatchEvent(mouseDown);
    });
    await click(row(tree, "src/lib/util.ts")!);

    expect(mouseDown.defaultPrevented).toBe(true);
    expect(model.getSelectedPaths()).toEqual(["src/lib/util.ts"]);
    expect(document.activeElement).toBe(search);
    search.remove();
  });

  it("shows git status letters and marks directories that contain changes", async () => {
    const { tree } = await renderTree({
      paths: PATHS,
      initialExpansion: "open",
      gitStatus: [
        { path: "src/lib/util.ts", status: "modified" },
        { path: "README.md", status: "added" },
      ],
    });

    expect(row(tree, "src/lib/util.ts")?.dataset.itemGitStatus).toBe("modified");
    expect(
      row(tree, "src/lib/util.ts")?.querySelector("[data-item-section=git]")?.textContent,
    ).toBe("M");
    expect(row(tree, "README.md")?.querySelector("[title=Added]")?.textContent).toBe("A");
    expect(row(tree, "src/")?.hasAttribute("data-item-contains-git-change")).toBe(true);
    expect(row(tree, "package.json")?.dataset.itemGitStatus).toBeUndefined();
  });

  it("highlights the indent guide of the focused row's parent", async () => {
    const { tree } = await renderTree({
      paths: PATHS,
      initialExpansion: "open",
      initialSelectedPaths: ["src/lib/util.ts"],
    });
    const activeGuides = () =>
      [...tree.querySelectorAll("[data-active-guide]")].map(
        (guide) => guide.closest<HTMLElement>("[role=treeitem]")!.dataset.itemPath,
      );

    expect(activeGuides()).toEqual(["src/lib/util.ts"]);
    await press(row(tree, "src/lib/util.ts")!, "ArrowDown");
    expect(activeGuides()).toEqual(["src/lib/", "src/lib/util.ts", "src/app.ts"]);
  });

  it("omits the git lane when there is no git status", async () => {
    const { tree } = await renderTree({ paths: PATHS });

    expect(tree.querySelector("[data-item-section=git]")).toBeNull();
  });

  it("renders flattened directory chains as one row that acts on the deepest directory", async () => {
    const { tree } = await renderTree({ paths: ["a/b/c/file.ts"], flattenEmptyDirectories: true });
    const chain = row(tree, "a/b/c/")!;

    expect(chain.getAttribute("aria-label")).toBe("a / b / c");
    expect(chain.textContent).toBe("a / b / c");
    await click(chain);
    expect(row(tree, "a/b/c/file.ts")).toBeTruthy();
  });

  it("shows paths added after a StrictMode remount", async () => {
    host = document.body.appendChild(document.createElement("div"));
    root = createRoot(host);
    let model: FileTreeModel | undefined;
    function Harness() {
      model = useFileTree({ paths: ["lib/", "README.md"], initialExpandedPaths: ["lib/"] }).model;
      return <FileTree model={model} />;
    }
    await act(async () =>
      root!.render(
        <StrictMode>
          <Harness />
        </StrictMode>,
      ),
    );

    await act(async () => model!.addPaths(["lib/a.ts"]));

    expect(renderedPaths(host.querySelector("[data-file-tree]")!)).toEqual([
      "lib/",
      "lib/a.ts",
      "README.md",
    ]);
  });

  describe("virtualization", () => {
    const manyPaths = Array.from(
      { length: 1000 },
      (_, index) => `file-${String(index).padStart(4, "0")}.ts`,
    );

    it("renders only the rows near the viewport", async () => {
      const { tree } = await renderTree({ paths: manyPaths });
      const scroller = tree.querySelector<HTMLElement>("[data-file-tree-scroll]")!;

      expect(renderedPaths(tree).length).toBeLessThan(50);
      expect(tree.querySelector<HTMLElement>("[data-file-tree-list]")?.style.height).toBe(
        "30000px",
      );

      scroller.scrollTop = 15_000;
      await act(async () => scroller.dispatchEvent(new Event("scroll")));
      expect(renderedPaths(tree)).toContain("file-0500.ts");
      expect(renderedPaths(tree)).not.toContain("file-0001.ts");
    });

    it("keeps the focused row mounted after it scrolls out of view", async () => {
      const { tree } = await renderTree({ paths: manyPaths });
      const scroller = tree.querySelector<HTMLElement>("[data-file-tree-scroll]")!;
      act(() => row(tree, "file-0000.ts")!.focus());

      scroller.scrollTop = 15_000;
      await act(async () => scroller.dispatchEvent(new Event("scroll")));

      expect(document.activeElement).toBe(row(tree, "file-0000.ts"));
      await press(document.activeElement!, "ArrowDown");
      expect(document.activeElement).toBe(row(tree, "file-0001.ts"));
      expect(scroller.scrollTop).toBe(30);
    });

    it("scrolls to a requested row", async () => {
      const { model, tree } = await renderTree({ paths: manyPaths });
      const scroller = tree.querySelector<HTMLElement>("[data-file-tree-scroll]")!;

      await act(async () => model.scrollToPath("file-0600.ts", { offset: "top" }));

      expect(scroller.scrollTop).toBe(18_000);
      expect(row(tree, "file-0600.ts")).toBeTruthy();
    });

    it("finishes a centered reveal the browser snapped to a whole pixel, so a resize or scroll keeps the reader's place", async () => {
      // 421px centers row 600 at 17,804.5, which the browser snaps to 17,805.
      const element = stubScrollElement({ height: 421, snap: true });
      try {
        const { model, tree } = await renderTree({ paths: manyPaths });
        await act(async () => model.scrollToPath("file-0600.ts", { offset: "center" }));
        expect(element.scrollTop()).toBe(17_805);

        // A later resize doesn't center row 600 again.
        await element.show(300);
        expect(element.scrollTop()).toBe(17_805);

        await element.userScrollsTo(tree, 0);
        await element.show(421);
        expect(element.scrollTop()).toBe(0);
      } finally {
        element.restore();
      }
    });

    it("drops a reveal still waiting once the reader scrolls the shown tree", async () => {
      // The tree has a size but hasn't taken the write yet, so the reveal waits.
      const element = stubScrollElement({ height: 420 });
      element.ignoreWrites(true);
      try {
        const { model, tree } = await renderTree({ paths: manyPaths });
        await act(async () => model.scrollToPath("file-0600.ts", { offset: "top" }));
        expect(element.scrollTop()).toBe(0);

        await element.userScrollsTo(tree, 600);
        element.ignoreWrites(false);
        await element.show(420);
        expect(element.scrollTop()).toBe(600);
      } finally {
        element.restore();
      }
    });

    /**
     * Stubs the tree's scroll element: `height` tall (zero while hidden, which ignores scrollTop
     * writes), snapping writes to whole pixels when `snap` is set, as browsers do. `show`
     * resizes it and tells its resize observers. Undo with `restore`.
     */
    function stubScrollElement({ height: initialHeight = 0, snap = false } = {}) {
      let height = initialHeight;
      let applied = 0;
      let acceptsWrites = true;
      const resized = new Set<ResizeObserverCallback>();
      vi.stubGlobal(
        "ResizeObserver",
        class {
          private readonly callback: ResizeObserverCallback;
          constructor(callback: ResizeObserverCallback) {
            this.callback = callback;
          }
          observe() {
            resized.add(this.callback);
          }
          unobserve() {}
          disconnect() {
            resized.delete(this.callback);
          }
        },
      );
      const rect = vi
        .spyOn(HTMLElement.prototype, "getBoundingClientRect")
        .mockImplementation(() => ({ height, width: 200 }) as DOMRect);
      Object.defineProperty(HTMLElement.prototype, "scrollTop", {
        configurable: true,
        get: () => applied,
        set: (value: number) => {
          if (height > 0 && acceptsWrites) applied = snap ? Math.round(value) : value;
        },
      });
      return {
        scrollTop: () => applied,
        /** Whether row `index` is inside the shown viewport. */
        showsRow: (index: number) =>
          index * FILE_TREE_ITEM_HEIGHT >= applied &&
          (index + 1) * FILE_TREE_ITEM_HEIGHT <= applied + height,
        /** Makes scrollTop writes no-ops while the element keeps its size. */
        ignoreWrites: (ignored: boolean) => {
          acceptsWrites = !ignored;
        },
        show: async (shownHeight: number) => {
          height = shownHeight;
          await act(async () => {
            for (const callback of [...resized]) callback([], {} as ResizeObserver);
          });
        },
        /** Scrolls as the reader would, with the scroll event that follows. */
        userScrollsTo: async (tree: HTMLElement, top: number) => {
          applied = top;
          const scroller = tree.querySelector<HTMLElement>("[data-file-tree-scroll]")!;
          await act(async () => scroller.dispatchEvent(new Event("scroll")));
        },
        restore: () => {
          rect.mockRestore();
          vi.unstubAllGlobals();
          delete (HTMLElement.prototype as { scrollTop?: number }).scrollTop;
        },
      };
    }

    it("reveals a row it was asked to while hidden once it's shown", async () => {
      const hidden = stubScrollElement();
      try {
        const { tree } = await renderTree({
          paths: manyPaths,
          initialSelectedPaths: ["file-0900.ts"],
        });
        // The hidden element still shows its top rows, so the tree draws them rather than a gap.
        expect(renderedPaths(tree).slice(0, 3)).toEqual([
          "file-0000.ts",
          "file-0001.ts",
          "file-0002.ts",
        ]);

        await hidden.show(420);

        expect(hidden.showsRow(900)).toBe(true);
        expect(row(tree, "file-0900.ts")?.getAttribute("aria-selected")).toBe("true");
        expect(renderedPaths(tree)).not.toContain("file-0000.ts");
      } finally {
        hidden.restore();
      }
    });

    it("reveals the row where it is when shown, after the hidden tree's listing changes", async () => {
      const hidden = stubScrollElement();
      try {
        const { model, tree } = await renderTree({
          paths: manyPaths,
          initialSelectedPaths: ["file-0900.ts"],
        });
        // A refresh while hidden moves the selected file from row 900 to row 100.
        await act(async () => model.resetPaths(manyPaths.slice(800)));

        await hidden.show(420);

        expect(hidden.showsRow(100)).toBe(true);
        expect(row(tree, "file-0900.ts")?.getAttribute("aria-selected")).toBe("true");
      } finally {
        hidden.restore();
      }
    });

    it("reveals a row once shown in a viewport shorter than the hidden tree assumed", async () => {
      const hidden = stubScrollElement();
      try {
        // Row 10 sits inside the 420px a hidden tree assumes, but below a 150px viewport.
        await renderTree({ paths: manyPaths, initialSelectedPaths: ["file-0010.ts"] });

        await hidden.show(150);

        expect(hidden.scrollTop()).toBeGreaterThan(0);
        expect(hidden.showsRow(10)).toBe(true);
      } finally {
        hidden.restore();
      }
    });

    it("reveals the initially selected row", async () => {
      const { tree } = await renderTree({
        paths: manyPaths,
        initialSelectedPaths: ["file-0900.ts"],
      });

      expect(row(tree, "file-0900.ts")?.getAttribute("aria-selected")).toBe("true");
      expect(tree.querySelector<HTMLElement>("[data-file-tree-scroll]")?.scrollTop).toBeGreaterThan(
        0,
      );
    });
  });

  it("mounts one shared icon sprite sheet while any tree is mounted", async () => {
    const first = await renderTree({ paths: PATHS });
    const secondHost = document.body.appendChild(document.createElement("div"));
    const secondRoot = createRoot(secondHost);
    function Second() {
      const { model } = useFileTree({ paths: PATHS });
      return <FileTree model={model} />;
    }
    await act(async () => secondRoot.render(<Second />));

    expect(document.querySelectorAll("#file-tree-icon-chevron")).toHaveLength(1);
    expect(first.tree.querySelector('[data-item-path="README.md"] use')?.getAttribute("href")).toBe(
      "#file-tree-builtin-markdown",
    );

    await act(async () => secondRoot.unmount());
    secondHost.remove();
    expect(document.querySelectorAll("#file-tree-icon-chevron")).toHaveLength(1);
    await act(async () => root!.unmount());
    root = undefined;
    expect(document.querySelector("#file-tree-icon-chevron")).toBeNull();
  });
});
