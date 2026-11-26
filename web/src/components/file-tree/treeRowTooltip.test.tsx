import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { treeRowTooltipText, useTreeRowTooltip } from "./treeRowTooltip";

const MODEL = {};

afterEach(() => {
  cleanup();
  document.body.replaceChildren();
});

function Tree({
  hidden = false,
  onHover,
}: {
  hidden?: boolean;
  onHover?: (treePath: string | null) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const tooltip = useTreeRowTooltip(container, MODEL, { hidden, onHover });
  return (
    <div ref={container}>
      <div data-file-tree="" data-testid="tree" />
      {tooltip}
    </div>
  );
}

// The tree draws its rows itself, and draws more as folders open or the list scrolls.
function addRow(treePath: string): HTMLElement {
  const row = document.createElement("button");
  row.setAttribute("role", "treeitem");
  row.setAttribute("data-item-path", treePath);
  row.textContent = treePath.split("/").filter(Boolean).at(-1) ?? treePath;
  screen.getByTestId("tree").appendChild(row);
  return row;
}

const tooltipWith = (text: string) =>
  screen.queryAllByText(text).filter((element) => element.style.position === "fixed");

describe("treeRowTooltipText", () => {
  it("names a file by its path and a folder without its trailing slash", () => {
    expect(treeRowTooltipText("src/app/main.ts")).toBe("src/app/main.ts");
    expect(treeRowTooltipText("src/app/")).toBe("src/app");
  });
});

describe("useTreeRowTooltip", () => {
  it("shows the full path of a row drawn after the tree mounted, and hears each row once", () => {
    const onHover = vi.fn();
    render(<Tree onHover={onHover} />);
    const row = addRow("packages/app/src/components/keyboardShortcutsPanel.test.ts");

    act(() => {
      fireEvent.pointerMove(row, { clientX: 40, clientY: 60 });
      fireEvent.pointerMove(row, { clientX: 44, clientY: 60 });
    });

    expect(tooltipWith("packages/app/src/components/keyboardShortcutsPanel.test.ts")).toHaveLength(
      1,
    );
    expect(onHover.mock.calls).toEqual([
      ["packages/app/src/components/keyboardShortcutsPanel.test.ts"],
    ]);
  });

  it("follows the pointer to a folder row and clears off the rows and on leaving the tree", () => {
    const onHover = vi.fn();
    render(<Tree onHover={onHover} />);
    const file = addRow("src/main.ts");
    const folder = addRow("src/app/");

    act(() => {
      fireEvent.pointerMove(file);
      fireEvent.pointerMove(folder);
    });
    expect(tooltipWith("src/main.ts")).toHaveLength(0);
    expect(tooltipWith("src/app")).toHaveLength(1);

    act(() => {
      fireEvent.pointerMove(screen.getByTestId("tree"));
    });
    expect(tooltipWith("src/app")).toHaveLength(0);

    act(() => {
      fireEvent.pointerMove(file);
      fireEvent.pointerLeave(screen.getByTestId("tree"));
    });
    expect(tooltipWith("src/main.ts")).toHaveLength(0);
    expect(onHover.mock.calls).toEqual([
      ["src/main.ts"],
      ["src/app/"],
      [null],
      ["src/main.ts"],
      [null],
    ]);
  });

  it("shows nothing while hidden, such as while a row's menu is open", () => {
    render(<Tree hidden />);
    const row = addRow("src/main.ts");

    act(() => {
      fireEvent.pointerMove(row);
    });

    expect(tooltipWith("src/main.ts")).toHaveLength(0);
  });
});
