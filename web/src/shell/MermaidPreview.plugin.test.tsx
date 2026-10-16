import { cleanup, render } from "@testing-library/react";
import { Streamdown } from "streamdown";
import { afterEach, expect, it, vi } from "vitest";
import { mermaid } from "@/components/ai-elements/mermaidPlugin";
import { MermaidPreview } from "./MermaidPreview";

vi.mock("next-themes", () => ({
  useTheme: () => ({ resolvedTheme: "light" }),
}));
vi.mock("streamdown", () => ({
  Streamdown: vi.fn(() => null),
}));

afterEach(cleanup);

it("renders file previews through the plugin that serialises diagrams as XML", () => {
  render(<MermaidPreview source={"flowchart LR\n  Agent --> Gateway"} />);

  const props = vi.mocked(Streamdown).mock.calls.at(-1)?.[0];
  expect(props?.plugins?.mermaid).toBe(mermaid);
});
