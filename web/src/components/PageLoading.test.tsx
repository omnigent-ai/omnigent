import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PageLoading } from "./PageLoading";

describe("PageLoading", () => {
  it("says what's loading, with a spinner", () => {
    render(<PageLoading />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading Omnigent…");
    expect(screen.getByText("Loading Omnigent…")).not.toHaveClass("sr-only");
  });

  it("quietly paints only the background, still named for screen readers", () => {
    render(<PageLoading quiet />);
    expect(screen.getByRole("status")).toHaveClass("bg-background");
    expect(screen.getByText("Loading Omnigent…")).toHaveClass("sr-only");
  });
});
