import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MermaidPreview } from "./MermaidPreview";

let resolvedTheme = "light";
vi.mock("next-themes", () => ({
  useTheme: () => ({ resolvedTheme }),
}));
vi.mock("streamdown", () => ({
  Streamdown: ({ mermaid }: { mermaid: { config: { theme: string } } }) => (
    <div data-testid="diagram" data-mermaid-theme={mermaid.config.theme} />
  ),
}));

afterEach(() => {
  cleanup();
  resolvedTheme = "light";
});

describe("MermaidPreview theme", () => {
  it("passes the current app theme to Mermaid after a theme change", () => {
    const source = "flowchart LR\n  Agent --> Gateway";
    const { rerender } = render(<MermaidPreview source={source} />);
    expect(screen.getByTestId("diagram")).toHaveAttribute("data-mermaid-theme", "default");

    resolvedTheme = "dark";
    rerender(<MermaidPreview source={source} />);
    expect(screen.getByTestId("diagram")).toHaveAttribute("data-mermaid-theme", "dark");
  });
});
