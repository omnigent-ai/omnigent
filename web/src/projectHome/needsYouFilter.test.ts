import { describe, expect, it } from "vitest";
import type { TicketRequest } from "@/lib/ticketRequestsApi";
import { needsYouTicketIds } from "./needsYouFilter";

function request(ticketId: string, overrides: Partial<TicketRequest> = {}): TicketRequest {
  return {
    project_id: "p1",
    project_name: "One",
    ticket_id: ticketId,
    title: ticketId,
    stage: { id: "open", name: "Pending" },
    reason: "owner",
    responders: ["viewer@example.com"],
    ask_position: null,
    raised_at: 1,
    raised_by_session_id: null,
    question: null,
    ...overrides,
  } as TicketRequest;
}

describe("needsYouTicketIds", () => {
  it("keeps this project's asks, without cc's or duplicates", () => {
    const requests = [
      request("a"),
      request("b", { reason: "product-ok" }),
      request("c", { reason: "cc" }),
      request("d", { project_id: "p2" }),
      request("a"),
    ];
    expect(needsYouTicketIds(requests, "p1")).toEqual(["a", "b"]);
    expect(needsYouTicketIds(requests, null)).toEqual([]);
  });
});
