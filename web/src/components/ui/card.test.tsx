import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Card, CardFooter, CardHeader } from "./card";

afterEach(cleanup);

describe("Card geometry", () => {
  it("uses Otto's medium container radius and warm first-step elevation", () => {
    render(
      <Card data-testid="card">
        <CardHeader data-testid="header">Header</CardHeader>
        <CardFooter data-testid="footer">Footer</CardFooter>
      </Card>,
    );

    expect(screen.getByTestId("card")).toHaveClass(
      "rounded-[var(--radius-otto-md)]",
      "ring-[var(--border-otto-container)]",
      "[box-shadow:var(--elevation-otto-1)]",
    );
    expect(screen.getByTestId("header")).toHaveClass("rounded-t-[var(--radius-otto-md)]");
    expect(screen.getByTestId("footer")).toHaveClass("rounded-b-[var(--radius-otto-md)]");
  });
});
