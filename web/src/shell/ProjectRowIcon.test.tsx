import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ProjectRowIcon } from "./ProjectPicker";

afterEach(cleanup);

describe("ProjectRowIcon", () => {
  it("renders the emoji in a fixed centered box so labels stay column-aligned", () => {
    render(<ProjectRowIcon icon="🚀" />);
    const icon = screen.getByTestId("project-icon");
    expect(icon).toHaveTextContent("🚀");
    // The emoji shares the folder icon's fixed size-3.5 footprint and is
    // centered — a bare span would track each glyph's advance-width and drift
    // the trailing label row to row.
    expect(icon.className).toContain("size-3.5");
    expect(icon.className).toContain("justify-center");
  });

  it("applies a caller's className to both the emoji and folder branches", () => {
    // The header breadcrumb resizes the icon to size-4 (16px); the class must
    // reach both the emoji span and the folder svg so neither shrinks.
    const { rerender, container } = render(<ProjectRowIcon icon="🚀" className="size-4" />);
    expect(screen.getByTestId("project-icon").className).toContain("size-4");

    rerender(<ProjectRowIcon icon={null} className="size-4" />);
    expect(container.querySelector("svg")?.getAttribute("class")).toContain("size-4");
  });

  it("falls back to a folder glyph when no icon is set", () => {
    const { container } = render(<ProjectRowIcon icon={null} />);
    expect(screen.queryByTestId("project-icon")).toBeNull();
    // The lucide folder icon renders as an inline svg.
    expect(container.querySelector("svg")).not.toBeNull();
  });
});
