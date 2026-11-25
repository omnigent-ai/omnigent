import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type * as TicketsApi from "@/lib/projectTicketsApi";
import type { TicketWaitingOn } from "@/lib/projectTicketsApi";
import { askEvidence, decisionOptions, questionStem, TicketDecisionBox } from "./TicketDecisionBox";

vi.mock("@/lib/projectTicketsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof TicketsApi>()),
  listTicketEvents: vi.fn(async () => ({
    data: [
      {
        id: "e1",
        ticket_id: "t1",
        position: 4,
        kind: "comment",
        user_id: "owner@example.com",
        session_id: "lead-session",
        agent_id: "agent",
        created_at: 1,
        payload: {
          text:
            QUESTION +
            "\n\n![Weekly chart](/v1/projects/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/ticket-attachments/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/content)",
        },
      },
    ],
    has_more: false,
    last_position: 4,
  })),
}));
vi.mock("@/components/SessionImage", () => ({
  SessionImage: ({ alt }: { alt: string }) => <img alt={alt} />,
}));

const QUESTION = [
  "Should human reviewers get a note when there's no new data that week?",
  "",
  "1. **Factory only** (recommended). The Lead files a ticket if two Mondays have no data.",
  '2. **Send humans a one-line note** ("No new data this week").',
  "",
  "Reply with 1 or 2.",
].join("\n");

function waiting(overrides: Partial<TicketWaitingOn> = {}): TicketWaitingOn {
  return {
    reason: "owner",
    responders: ["owner@example.com"],
    raised_by_user_id: null,
    raised_by_session_id: "lead-session",
    raised_at: 1_000,
    ask_position: 4,
    question: QUESTION,
    ...overrides,
  };
}

function renderBox(viewer: string, waitingOn = waiting(), signOffOffered = false) {
  const handlers = { onAnswer: vi.fn(), onAnswerInComment: vi.fn(), onShowAsk: vi.fn() };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <TicketDecisionBox
        projectId="p"
        ticketId="t1"
        waitingOn={waitingOn}
        viewerId={viewer}
        personaLookup={(session) => (session === "lead-session" ? "Lead" : null)}
        nowMs={1_000_000 + 2 * 60_000}
        answering={false}
        signOffOffered={signOffOffered}
        {...handlers}
      />
    </QueryClientProvider>,
  );
  return handlers;
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("decisionOptions", () => {
  it("reads explicit numbered choices and yes-or-no questions", () => {
    expect(decisionOptions(QUESTION)).toEqual([
      "1. Factory only (recommended)",
      '2. Send humans a one-line note ("No new data this week")',
    ]);
    expect(decisionOptions("Weekly digest, yes or no?")).toEqual(["Yes", "No"]);
  });

  it("never guesses options from free text or a single item", () => {
    expect(decisionOptions("Which region should we use for the pilot?")).toEqual([]);
    expect(decisionOptions("1. Only one step listed here.")).toEqual([]);
  });

  it("shows only the question's stem once its choices are buttons", () => {
    expect(questionStem(QUESTION, true)).toBe(
      "Should human reviewers get a note when there's no new data that week?",
    );
    expect(questionStem(QUESTION, false)).toContain("1. **Factory only**");
  });

  it("finds up to three images or videos in the ask", () => {
    const text = [1, 2, 3, 4]
      .map(
        (n) =>
          `![Shot ${n}](/v1/projects/${"a".repeat(32)}/ticket-attachments/${String(n).repeat(32)}/content)`,
      )
      .join("\n");
    expect(askEvidence(text).map((item) => item.alt)).toEqual(["Shot 1", "Shot 2", "Shot 3"]);
  });
});

describe("TicketDecisionBox", () => {
  it("shows the asker the question, who asked and when, options and evidence", async () => {
    const handlers = renderBox("owner@example.com");
    const box = screen.getByRole("region", { name: "Decision needed" });
    expect(box).toHaveTextContent("Decision needed · asked by Lead · 2m ago");
    expect(box).toHaveTextContent("Should human reviewers get a note");
    expect(await screen.findByAltText("Weekly chart")).toBeTruthy();
    // The decision controls sit right under the question, before the evidence.
    const options = screen.getByTestId("ticket-decision-options");
    const evidence = screen.getByTestId("ticket-decision-evidence");
    expect(
      options.compareDocumentPosition(evidence) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "1. Factory only (recommended)" }));
    expect(handlers.onAnswer).toHaveBeenCalledWith("Decision: 1. Factory only (recommended)");

    fireEvent.click(screen.getByTestId("ticket-decision-comment"));
    expect(handlers.onAnswerInComment).toHaveBeenCalledWith("@Lead ");
    fireEvent.click(screen.getByRole("button", { name: "See the full ask" }));
    expect(handlers.onShowAsk).toHaveBeenCalledWith(4);
  });

  it("points to the one-click sign-off instead of option buttons on a sign-off ask", () => {
    renderBox("owner@example.com", waiting({ question: "Product OK, yes or no?" }), true);
    expect(screen.queryByTestId("ticket-decision-options")).toBeNull();
    expect(screen.getByTestId("ticket-decision-sign-off")).toHaveTextContent("sign-off above");
  });

  it("shows anyone else one grey line, with no answer controls", () => {
    renderBox("member@example.com");
    expect(screen.getByTestId("ticket-decision-waiting")).toHaveTextContent(
      /^Waiting on .+: Should human reviewers get a note/,
    );
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("stays hidden on an older server that sends no question", () => {
    renderBox("member@example.com", waiting({ question: undefined }));
    expect(screen.queryByRole("region", { name: "Decision needed" })).toBeNull();
  });
});
