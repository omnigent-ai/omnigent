import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { SessionContextBreakdown, TurnContextBreakdown } from "@/lib/sessionsApi";
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
    reported: null,
    failedAttempt: null,
    ...overrides,
  };
}

function turn(overrides: Partial<TurnContextBreakdown> = {}): TurnContextBreakdown {
  return {
    systemTokens: 0,
    toolTokens: 0,
    toolCount: 0,
    historyTokens: 0,
    historyItemCount: 0,
    attachmentTokens: 0,
    attachmentCount: 0,
    unestimatedAttachmentCount: 0,
    messageTokens: 0,
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

  it("prefers the runner's report of the request it sent", () => {
    const reported = turn({
      systemTokens: 3_000,
      toolTokens: 5_400,
      toolCount: 18,
      historyTokens: 8_000,
      historyItemCount: 6,
      messageTokens: 100,
    });
    renderBreakdown(breakdown({ mcpServers: ["github"], reported }));

    expect(screen.getByTestId("context-row-system-prompt")).toHaveTextContent("~3K");
    const tools = screen.getByTestId("context-row-tools");
    expect(tools).toHaveAttribute("data-source", "estimated");
    expect(tools).toHaveTextContent("~5.4K");
    expect(tools).toHaveTextContent("18 tools · MCP: github");
    expect(screen.getByTestId("context-row-history")).toHaveTextContent("~8K");
    expect(screen.getByTestId("context-row-message")).toHaveTextContent("~100");
    expect(itemizedEstimate(breakdown({ reported }))).toBe(16_500);
    // 20K measured minus the runner's 16.5K.
    expect(screen.getByTestId("context-row-other")).toHaveTextContent("~3.5K");
    expect(screen.queryByTestId("context-failed-attempt")).not.toBeInTheDocument();
  });

  it("shows what a request rejected as too long attempted", () => {
    renderBreakdown(
      breakdown({
        failedAttempt: turn({ historyTokens: 190_000, historyItemCount: 300, toolTokens: 6_000 }),
      }),
    );

    const attempt = screen.getByTestId("context-failed-attempt");
    expect(attempt).toHaveTextContent("Last request was too long");
    expect(attempt).toHaveTextContent("About 196K tokens attempted");
    expect(attempt).toHaveTextContent("300 messages and tool calls");
    // Empty categories are left out of the attempt.
    expect(attempt).not.toHaveTextContent("Current message");
  });

  it("lists attachments under the runner's total without their own estimates", () => {
    renderBreakdown(
      breakdown({
        reported: turn({ attachmentTokens: 1_600, attachmentCount: 1 }),
        attachments: [
          {
            fileId: "file_1",
            filename: "shot.png",
            contentType: "image/png",
            bytes: 2_048,
            delivery: "inline",
            estimatedTokens: 1_600,
          },
        ],
      }),
    );

    expect(screen.getByTestId("context-row-attachments")).toHaveTextContent("~1.6K");
    expect(screen.getByText("Sent inline")).toBeInTheDocument();
  });

  it("says when the estimates exceed the measured total", () => {
    renderBreakdown(breakdown({ reported: turn({ historyTokens: 30_000 }) }));

    const other = screen.getByTestId("context-row-other");
    expect(other).toHaveTextContent("~0");
    expect(other).toHaveTextContent("exceed the measured total");
  });
});
