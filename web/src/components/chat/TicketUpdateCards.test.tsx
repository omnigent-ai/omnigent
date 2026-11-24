import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { parseTicketUpdateMessage } from "@/lib/ticketUpdateMessage";
import { TicketUpdateCards, personName } from "./TicketUpdateCards";

const PROJECT = "d".repeat(32);
const TICKET = "a".repeat(32);
const OTHER = "b".repeat(32);
const URGENT = `Automatic ticket update for project ${PROJECT}. Use the orchestrator skill to handle it.`;

function renderText(lines: string[]) {
  const message = parseTicketUpdateMessage(lines.join("\n"));
  if (!message) throw new Error("fixture did not parse");
  return render(<TicketUpdateCards message={message} />);
}

describe("TicketUpdateCards", () => {
  it("leads a needs-human update with the question, not the raw labels", () => {
    renderText([
      URGENT,
      "",
      `Ticket ${TICKET} · updated`,
      JSON.stringify({
        changes: {
          labels: [
            { key: "kind", value: "feature" },
            { key: "approved", value: "6f3cdf49105bdf8aae5adc2723b3362162665fcb" },
            { key: "needs-human", value: "Aaron: add product-ok to land it?" },
          ],
        },
      }),
    ]);
    const card = screen.getByTestId("ticket-update-cards");
    expect(card).toHaveTextContent("Needs your answer");
    expect(card).toHaveTextContent("Add product-ok to land it?");
    expect(card).toHaveTextContent("Approved");
    expect(card).not.toHaveTextContent("kind");
    expect(card).not.toHaveTextContent("6f3cdf4");
    expect(screen.queryByRole("button", { name: "Show raw" })).toBeNull();
  });

  it("names the person who commented and shows a stage move", () => {
    renderText([
      URGENT,
      "",
      `Ticket ${TICKET} · comment · by casey.example@example.com`,
      JSON.stringify({ text: "Can we show the team total too?" }),
      "",
      `Ticket ${OTHER} · updated`,
      JSON.stringify({
        changes: { stage_id: "code_review" },
        stage: { id: "code_review", name: "Review", is_complete: false, icon: "eye" },
      }),
    ]);
    const card = screen.getByTestId("ticket-update-cards");
    expect(card).toHaveTextContent("2 tickets updated");
    expect(card).toHaveTextContent("Can we show the team total too?");
    expect(card).toHaveTextContent("Review");
  });

  it("writes digest summaries in plain words", () => {
    renderText([
      `Ticket update digest for project ${PROJECT}. Nothing here asked to wake you; act only where the orchestrator skill says to.`,
      "",
      "Digest of 4 background updates (agent notes, outputs and edits). Read a ticket only if a line needs action:",
      `- ${TICKET} "Board fix": output — latest: artifact /v1/projects/x/files/y/content`,
      `- ${OTHER} "Shortcuts": edited labels`,
    ]);
    const rows = screen.getAllByTestId("ticket-update-card");
    expect(rows[0]).toHaveTextContent("Added a file");
    expect(rows[0]).not.toHaveTextContent("/v1/projects");
    expect(rows[1]).toHaveTextContent("Labels updated");
  });

  it("turns an email into a display name", () => {
    expect(personName("casey.example@example.com")).toBe("Casey Example");
    expect(personName("sam@example.com")).toBe("Sam");
  });
});
