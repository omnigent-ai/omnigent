import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ComposerFormSurface, ComposerSurface } from "./ComposerSurface";

afterEach(cleanup);

describe("ComposerSurface geometry", () => {
  it("uses the Otto-derived outer radius without changing core sizing", () => {
    render(<ComposerSurface data-testid="composer">Composer</ComposerSurface>);

    expect(screen.getByTestId("composer")).toHaveClass(
      "min-h-[58px]",
      "rounded-[var(--radius-otto-lg)]",
      "p-3",
      "duration-[var(--duration-otto-fast)]",
      "transition-[border-color,box-shadow,transform]",
      "ease-[var(--ease-otto)]",
    );
  });

  it("keeps form composers and drag overlays on the same silhouette", () => {
    render(
      <ComposerFormSurface data-testid="composer-form" isDragActive>
        Composer
      </ComposerFormSurface>,
    );

    expect(screen.getByTestId("composer-form")).toHaveClass("rounded-[var(--radius-otto-lg)]");
    expect(screen.getByText("Drop files here").parentElement).toHaveClass(
      "rounded-[var(--radius-otto-lg)]",
    );
  });
});
