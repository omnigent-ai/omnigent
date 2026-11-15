import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { SessionContextBreakdown } from "@/lib/sessionsApi";
import { ComposerContextBreakdown, itemizedEstimate } from "./ComposerContextBreakdown";

function breakdown(overrides: Partial<SessionContextBreakdown> = {}): SessionContextBreakdown {
  return {
    systemPromptTokens: 2_000,
    skillCount: 0,
    skillListingTokens: 0,
    mcpServers: [],
    localToolCount: 0,
    builtinToolCount: 0,
    attachments: [],
    historyTokens: 10_000,
    historyItemCount: 8,
    summarizedItemCount: 0,
    summaryTokens: 0,
    historyPartial: false,
    summarizedPartial: false,
    bytesPerToken: 4,
    ...overrides,
  };
}

function renderBreakdown(
  data: SessionContextBreakdown | undefined,
  status: "pending" | "error" | "success" = "success",
  { contextWindow = 100_000, tokensUsed = 20_000 } = {},
) {
  return render(
    <ComposerContextBreakdown
      contextWindow={contextWindow}
      tokensUsed={tokensUsed}
      breakdown={data}
      status={status}
    />,
  );
}

afterEach(cleanup);

describe("ComposerContextBreakdown", () => {
  it("labels estimated rows apart from the measured total and free space", () => {
    renderBreakdown(breakdown());

    expect(screen.getByText("20% used")).toBeInTheDocument();
    expect(screen.getByText(/20K \/ 100K tokens, measured on the\s+last turn/)).toBeInTheDocument();
    const prompt = screen.getByTestId("context-row-system-prompt");
    expect(prompt).toHaveAttribute("data-source", "estimated");
    expect(prompt).toHaveTextContent("~2K");
    const free = screen.getByTestId("context-row-free");
    expect(free).toHaveAttribute("data-source", "measured");
    expect(free).toHaveTextContent("80K");
    expect(free).not.toHaveTextContent("~");
    expect(screen.getByTestId("context-row-tools")).toHaveTextContent("Not measured");
  });

  it("attributes the measured remainder to an Other row and never shows it negative", () => {
    const { unmount } = renderBreakdown(breakdown({ skillCount: 2, skillListingTokens: 500 }));
    // 20K measured minus 2K prompt, 0.5K skills and 10K history.
    expect(screen.getByTestId("context-row-other")).toHaveTextContent("~7.5K");
    unmount();

    renderBreakdown(breakdown({ historyTokens: 90_000 }));
    expect(screen.getByTestId("context-row-other")).toHaveTextContent("~0");
  });

  it("lists each attachment with its size and how it reached the model", () => {
    const data = breakdown({
      attachments: [
        {
          fileId: "file_1",
          filename: "shot.png",
          contentType: "image/png",
          bytes: 2_048,
          delivery: "inline",
          estimatedTokens: 1_600,
        },
        {
          fileId: "file_2",
          filename: "spec.pdf",
          contentType: "application/pdf",
          bytes: 4_096,
          delivery: "inline",
          estimatedTokens: null,
        },
        {
          fileId: "file_3",
          filename: "logs.zip",
          contentType: "application/zip",
          bytes: 1_024,
          delivery: "workspace",
          estimatedTokens: null,
        },
        {
          fileId: "file_4",
          filename: "old.csv",
          contentType: null,
          bytes: null,
          delivery: "missing",
          estimatedTokens: null,
        },
      ],
    });
    renderBreakdown(data);

    expect(screen.getByTestId("context-row-attachments")).toHaveTextContent("Attachments (4)");
    expect(screen.getByTestId("context-row-attachments")).toHaveTextContent("~1.6K");
    expect(screen.getByTestId("context-row-attachments")).toHaveTextContent(
      "Plus 1 sent without an estimate, counted in Other",
    );
    expect(screen.getByText("shot.png · 2.0 KB").closest("li")).toHaveTextContent("~1.6K");
    expect(screen.getByText("spec.pdf · 4.0 KB").closest("li")).toHaveTextContent("Size only");
    expect(screen.getByText("logs.zip · 1.0 KB").closest("li")).toHaveTextContent("In workspace");
    expect(screen.getByText("old.csv").closest("li")).toHaveTextContent("Not delivered");
    expect(itemizedEstimate(data)).toBe(2_000 + 1_600 + 10_000);
  });

  it("says when earlier history was replaced by a summary", () => {
    const { unmount } = renderBreakdown(
      breakdown({ summarizedItemCount: 31, historyPartial: true }),
    );
    expect(screen.getByTestId("context-row-history")).toHaveTextContent(
      "At least 8 messages and tool calls · 31 earlier items replaced by a summary",
    );
    unmount();

    renderBreakdown(breakdown({ summarizedItemCount: 1_995, summarizedPartial: true }));
    expect(screen.getByTestId("context-row-history")).toHaveTextContent(
      "8 messages and tool calls · at least 1995 earlier items replaced by a summary",
    );
  });

  it("names declared tool sources without inventing their size", () => {
    renderBreakdown(breakdown({ mcpServers: ["github", "slack"], builtinToolCount: 1 }));

    const tools = screen.getByTestId("context-row-tools");
    expect(tools).toHaveTextContent("MCP: github, slack · 1 built-in tool");
    expect(tools).toHaveAttribute("data-source", "unknown");
  });

  it("keeps the measured header when the breakdown is loading or unavailable", () => {
    const { unmount } = renderBreakdown(undefined, "pending");
    expect(screen.getByText("Estimating what fills the window…")).toBeInTheDocument();
    unmount();

    renderBreakdown(undefined, "error");
    expect(screen.getByText("A breakdown isn't available for this session.")).toBeInTheDocument();
    expect(screen.getByText("20% used")).toBeInTheDocument();
  });
});
