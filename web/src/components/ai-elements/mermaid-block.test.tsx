// ChatMermaidBlock: the chat-side mermaid renderer must render as soon as
// a fence is complete — INDEPENDENT of visibility (the settled-turn fold
// keeps its trace mounted but hidden, where an IntersectionObserver-gated
// renderer would never run) — and must paint a previously rendered chart
// on its first frame after a remount, so re-expanding settled content
// cannot re-run the async diagram pipeline and jolt the layout.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatMermaidBlock } from "./mermaid-block";

const { renderMock } = vi.hoisted(() => ({
  renderMock: vi.fn(async (id: string, chart: string) => ({
    svg: `<svg data-chart-len="${chart.length}"><title>${id}</title></svg>`,
  })),
}));

vi.mock("@streamdown/mermaid", () => ({
  mermaid: { getMermaid: () => ({ render: renderMock }) },
}));

afterEach(() => {
  cleanup();
  renderMock.mockClear();
});

// The module keeps a per-chart cache across mounts (that is the point),
// so each test uses a distinct chart to stay independent.
const chart = (name: string) => `graph TD\n  A[${name}] --> B[end]`;

describe("ChatMermaidBlock", () => {
  it("does not render a still-streaming fence, reserving its footprint", () => {
    render(<ChatMermaidBlock code={chart("streaming")} isIncomplete language="mermaid" />);
    expect(renderMock).not.toHaveBeenCalled();
    expect(screen.queryByRole("img")).toBeNull();
    // The body still reserves space so completion shifts layout minimally.
    expect(screen.getByTestId("chat-mermaid-block")).toBeDefined();
  });

  it("renders the diagram once the fence is complete, even while hidden", async () => {
    const code = chart("complete");
    render(
      // Hidden wrapper: rendering must not depend on visibility.
      <div hidden>
        <ChatMermaidBlock code={code} isIncomplete={false} language="mermaid" />
      </div>,
    );
    const img = await screen.findByRole("img", { hidden: true });
    expect(img.querySelector("svg")).not.toBeNull();
    expect(renderMock).toHaveBeenCalledTimes(1);
    expect(renderMock).toHaveBeenCalledWith(expect.stringMatching(/^chat-mermaid-/), code);
  });

  it("paints a cached chart on the first frame of a remount without re-rendering", async () => {
    const code = chart("cached");
    const first = render(<ChatMermaidBlock code={code} isIncomplete={false} language="mermaid" />);
    await screen.findByRole("img");
    first.unmount();

    render(<ChatMermaidBlock code={code} isIncomplete={false} language="mermaid" />);
    // Synchronously present — no async pipeline, no layout jolt.
    expect(screen.getByRole("img").querySelector("svg")).not.toBeNull();
    expect(renderMock).toHaveBeenCalledTimes(1);
  });

  it("coalesces concurrent mounts of the same chart into one render", async () => {
    const code = chart("shared");
    render(
      <>
        <ChatMermaidBlock code={code} isIncomplete={false} language="mermaid" />
        <ChatMermaidBlock code={code} isIncomplete={false} language="mermaid" />
      </>,
    );
    await waitFor(() => expect(screen.getAllByRole("img")).toHaveLength(2));
    expect(renderMock).toHaveBeenCalledTimes(1);
  });

  it("surfaces a render failure with the chart source", async () => {
    renderMock.mockRejectedValueOnce(new Error("Parse error on line 2"));
    const code = chart("broken");
    render(<ChatMermaidBlock code={code} isIncomplete={false} language="mermaid" />);
    await screen.findByText(/Mermaid error: Parse error on line 2/);
    expect(screen.getByText(new RegExp("A\\[broken\\]"))).toBeDefined();
    expect(screen.queryByRole("img")).toBeNull();
  });
});
