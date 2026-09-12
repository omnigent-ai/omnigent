// Tests for the shared project label: the chosen emoji renders (aria-hidden,
// so the accessible name stays the plain project name), and an emoji-less
// project falls back to the default FolderIcon glyph instead of a blank gap.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ProjectLabel } from "./ProjectLabel";

afterEach(cleanup);

describe("ProjectLabel", () => {
  it("renders the project's emoji icon, hidden from the accessible name", () => {
    render(<ProjectLabel name="Data Pipeline" icon="📊" />);
    const glyph = screen.getByTestId("project-label-icon");
    expect(glyph).toHaveTextContent("📊");
    // aria-hidden keeps role/name-based selectors matching the bare name.
    expect(glyph).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByText("Data Pipeline")).toBeInTheDocument();
    expect(screen.queryByTestId("project-label-fallback")).toBeNull();
  });

  it("falls back to the folder glyph when there is no icon", () => {
    render(<ProjectLabel name="Plain Project" />);
    const fallback = screen.getByTestId("project-label-fallback");
    expect(fallback).toBeInTheDocument();
    expect(fallback).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByText("Plain Project")).toBeInTheDocument();
    expect(screen.queryByTestId("project-label-icon")).toBeNull();
  });

  it("treats an explicit null icon like an absent one", () => {
    render(<ProjectLabel name="Null Icon" icon={null} />);
    expect(screen.getByTestId("project-label-fallback")).toBeInTheDocument();
    expect(screen.queryByTestId("project-label-icon")).toBeNull();
  });
});
