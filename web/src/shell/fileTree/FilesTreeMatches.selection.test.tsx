// Picking matches in a filtered Files tree, on the real tree: a file opens from the input
// that chose it, as in the full tree, and the selection follows the viewed file.
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import { isMacPlatform } from "@/lib/hotkeys";

import { FilesTreeMatches } from "./FilesTreeMatches";

const file = (path: string): WorkspaceFile => ({
  path,
  name: path.split("/").at(-1)!,
  type: "file",
  bytes: 1,
  modified_at: null,
});
const MATCHES = [file("src/a.ts"), file("src/b.ts"), file("src/c.ts")];

function renderMatches(activeFile: string | null) {
  const onFileSelect = vi.fn();
  const matches = (viewed: string | null) => (
    <FilesTreeMatches
      matches={MATCHES}
      activeFile={viewed}
      onFileSelect={onFileSelect}
      label="Matching workspace files"
      dark={false}
    />
  );
  const view = render(matches(activeFile));
  return { onFileSelect, viewing: (path: string | null) => view.rerender(matches(path)) };
}

const row = (name: string) =>
  screen.findByRole("treeitem", { name: new RegExp(`^${name.replace(".", "\\.")}`) });
const selected = () =>
  screen
    .queryAllByRole("treeitem")
    .filter((item) => item.getAttribute("aria-selected") === "true")
    .map((item) => item.getAttribute("data-item-path"));

afterEach(cleanup);

describe("FilesTreeMatches selection", () => {
  it("starts on the viewed file and opens the matches a person clicks, not their folders", async () => {
    const { onFileSelect } = renderMatches("src/a.ts");
    await row("b.ts");
    expect(selected()).toEqual(["src/a.ts"]);
    expect(onFileSelect).not.toHaveBeenCalled();

    fireEvent.click(await row("b.ts"));
    fireEvent.click(await row("a.ts"));
    // A click on a folder only closes it.
    fireEvent.click(await row("src"));

    expect(onFileSelect.mock.calls).toEqual([["src/b.ts"], ["src/a.ts"]]);
  });

  it("opens a match in a new tab on ⌘-click (Ctrl-click off macOS) or a middle click", async () => {
    const { onFileSelect } = renderMatches(null);
    const command = isMacPlatform() ? { metaKey: true } : { ctrlKey: true };
    fireEvent.click(await row("b.ts"), command);
    fireEvent(
      await row("c.ts"),
      new MouseEvent("auxclick", { bubbles: true, cancelable: true, composed: true, button: 1 }),
    );

    expect(onFileSelect.mock.calls).toEqual([
      ["src/b.ts", { newTab: true }],
      ["src/c.ts", { newTab: true }],
    ]);
  });

  it("opens the focused match on Enter, and nothing on select-all", async () => {
    const { onFileSelect } = renderMatches("src/a.ts");
    const b = await row("b.ts");
    b.focus();
    const command = isMacPlatform() ? { metaKey: true } : { ctrlKey: true };
    fireEvent.keyDown(b, { key: "a", ...command });
    expect(onFileSelect).not.toHaveBeenCalled();
    expect(selected()).toEqual(["src/a.ts"]);

    fireEvent.keyDown(b, { key: "Enter" });
    expect(onFileSelect.mock.calls).toEqual([["src/b.ts"]]);
  });

  it("follows a file opened elsewhere without opening anything, so the one before opens again", async () => {
    const { onFileSelect, viewing } = renderMatches("src/a.ts");
    await row("a.ts");

    // The breadcrumbs open b.ts: the tree selects it alone, and that opens nothing.
    viewing("src/b.ts");
    expect(selected()).toEqual(["src/b.ts"]);
    // A viewed file outside the matches leaves nothing selected.
    viewing("README.md");
    expect(selected()).toEqual([]);
    expect(onFileSelect).not.toHaveBeenCalled();

    fireEvent.click(await row("a.ts"));
    expect(onFileSelect.mock.calls).toEqual([["src/a.ts"]]);
  });
});
