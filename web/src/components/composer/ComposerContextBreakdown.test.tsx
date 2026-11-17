import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type {
  ContextBreakdownCategory,
  ContextCategoryKind,
  SessionContextBreakdown,
} from "@/lib/sessionsApi";
import { ComposerContextBreakdown } from "./ComposerContextBreakdown";

function row(
  kind: ContextCategoryKind,
  tokens: number | null,
  overrides: Partial<ContextBreakdownCategory> = {},
): ContextBreakdownCategory {
  return {
    kind,
    tokens,
    source: tokens == null ? "unknown" : "estimated",
    origin: "server",
    count: null,
    detail: null,
    ...overrides,
  };
}

function breakdown(overrides: Partial<SessionContextBreakdown> = {}): SessionContextBreakdown {
  return {
    measured: { totalTokens: 20_000, contextWindow: 100_000 },
    categories: [
      row("system_prompt", 2_000, { detail: "Agent instructions and Omnigent guidance" }),
      row("tools", null, { detail: "MCP: github, slack · 1 built-in tool" }),
      row("history", 10_000, { count: 8, detail: "8 messages and tool calls" }),
      row("other", 8_000, {
        detail: "Tool schemas, content the harness adds itself and the last reply",
      }),
      row("free", 80_000, { source: "measured" }),
    ],
    estimatesExceedMeasured: false,
    attachments: [],
    bytesPerToken: 4,
    failedAttempt: null,
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
  it("renders the server's rows in order with their source and detail", () => {
    renderBreakdown(breakdown());

    const rows = screen.getAllByRole("listitem").map((item) => item.dataset.testid);
    expect(rows).toEqual([
      "context-row-system-prompt",
      "context-row-tools",
      "context-row-history",
      "context-row-other",
      "context-row-free",
    ]);
    const prompt = screen.getByTestId("context-row-system-prompt");
    expect(prompt).toHaveAttribute("data-source", "estimated");
    expect(prompt).toHaveTextContent("System prompt");
    expect(prompt).toHaveTextContent("~2K");
    expect(prompt).toHaveTextContent("Agent instructions and Omnigent guidance");
    const free = screen.getByTestId("context-row-free");
    expect(free).toHaveAttribute("data-source", "measured");
    expect(free).toHaveTextContent("80K");
    expect(free).not.toHaveTextContent("~");
    const tools = screen.getByTestId("context-row-tools");
    expect(tools).toHaveAttribute("data-source", "unknown");
    expect(tools).toHaveTextContent("Not measured");
    expect(tools).toHaveTextContent("MCP: github, slack · 1 built-in tool");
    expect(screen.getByTestId("context-row-history")).toHaveTextContent(
      "8 messages and tool calls",
    );
  });

  it("takes the header and every figure from the response, not the ring's own total", () => {
    // The ring still holds an older total than the one the server read its rows against.
    renderBreakdown(
      breakdown({
        measured: { totalTokens: 60_000, contextWindow: 200_000 },
        categories: [row("history", 90_000), row("other", 0, { detail: "They run high" })],
        estimatesExceedMeasured: true,
      }),
    );

    expect(screen.getByText("30% used")).toBeInTheDocument();
    expect(screen.getByText(/60K \/ 200K tokens, measured on the last turn/)).toBeInTheDocument();
    // The remainder is shown as sent: nothing is subtracted or clamped here.
    const other = screen.getByTestId("context-row-other");
    expect(other).toHaveTextContent("~0");
    expect(other).toHaveTextContent("They run high");
    expect(screen.queryByTestId("context-row-free")).not.toBeInTheDocument();
  });

  it("shows only the rows it was sent when nothing was measured", () => {
    renderBreakdown(
      breakdown({
        measured: null,
        categories: [
          row("system_prompt", null, { detail: "Not delivered by this harness" }),
          row("skills", 300, { count: 3 }),
          row("message", 120, { origin: "runner" }),
          // A kind this client does not know yet still renders.
          row("cache_overhead" as ContextCategoryKind, 40),
        ],
      }),
    );

    // The ring's own figures still head the popover.
    expect(screen.getByText("20% used")).toBeInTheDocument();
    const prompt = screen.getByTestId("context-row-system-prompt");
    expect(prompt).toHaveAttribute("data-source", "unknown");
    expect(prompt).toHaveTextContent("Not delivered by this harness");
    expect(screen.getByTestId("context-row-skills")).toHaveTextContent("Skill listing (3)");
    expect(screen.getByTestId("context-row-message")).toHaveTextContent("Current message");
    expect(screen.getByTestId("context-row-cache-overhead")).toHaveTextContent("cache overhead");
    expect(screen.queryByTestId("context-row-other")).not.toBeInTheDocument();
    expect(screen.queryByTestId("context-row-free")).not.toBeInTheDocument();
  });

  it("lists each attachment under its row with its size and how it reached the model", () => {
    const data = breakdown({
      categories: [
        row("attachments", 1_600, {
          count: 4,
          detail: "Plus 1 sent without an estimate, counted in Other",
        }),
        row("history", 10_000),
      ],
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
    // The files sit between their row and the next one.
    const items = screen.getAllByRole("listitem");
    expect(items[0]).toHaveAttribute("data-testid", "context-row-attachments");
    expect(items[5]).toHaveAttribute("data-testid", "context-row-history");
  });

  it("keeps the measured header when the breakdown is loading or unavailable", () => {
    const { unmount } = renderBreakdown(undefined, "pending");
    expect(screen.getByText("Estimating what fills the window…")).toBeInTheDocument();
    unmount();

    renderBreakdown(undefined, "error");
    expect(screen.getByText("A breakdown isn't available for this session.")).toBeInTheDocument();
    expect(screen.getByText("20% used")).toBeInTheDocument();
  });

  it("renders runner rows and a rejected request's rows as the server sent them", () => {
    renderBreakdown(
      breakdown({
        categories: [
          row("tools", 5_400, { origin: "runner", count: 18, detail: "18 tools · MCP: github" }),
          row("message", 100, { origin: "runner" }),
        ],
        failedAttempt: {
          totalTokens: 196_000,
          categories: [
            row("tools", 6_000, { origin: "runner", detail: "18 tools" }),
            row("history", 190_000, {
              origin: "runner",
              detail: "300 messages and tool calls",
            }),
          ],
        },
      }),
    );

    const tools = screen.getByTestId("context-row-tools");
    expect(tools).toHaveTextContent("~5.4K");
    expect(tools).toHaveTextContent("18 tools · MCP: github");
    expect(screen.getByTestId("context-row-message")).toHaveTextContent("Current message");
    const attempt = screen.getByTestId("context-failed-attempt");
    expect(attempt).toHaveTextContent("Last request was too long");
    expect(attempt).toHaveTextContent("About 196K tokens when the turn started");
    expect(attempt).toHaveTextContent("300 messages and tool calls");
    // Only the rows the server sent for the attempt are listed.
    expect(attempt).not.toHaveTextContent("Current message");
  });

  it("shows no rejected-request section without one", () => {
    renderBreakdown(breakdown());

    expect(screen.queryByTestId("context-failed-attempt")).not.toBeInTheDocument();
  });
});
