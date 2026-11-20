import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PageLoading } from "./PageLoading";

describe("PageLoading", () => {
  it("says what's loading, with a spinner", () => {
    const { container } = render(<PageLoading />);
    expect(screen.getAllByRole("status")[0]).toHaveTextContent("Loading Omnigent…");
    expect(screen.getByText("Loading Omnigent…")).not.toHaveClass("sr-only");
    expect(container.querySelector(".animate-spin")).not.toBeNull();
  });

  it("quietly paints only the background, still named for screen readers", () => {
    const { container } = render(<PageLoading quiet />);
    expect(screen.getByRole("status")).toHaveClass("bg-background");
    expect(screen.getByText("Loading Omnigent…")).toHaveClass("sr-only");
    expect(container.querySelector(".animate-spin")).toBeNull();
  });
});
