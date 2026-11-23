// Picking matches in a filtered Files tree: the selection follows the viewed file, picking
// it opens nothing, and any other match opens, including after a file opened elsewhere.
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";

/** The tree's selection, reporting each change as the vendored model does. */
class FakeModel {
  selected: string[];
  private readonly onChange: (selected: readonly string[]) => void;
  constructor(initial: string[] | undefined, onChange: (selected: readonly string[]) => void) {
    this.selected = initial ?? [];
    this.onChange = onChange;
  }
  getSelectedPaths = () => [...this.selected];
  selectPath = (path: string) => {
    if (this.selected.includes(path)) return;
    this.selected = [...this.selected, path];
    this.onChange(this.selected);
  };
  deselectPath = (path: string) => {
    if (!this.selected.includes(path)) return;
    this.selected = this.selected.filter((selected) => selected !== path);
    this.onChange(this.selected);
  };
  /** A click on a row: it becomes the only selection. */
  click = (path: string) => {
    if (this.selected.length === 1 && this.selected[0] === path) return;
    this.selected = [path];
    this.onChange(this.selected);
  };
}

const tree = vi.hoisted(() => ({ model: null as FakeModel | null }));

vi.mock("@/components/file-tree/vendored", async () => {
  const { useState } = await import("react");
  return {
    useFileTree: (options: {
      initialSelectedPaths?: string[];
      onSelectionChange: (selected: readonly string[]) => void;
    }) => {
      const [model] = useState(
        () => new FakeModel(options.initialSelectedPaths, options.onSelectionChange),
      );
      tree.model = model;
      return { model };
    },
    FileTree: () => <div data-testid="matches-tree" />,
  };
});

import { FilesTreeMatches } from "./FilesTreeMatches";

const file = (path: string): WorkspaceFile => ({
  path,
  name: path.split("/").at(-1)!,
  type: "file",
  bytes: 1,
  modified_at: null,
});
const MATCHES = [file("src/a.ts"), file("src/b.ts")];

function renderMatches(activeFile: string | null, onFileSelect = vi.fn()) {
  const view = render(
    <FilesTreeMatches
      matches={MATCHES}
      activeFile={activeFile}
      onFileSelect={onFileSelect}
      label="Matching workspace files"
      dark={false}
    />,
  );
  const viewing = (path: string | null) =>
    view.rerender(
      <FilesTreeMatches
        matches={MATCHES}
        activeFile={path}
        onFileSelect={onFileSelect}
        label="Matching workspace files"
        dark={false}
      />,
    );
  return { onFileSelect, viewing };
}

afterEach(() => {
  cleanup();
  tree.model = null;
});

describe("FilesTreeMatches selection", () => {
  it("starts on the viewed file, and opens another match, then the first again", () => {
    const { onFileSelect, viewing } = renderMatches("src/a.ts");
    expect(tree.model?.selected).toEqual(["src/a.ts"]);

    act(() => tree.model?.click("src/b.ts"));
    viewing("src/b.ts");
    act(() => tree.model?.click("src/a.ts"));
    expect(onFileSelect.mock.calls).toEqual([["src/b.ts"], ["src/a.ts"]]);
    // A folder opens nothing.
    act(() => tree.model?.click("src/"));
    expect(onFileSelect).toHaveBeenCalledTimes(2);
  });

  it("follows a file opened elsewhere, so the one viewed before opens from the tree", () => {
    const { onFileSelect, viewing } = renderMatches("src/a.ts");

    // The breadcrumbs open b.ts: the tree selects it, and that opens nothing.
    viewing("src/b.ts");
    expect(tree.model?.selected).toEqual(["src/b.ts"]);
    expect(onFileSelect).not.toHaveBeenCalled();
    act(() => tree.model?.click("src/a.ts"));
    expect(onFileSelect.mock.calls).toEqual([["src/a.ts"]]);

    // A viewed file outside the matches leaves nothing selected.
    viewing("README.md");
    expect(tree.model?.selected).toEqual([]);
    act(() => tree.model?.click("src/b.ts"));
    expect(onFileSelect.mock.calls).toEqual([["src/a.ts"], ["src/b.ts"]]);
  });
});
