import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setEmbedRoot } from "@/lib/host";
import { useCursorTooltip } from "./useCursorTooltip";

// jsdom has no layout: a 1000x800 viewport and a 200x30 tooltip box.
beforeEach(() => {
  vi.spyOn(document.documentElement, "clientWidth", "get").mockReturnValue(1000);
  vi.spyOn(document.documentElement, "clientHeight", "get").mockReturnValue(800);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    width: 200,
    height: 30,
  } as DOMRect);
});

afterEach(() => {
  cleanup();
  setEmbedRoot(null);
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

// Mirrors a virtualized FolderTree row: the wrapper's transform would become
// the containing block for a fixed tooltip rendered inside it.
function TransformedRow() {
  const { handlers, tooltip } = useCursorTooltip("folder/file.ts");

  return (
    <div data-testid="row" style={{ transform: "translateY(600px)" }}>
      <span {...handlers}>file.ts</span>
      {tooltip}
    </div>
  );
}

function hoverAt(clientX: number, clientY: number) {
  render(<TransformedRow />);
  fireEvent.mouseMove(screen.getByText("file.ts"), { clientX, clientY });
  return screen.getByText("folder/file.ts");
}

describe("useCursorTooltip", () => {
  it("portals the hovered tooltip to the body and removes it on mouse leave", () => {
    const tooltip = hoverAt(120, 200);

    expect(tooltip.parentElement).toBe(document.body);
    expect(screen.getByTestId("row")).not.toContainElement(tooltip);
    expect(tooltip).toHaveStyle({ position: "fixed", left: "120px", top: "214px" });

    fireEvent.mouseLeave(screen.getByText("file.ts"));
    expect(screen.queryByText("folder/file.ts")).not.toBeInTheDocument();
  });

  it("portals into the embed root when one is registered", () => {
    const embedRoot = document.createElement("div");
    document.body.appendChild(embedRoot);
    setEmbedRoot(embedRoot);

    expect(hoverAt(120, 200).parentElement).toBe(embedRoot);
  });

  // Margin 8, 14px below the pointer, 8px above it.
  it.each([
    { name: "above the pointer near the bottom edge", pointer: [120, 780], left: 120, top: 742 },
    { name: "left of the pointer near the right edge", pointer: [900, 200], left: 700, top: 214 },
  ])("flips $name", ({ pointer, left, top }) => {
    expect(hoverAt(pointer[0], pointer[1])).toHaveStyle({ left: `${left}px`, top: `${top}px` });
  });
});
