import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { leftTrimmedPathCandidates, useLeftTrimmedPath } from "./leftTrimmedPath";

describe("leftTrimmedPathCandidates", () => {
  it("drops one leading segment at a time down to the final folder", () => {
    expect(leftTrimmedPathCandidates("/Users/me/omnigent-worktrees/fix-sse")).toEqual([
      "/Users/me/omnigent-worktrees/fix-sse",
      "…/me/omnigent-worktrees/fix-sse",
      "…/omnigent-worktrees/fix-sse",
      "…/fix-sse",
    ]);
  });

  it("keeps a single-segment or root path as-is", () => {
    expect(leftTrimmedPathCandidates("/repo")).toEqual(["/repo"]);
    expect(leftTrimmedPathCandidates("/")).toEqual(["/"]);
  });

  it("keeps backslash separators for Windows paths", () => {
    expect(leftTrimmedPathCandidates("C:\\Users\\me\\repo")).toEqual([
      "C:\\Users\\me\\repo",
      "…\\Users\\me\\repo",
      "…\\me\\repo",
      "…\\repo",
    ]);
  });
});

describe("useLeftTrimmedPath", () => {
  // jsdom has no layout: model an 8px-per-character span in a fixed box.
  let boxWidth = 0;
  beforeEach(() => {
    vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockImplementation(function (
      this: HTMLElement,
    ) {
      return (this.textContent?.length ?? 0) * 8;
    });
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(() => boxWidth);
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  function Harness({ path }: { path: string }) {
    const { ref, text } = useLeftTrimmedPath<HTMLSpanElement>(path);
    return (
      <span data-testid="path" ref={ref}>
        {text}
      </span>
    );
  }

  it("shows the full path when it fits", () => {
    boxWidth = 400;
    render(<Harness path="/Users/me/omnigent-worktrees/fix-sse" />);
    expect(screen.getByTestId("path")).toHaveTextContent(
      /^\/Users\/me\/omnigent-worktrees\/fix-sse$/,
    );
  });

  it("keeps the longest left-trimmed tail that fits", () => {
    boxWidth = 240; // 30 characters
    render(<Harness path="/Users/me/omnigent-worktrees/fix-sse" />);
    expect(screen.getByTestId("path")).toHaveTextContent(/^…\/omnigent-worktrees\/fix-sse$/);
  });

  it("stops at the final folder even when it still overflows", () => {
    boxWidth = 40;
    render(<Harness path="/Users/me/a-very-long-final-folder-name" />);
    expect(screen.getByTestId("path")).toHaveTextContent(/^…\/a-very-long-final-folder-name$/);
  });

  // Mounts the measured element only while open, like tooltip content.
  function LateHarness({ open }: { open: boolean }) {
    const { ref, text } = useLeftTrimmedPath<HTMLSpanElement>(
      "/Users/me/omnigent-worktrees/fix-sse",
    );
    return open ? (
      <span data-testid="path" ref={ref}>
        {text}
      </span>
    ) : null;
  }

  it("measures an element that mounts after the hook, like an opening tooltip", () => {
    boxWidth = 240;
    const { rerender } = render(<LateHarness open={false} />);
    rerender(<LateHarness open />);
    expect(screen.getByTestId("path")).toHaveTextContent(/^…\/omnigent-worktrees\/fix-sse$/);
  });

  it("restarts from the full path when the element remounts with more room", () => {
    boxWidth = 240;
    const { rerender } = render(<LateHarness open />);
    expect(screen.getByTestId("path")).toHaveTextContent(/^…\/omnigent-worktrees\/fix-sse$/);
    rerender(<LateHarness open={false} />);
    boxWidth = 400;
    rerender(<LateHarness open />);
    expect(screen.getByTestId("path")).toHaveTextContent(
      /^\/Users\/me\/omnigent-worktrees\/fix-sse$/,
    );
  });

  it("re-measures from the full path when the path changes", () => {
    boxWidth = 240;
    const { rerender } = render(<Harness path="/Users/me/omnigent-worktrees/fix-sse" />);
    rerender(<Harness path="/srv/app" />);
    expect(screen.getByTestId("path")).toHaveTextContent(/^\/srv\/app$/);
  });
});
