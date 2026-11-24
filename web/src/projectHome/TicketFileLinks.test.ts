import { describe, expect, it } from "vitest";
import { ticketFileHref } from "./TicketFileLinks";

describe("ticketFileHref", () => {
  it("opens the file in the authoring session", () => {
    expect(ticketFileHref("abc", "/home/user/plan.md")).toBe(
      "/c/abc?file=%2Fhome%2Fuser%2Fplan.md",
    );
  });

  it("carries a cited position", () => {
    const href = new URL(
      ticketFileHref("abc", "src/a.ts", { line: 12, column: 3, endLine: 20 }),
      "http://x",
    );
    expect(Object.fromEntries(href.searchParams)).toEqual({
      file: "src/a.ts",
      line: "12",
      column: "3",
      endLine: "20",
    });
  });
});
