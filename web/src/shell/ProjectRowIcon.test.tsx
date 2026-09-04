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

  it("falls back to a folder glyph when no icon is set", () => {
    const { container } = render(<ProjectRowIcon icon={null} />);
    expect(screen.queryByTestId("project-icon")).toBeNull();
    // The lucide folder icon renders as an inline svg.
    expect(container.querySelector("svg")).not.toBeNull();
  });
});
