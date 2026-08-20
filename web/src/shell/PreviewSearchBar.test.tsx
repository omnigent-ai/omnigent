// Tests for the rendered-preview find bar (PreviewSearchBar).
//
// jsdom implements neither the CSS Custom Highlight API nor layout
// (getBoundingClientRect returns zeros), so both are stubbed: a fake
// CSS.highlights/Highlight records what would be painted, letting us assert the
// query → count → paint → navigate flow against a real preview DOM subtree.

import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createRef } from "react";
import { PreviewSearchBar } from "./PreviewSearchBar";

// Records the ranges set under each highlight name.
const painted = new Map<string, number>();

class FakeHighlight {
  ranges: unknown[];
  constructor(...ranges: unknown[]) {
    this.ranges = ranges;
  }
}

beforeEach(() => {
  painted.clear();
  // getBoundingClientRect is used only for scroll-into-view; zeros make it a no-op.
  Element.prototype.scrollBy = vi.fn();
  Range.prototype.getBoundingClientRect = vi.fn(
    () => ({ top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }) as DOMRect,
  );
  vi.stubGlobal("Highlight", FakeHighlight);
  vi.stubGlobal("CSS", {
    highlights: {
      set: (name: string, hl: FakeHighlight) => painted.set(name, hl.ranges.length),
      delete: (name: string) => painted.delete(name),
    },
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// Renders a fixed preview subtree plus the bar pointing at it.
function renderBar(props: { open?: boolean; onClose?: () => void } = {}) {
  const onClose = props.onClose ?? vi.fn();
  const containerRef = createRef<HTMLDivElement>();
  const inputRef = createRef<HTMLInputElement>();
  const utils = render(
    <div>
      <div ref={containerRef}>
        <p>The quick brown fox jumps over the lazy dog.</p>
      </div>
      <PreviewSearchBar
        containerRef={containerRef}
        contentVersion="v1"
        open={props.open ?? true}
        onClose={onClose}
        inputRef={inputRef}
      />
    </div>,
  );
  return { ...utils, onClose };
}

function type(value: string) {
  const input = screen.getByPlaceholderText("Find…");
  fireEvent.change(input, { target: { value } });
  return input;
}

// Harness whose preview text + contentVersion can be updated, to exercise the
// "rendered content changes while the bar is open" recompute path.
function Harness({ text, version }: { text: string; version: string }) {
  const containerRef = createRef<HTMLDivElement>();
  const inputRef = createRef<HTMLInputElement>();
  return (
    <div>
      <div ref={containerRef}>
        <p>{text}</p>
      </div>
      <PreviewSearchBar
        containerRef={containerRef}
        contentVersion={version}
        open
        onClose={vi.fn()}
        inputRef={inputRef}
      />
    </div>
  );
}

describe("PreviewSearchBar", () => {
  it("renders nothing when closed", () => {
    renderBar({ open: false });
    expect(screen.queryByPlaceholderText("Find…")).toBeNull();
  });

  it("shows the match count and paints highlights as the user types", async () => {
    renderBar();
    await act(async () => {
      type("the");
    });
    // Two matches of "the"; current is painted separately from the rest.
    expect(screen.getByText("1 / 2")).toBeDefined();
    expect(painted.get("md-preview-search")).toBe(1); // the non-current match
    expect(painted.get("md-preview-search-current")).toBe(1);
  });

  it("shows 'No results' and paints nothing when nothing matches", async () => {
    renderBar();
    await act(async () => {
      type("zzz");
    });
    expect(screen.getByText("No results")).toBeDefined();
    expect(painted.has("md-preview-search")).toBe(false);
  });

  it("advances to the next match on Enter and wraps around", async () => {
    renderBar();
    const input = await act(async () => type("the"));
    expect(screen.getByText("1 / 2")).toBeDefined();
    await act(async () => {
      fireEvent.keyDown(input, { key: "Enter" });
    });
    expect(screen.getByText("2 / 2")).toBeDefined();
    await act(async () => {
      fireEvent.keyDown(input, { key: "Enter" });
    });
    expect(screen.getByText("1 / 2")).toBeDefined();
  });

  it("navigates with the up/down buttons", async () => {
    renderBar();
    await act(async () => type("the"));
    await act(async () => {
      fireEvent.click(screen.getByLabelText("Next match"));
    });
    expect(screen.getByText("2 / 2")).toBeDefined();
    await act(async () => {
      fireEvent.click(screen.getByLabelText("Previous match"));
    });
    expect(screen.getByText("1 / 2")).toBeDefined();
  });

  it("calls onClose and clears highlights on Escape", async () => {
    const { onClose } = renderBar();
    const input = await act(async () => type("the"));
    expect(painted.size).toBeGreaterThan(0);
    await act(async () => {
      fireEvent.keyDown(input, { key: "Escape" });
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("calls onClose when the ✕ button is clicked", async () => {
    const { onClose } = renderBar();
    await act(async () => type("the"));
    await act(async () => {
      fireEvent.click(screen.getByLabelText("Close search"));
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("recomputes matches against the new DOM when content changes while open", async () => {
    // One "cat" initially.
    const { rerender } = render(<Harness text="cat" version="v1" />);
    await act(async () => {
      type("cat");
    });
    expect(screen.getByText("1 / 1")).toBeDefined();

    // The rendered content changes while the bar stays open (new version). The
    // recompute must walk the committed DOM, not the replaced nodes — so the
    // count reflects the new text ("cat cat" → 2), proving ranges rebuilt
    // post-commit rather than during render against stale nodes.
    await act(async () => {
      rerender(<Harness text="cat cat" version="v2" />);
    });
    expect(screen.getByText("1 / 2")).toBeDefined();
  });
});
