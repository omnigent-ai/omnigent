import { describe, expect, it, vi } from "vitest";
import type { ProjectTicket, TicketStage } from "@/lib/projectTicketsApi";
import { milestoneToShow } from "./ProjectMilestoneNote";
import { DONE_MARK_MS, emptyLaneLine, isRecentlyFinishedForViewer } from "./ProjectTicketBoard";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/identity")>()),
  getCurrentUserId: () => "kasey@example.com",
}));

const stage = (overrides: Partial<TicketStage>) =>
  ({ id: "review", name: "Review", is_complete: false, ...overrides }) as TicketStage;

describe("milestoneToShow", () => {
  it("shows the highest milestone reached that hasn't been dismissed", () => {
    expect(milestoneToShow(9, 0)).toBeNull();
    expect(milestoneToShow(10, 0)).toBe(10);
    expect(milestoneToShow(140, 10)).toBe(100);
    expect(milestoneToShow(140, 100)).toBeNull();
    expect(milestoneToShow(300, 0)).toBe(250);
  });

  it("stays hidden while the count is unknown", () => {
    expect(milestoneToShow(null, 0)).toBeNull();
  });
});

describe("emptyLaneLine", () => {
  it("names the lane without implying work is late", () => {
    expect(emptyLaneLine(stage({}), false)).toBe("Nothing in Review right now.");
    expect(emptyLaneLine(stage({ id: "open", name: "Open" }), true)).toBe("Nothing new right now.");
    expect(emptyLaneLine(stage({ id: "done", name: "Done", is_complete: true }), false)).toBe(
      "Nothing finished yet.",
    );
  });
});

describe("isRecentlyFinishedForViewer", () => {
  const now = 1_800_000_000_000;
  const ticket = (requested_by: string, ageMs: number) =>
    ({ requested_by, updated_at: (now - ageMs) / 1000 }) as ProjectTicket;

  it("marks only the viewer's own tickets, for a day", () => {
    expect(isRecentlyFinishedForViewer(ticket("kasey@example.com", 60_000), now)).toBe(true);
    expect(isRecentlyFinishedForViewer(ticket("alex@example.com", 60_000), now)).toBe(false);
    expect(isRecentlyFinishedForViewer(ticket("kasey@example.com", DONE_MARK_MS + 1), now)).toBe(
      false,
    );
  });
});
