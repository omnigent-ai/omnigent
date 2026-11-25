import { describe, expect, it } from "vitest";
import { parseTicketUpdateMessage } from "./ticketUpdateMessage";

const PROJECT = "d".repeat(32);
const TICKET = "a".repeat(32);
const OTHER = "b".repeat(32);
const URGENT = `Automatic ticket update for project ${PROJECT}. Use the orchestrator skill to handle it.`;

describe("parseTicketUpdateMessage", () => {
  it("merges a ticket's label edit and comment into one card", () => {
    const text = [
      URGENT,
      "",
      `Ticket ${TICKET} · updated`,
      JSON.stringify({
        changes: {
          labels: [
            { key: "approved", value: "6f3cdf49105bdf8aae5adc2723b3362162665fcb" },
            { key: "needs-human", value: "Aaron: add product-ok?" },
          ],
        },
      }),
      "",
      `Ticket ${TICKET} · comment`,
      JSON.stringify({ text: "Ready to land.\n\nNeeds two sign-offs.", audience: "human" }),
    ].join("\n");
    const parsed = parseTicketUpdateMessage(text);
    expect(parsed?.projectId).toBe(PROJECT);
    expect(parsed?.cards).toHaveLength(1);
    const card = parsed!.cards[0]!;
    expect(card.labels?.map((label) => label.key)).toEqual(["approved", "needs-human"]);
    expect(card.comment).toMatchObject({
      audience: "human",
      text: "Ready to land.\n\nNeeds two sign-offs.",
    });
  });

  it("reads a stage move, a person's author and the escalation note", () => {
    const text = [
      URGENT,
      "",
      `Ticket ${TICKET} · comment · by member@example.com`,
      JSON.stringify({ text: "Anyone there?" }),
      "",
      "No worker linked to this ticket replied to this person's comment within 5 minutes. Answer it or hand it on.",
      "",
      `Ticket ${OTHER} · updated`,
      JSON.stringify({
        changes: { stage_id: "code_review" },
        stage: { id: "code_review", name: "Review", is_complete: false, icon: "eye" },
      }),
      "",
      "This ticket is in its completed stage without a final update.",
    ].join("\n");
    const cards = parseTicketUpdateMessage(text)!.cards;
    expect(cards[0]).toMatchObject({ escalated: true, comment: { author: "member@example.com" } });
    expect(cards[1]!.movedTo).toMatchObject({ id: "code_review", name: "Review" });
    expect(cards[1]!.editedFields).toEqual([]);
  });

  it("turns digest lines into titled cards", () => {
    const text = [
      `Ticket update digest for project ${PROJECT}. Nothing here asked to wake you; act only where the orchestrator skill says to.`,
      "",
      "Digest of 3 background updates (agent notes, outputs and edits). Read a ticket only if a line needs action:",
      `- ${TICKET} "Fix: the board": agent note ×2 — latest: Rebased onto the tip.`,
      `- ${OTHER} "Second": edited labels`,
    ].join("\n");
    const cards = parseTicketUpdateMessage(text)!.cards;
    expect(cards.map((card) => card.title)).toEqual(["Fix: the board", "Second"]);
    expect(cards[0]).toMatchObject({
      digestSummary: "agent note ×2",
      digestLatest: "Rebased onto the tip.",
    });
    expect(cards[1]).toMatchObject({ digestSummary: "edited labels", digestLatest: null });
  });

  it.each([
    ["another message", "Please review the branch."],
    ["broken JSON", `${URGENT}\n\nTicket ${TICKET} · updated\n{"changes": `],
    ["no tickets", URGENT],
    ["an unknown line", `${URGENT}\n\nSomething new`],
    ["a malformed digest line", `${URGENT}\n\nDigest of 1 background update (x):\n* odd`],
  ])("falls back to text for %s", (_name, text) => {
    expect(parseTicketUpdateMessage(text)).toBeNull();
  });

  it("reads a worker's turn for a person's comment as a comment card", () => {
    const text =
      `A person commented on ticket ${TICKET} "tabs on project page shift" in project ${PROJECT}, which you are linked to:\n\n` +
      "You might want to spin up a Codex harness.\n\nSecond paragraph.\n\n" +
      "You are the one agent asked to answer it; the ticket's other agents were not woken. Reply on the ticket yourself.";
    const message = parseTicketUpdateMessage(text);
    expect(message?.projectId).toBe(PROJECT);
    expect(message?.cards).toHaveLength(1);
    expect(message?.cards[0]).toMatchObject({
      ticketId: TICKET,
      title: "tabs on project page shift",
      personNotice: { kind: "comment" },
      comment: {
        text: "You might want to spin up a Codex harness.\n\nSecond paragraph.",
        audience: "human",
        author: null,
      },
    });
  });

  it.each([
    [
      "worker",
      `A person added \`risk-ok\`, \`product-ok\` on ticket ${TICKET} "Fix it" in project ${PROJECT}, which you are linked to.\n\nCarry on with the step that waited on it.`,
    ],
    [
      "merger",
      `A person added \`risk-ok\`, \`product-ok\` on ticket ${TICKET} "Fix it" in project ${PROJECT}. Its approved commit is \`${"c".repeat(40)}\`. If it is waiting, continue.`,
    ],
  ])("reads the %s's sign-off turn as a card naming the labels", (_who, text) => {
    expect(parseTicketUpdateMessage(text)?.cards[0]).toMatchObject({
      ticketId: TICKET,
      title: "Fix it",
      personNotice: { kind: "signoff", labels: "risk-ok, product-ok" },
      comment: null,
    });
  });

  it("leaves a person's own message that merely starts the same way alone", () => {
    expect(parseTicketUpdateMessage("A person commented on ticket 42, can you look?")).toBeNull();
  });
});

