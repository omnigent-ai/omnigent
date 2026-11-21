// A design-tool message in the chat: a compact card in the user's bubble.
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it } from "vitest";
import { buildDesignModePrompt } from "@/lib/designModePrompt";
import type { Bubble } from "@/lib/renderItems";
import { BubbleView } from "./chatBubbleParts";

function userBubble(text: string): Bubble {
  return {
    kind: "user",
    itemId: "design_message",
    content: [{ type: "input_text", text }],
    createdAtS: 1_700_000_000,
  };
}

function renderBubble(bubble: Bubble) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <BubbleView bubble={bubble} isLastAssistant={false} />
    </QueryClientProvider>,
  );
}

afterEach(cleanup);

describe("a design-tool message", () => {
  it("shows its request and element as a card instead of the raw block", async () => {
    renderBubble(
      userBubble(buildDesignModePrompt({ tag: "p", id: "#blurb", text: "Hi" }, "Make it red")),
    );
    expect(await screen.findByTestId("design-request")).toHaveTextContent('p#blurb "Hi"');
    expect(screen.getByText("Make it red")).toBeInTheDocument();
    expect(screen.queryByText(/Design Mode/)).toBeNull();
  });

  it("renders the request's Markdown, as other messages do", async () => {
    renderBubble(
      userBubble(
        buildDesignModePrompt({ tag: "p" }, "Match **this** [style](https://example.com/)"),
      ),
    );
    await screen.findByTestId("design-request");
    expect(screen.getByRole("link", { name: "style" })).toHaveAttribute(
      "href",
      "https://example.com/",
    );
    expect(screen.queryByText(/\*\*/)).toBeNull();
  });
});
