import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import type * as SessionsApiModule from "@/lib/sessionsApi";
import { getSessionContextBreakdown, type SessionContextBreakdown } from "@/lib/sessionsApi";
import { ComposerContextRing } from "./ComposerContextRing";

vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  getSessionContextBreakdown: vi.fn(),
}));

function renderRing(
  contextWindow: number | null,
  tokensUsed: number | null,
  sessionId: string | null = null,
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <ComposerContextRing
          contextWindow={contextWindow}
          tokensUsed={tokensUsed}
          sessionId={sessionId}
        />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

const BREAKDOWN: SessionContextBreakdown = {
  systemPromptTokens: 1_200,
  skillCount: 0,
  skillListingTokens: 0,
  mcpServers: ["github"],
  localToolCount: 0,
  builtinToolCount: 0,
  attachments: [],
  historyTokens: 40_000,
  historyItemCount: 12,
  summarizedItemCount: 0,
  summaryTokens: 0,
  historyPartial: false,
  summarizedPartial: false,
  bytesPerToken: 4,
};

afterEach(() => {
  cleanup();
  vi.mocked(getSessionContextBreakdown).mockReset();
});

describe("ComposerContextRing", () => {
  it("renders nothing when the context window is unknown or zero", () => {
    expect(renderRing(null, 100).container).toBeEmptyDOMElement();
    expect(renderRing(0, 100).container).toBeEmptyDOMElement();
  });

  it("renders nothing when tokensUsed is unknown", () => {
    expect(renderRing(1000, null).container).toBeEmptyDOMElement();
  });

  it("keeps usage text out of the bar while preserving an accessible percentage", () => {
    renderRing(1000, 123);
    expect(screen.getByTestId("composer-context-ring")).not.toHaveTextContent("12%");
    expect(screen.getByTestId("composer-context-ring")).toHaveClass("shrink-0");
    expect(screen.getByTestId("composer-context-ring")).not.toHaveClass("gap-1");
    expect(screen.getByLabelText("12% of context used")).toBeInTheDocument();
  });

  it("shows the actual context usage in the tooltip", async () => {
    const user = userEvent.setup();
    renderRing(1000, 123);
    await user.tab();
    expect(screen.getByTestId("composer-context-ring")).toHaveFocus();
    expect(await screen.findByText("12% context used")).toBeInTheDocument();
    expect(screen.getByText("123 / 1K tokens")).toHaveClass(
      "text-neutral-400",
      "dark:text-muted-foreground",
    );
  });

  it("fits the SVG to the painted ring so its padding does not widen the label gap", () => {
    renderRing(1000, 490);
    const svg = screen.getByTestId("composer-context-ring").querySelector("svg");
    expect(svg).toHaveAttribute("viewBox", "1.5 1.5 13 13");
    expect(svg).toHaveAttribute("width", "13");
    expect(svg).toHaveAttribute("height", "13");
  });

  it("clamps over-full usage to 100%", () => {
    renderRing(1000, 4000);
    expect(screen.getByLabelText("100% of context used")).toBeInTheDocument();
    expect(screen.getByTestId("composer-context-ring")).not.toHaveTextContent("100%");
  });

  it("opens the session's context breakdown on click", async () => {
    vi.mocked(getSessionContextBreakdown).mockResolvedValue(BREAKDOWN);
    const user = userEvent.setup();
    renderRing(200_000, 50_000, "conv_1");

    await user.click(screen.getByTestId("composer-context-ring"));

    const dialog = await screen.findByRole("dialog", { name: "Context window breakdown" });
    expect(await within(dialog).findByTestId("context-row-history")).toHaveTextContent("~40K");
    expect(getSessionContextBreakdown).toHaveBeenCalledWith("conv_1", expect.anything());
    // The hover summary stays closed while its breakdown is open.
    expect(screen.queryByText("25% context used")).not.toBeInTheDocument();
  });

  it("does not fetch a breakdown without a session or before it opens", async () => {
    renderRing(200_000, 50_000, "conv_1");
    renderRing(200_000, 50_000);
    const user = userEvent.setup();

    await user.click(screen.getAllByTestId("composer-context-ring")[1]);

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(getSessionContextBreakdown).not.toHaveBeenCalled();
  });

  it("stays grayscale — never warning/destructive — even at high usage", () => {
    // WHY: the bar is ambient status, not an alarm (#7024). A near-full
    // context must not paint the old warning/destructive colors.
    renderRing(1000, 950);
    const ring = screen.getByTestId("composer-context-ring");
    expect(ring).toHaveClass("text-muted-foreground");
    expect(ring.className).not.toMatch(/text-(warning|destructive)/);
  });
});
