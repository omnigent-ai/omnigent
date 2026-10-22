import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MessageResponse } from "./message";

afterEach(cleanup);

// A throw anywhere inside the markdown pipeline (Streamdown renders mermaid
// diagrams behind React.lazy, whose rejected import is re-thrown on every
// render) used to unmount the whole app, because nothing above it caught.
// Simulated here with a component override that throws during render.
describe("markdown rendering is contained to the block that fails", () => {
  const MARKDOWN = "the diagram source";
  const Boom = () => {
    throw new Error("markdown pipeline exploded");
  };

  it("falls back to the source instead of propagating the throw", () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      // Renders without throwing: the boundary catches, so React keeps the
      // surrounding tree (in the real app, the entire page) mounted.
      const { container } = render(
        <div data-testid="app">
          <MessageResponse components={{ p: Boom }}>{MARKDOWN}</MessageResponse>
        </div>,
      );
      expect(container.querySelector('[data-testid="app"]')).toBeTruthy();
      expect(container.textContent).toContain(MARKDOWN);
    } finally {
      errors.mockRestore();
    }
  });
});
