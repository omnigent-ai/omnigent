import { cleanup, render, screen } from "@testing-library/react";
import { Streamdown } from "streamdown";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mermaid as standaloneSvgMermaid } from "@/components/ai-elements/mermaidPlugin";
import { MermaidPreview } from "./MermaidPreview";

let resolvedTheme = "light";
vi.mock("next-themes", () => ({
  useTheme: () => ({ resolvedTheme }),
}));
vi.mock("streamdown", () => ({
  Streamdown: vi.fn(({ mermaid }: { mermaid: { config: { theme: string } } }) => (
    <div data-testid="diagram" data-mermaid-theme={mermaid.config.theme} />
  )),
}));

const source = "flowchart LR\n  Agent --> Gateway";

afterEach(() => {
  cleanup();
  resolvedTheme = "light";
});

describe("MermaidPreview", () => {
  it("passes the current app theme to Mermaid after a theme change", () => {
    const { rerender } = render(<MermaidPreview source={source} />);
    expect(screen.getByTestId("diagram")).toHaveAttribute("data-mermaid-theme", "default");

    resolvedTheme = "dark";
    rerender(<MermaidPreview source={source} />);
    expect(screen.getByTestId("diagram")).toHaveAttribute("data-mermaid-theme", "dark");
  });

  it("renders through the plugin that saves diagrams as standalone SVG", () => {
    render(<MermaidPreview source={source} />);
    expect(vi.mocked(Streamdown).mock.calls.at(-1)?.[0].plugins?.mermaid).toBe(
      standaloneSvgMermaid,
    );
  });
});
