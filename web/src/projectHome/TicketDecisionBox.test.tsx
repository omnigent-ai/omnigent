import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type * as TicketsApi from "@/lib/projectTicketsApi";
import type { TicketWaitingOn } from "@/lib/projectTicketsApi";
import {
  askEvidence,
  askRecommendation,
  decisionOptions,
  questionStem,
  TicketDecisionBox,
} from "./TicketDecisionBox";

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

function renderBox(
  viewer: string,
  waitingOn = waiting(),
  signOffOffered = false,
  extra: Partial<ComponentProps<typeof TicketDecisionBox>> = {},
) {
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
        {...extra}
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
  const labels = (question: string) => decisionOptions(question).map((option) => option.label);

  it("reads numbered choices the ask says to reply with, and yes-or-no questions", () => {
    expect(decisionOptions(QUESTION)).toEqual([
      {
        label: "1. Factory only (recommended)",
        detail: "The Lead files a ticket if two Mondays have no data.",
      },
      { label: '2. Send humans a one-line note ("No new data this week")', detail: "" },
    ]);
    expect(labels("Weekly digest, yes or no?")).toEqual(["Yes", "No"]);
  });

  it("reads lettered and named options, keeping the text after a colon", () => {
    const lettered = [
      "Which scope should Lead messages cover?",
      "A. Questions only: Lead-to-Lead messages that ask for a decision.",
      "B. Everything: status updates too.",
    ].join("\n");
    expect(decisionOptions(lettered)).toEqual([
      {
        label: "A. Questions only",
        detail: "Lead-to-Lead messages that ask for a decision.",
      },
      { label: "B. Everything", detail: "status updates too." },
    ]);
    const named = "Which one?\n- **Option 1:** Ship now\n- **Option 2:** Wait a week";
    expect(labels(named)).toEqual(["Option 1: Ship now", "Option 2: Wait a week"]);
  });

  it("leaves a numbered plan as text, and never guesses from free text", () => {
    const plan = [
      "Here's the plan; does it look right?",
      "1. Add the column.",
      "2. Backfill it.",
      "3. Switch reads over.",
    ].join("\n");
    expect(decisionOptions(plan)).toEqual([]);
    expect(decisionOptions("Which region should we use for the pilot?")).toEqual([]);
    expect(decisionOptions("1. Only one step listed here.")).toEqual([]);
    // Lettered lines that aren't a question stay text too.
    expect(decisionOptions("A. Done the export.\nB. Done the import.")).toEqual([]);
  });

  it("keeps a numbered plan in the stem beside lettered choices", () => {
    const mixed =
      "Plan:\n1. Build the table\n2. Backfill\n\nWhich rollout?\nA. All at once\nB. Behind a flag";
    expect(labels(mixed)).toEqual(["A. All at once", "B. Behind a flag"]);
    expect(questionStem(mixed, true)).toBe(
      "Plan:\n1. Build the table\n2. Backfill\n\nWhich rollout?",
    );
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

    const first = screen.getByRole("button", { name: /^1\. Factory only \(recommended\)/ });
    // The button keeps the option's full text, under its short label and as a tooltip.
    expect(first).toHaveTextContent("The Lead files a ticket if two Mondays have no data.");
    expect(first).toHaveAttribute(
      "title",
      "1. Factory only (recommended): The Lead files a ticket if two Mondays have no data.",
    );
    fireEvent.click(first);
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

  const PDF_QUESTION = [
    "Kasey, should the PDF include the charts?",
    "",
    "A) Charts and table",
    "B) Table only",
    "C) Charts only, table as an appendix",
    "",
    "I recommend A: it matches the web view.",
  ].join("\n");

  it("reads the recommendation and the option it names", () => {
    const options = decisionOptions(PDF_QUESTION);
    expect(askRecommendation(PDF_QUESTION, options)).toMatchObject({
      text: "A: it matches the web view.",
      label: "A. Charts and table",
    });
    expect(
      askRecommendation("Pick one?\nA. x\nB. y", decisionOptions("Pick one?\nA. x\nB. y")),
    ).toBeNull();
  });

  it("asks the named member, with choices, the recommendation and the sign-off row", () => {
    const handlers = renderBox(
      "kasey@example.com",
      waiting({
        responders: ["kasey@example.com"],
        question: PDF_QUESTION,
        ask_kind: "question",
      }),
      true,
      {
        requestedBy: "alex.rivera@example.com",
        pendingSignOffs: [
          { key: "product-ok", approvers: ["aaron@example.com", "ankit@example.com"] },
        ],
      },
    );
    const box = screen.getByRole("region", { name: "Question for you" });
    expect(box).toHaveTextContent("Question for you · from Lead, for Alex Rivera · 2m ago");
    expect(box).toHaveTextContent("Kasey, should the PDF include the charts?");
    expect(screen.getByTestId("ticket-decision-recommendation")).toHaveTextContent(
      "Recommended: A: it matches the web view.",
    );
    // A sign-off owed elsewhere doesn't drop the question's choices.
    const recommended = screen.getByRole("button", { name: /^A\. Charts and table/ });
    expect(recommended).toHaveTextContent("Recommended");
    expect(screen.getByRole("button", { name: /^C\. Charts only/ })).toHaveTextContent(
      "table as an appendix",
    );
    expect(screen.getByTestId("ticket-decision-comment")).toHaveTextContent(
      "Or reply in your own words",
    );
    expect(screen.getByTestId("ticket-decision-sign-off-row")).toHaveTextContent(
      "Also needs a product OKfrom Aaron or Ankit. Not you.",
    );
    fireEvent.click(recommended);
    expect(handlers.onAnswer).toHaveBeenCalledWith("Decision: A. Charts and table");
  });

  it("tells an approver they can give the sign-off themselves", () => {
    renderBox(
      "aaron@example.com",
      waiting({ responders: ["aaron@example.com"], question: PDF_QUESTION, ask_kind: "question" }),
      false,
      { pendingSignOffs: [{ key: "product-ok", approvers: ["aaron@example.com"] }] },
    );
    expect(screen.getByTestId("ticket-decision-sign-off-row")).toHaveTextContent(
      "You can give it with the sign-off above.",
    );
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
