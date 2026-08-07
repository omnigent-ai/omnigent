import { describe, expect, it } from "vitest";
import { formatMessageTime } from "./messageTime";

// Fixed "now" so day-boundary branches are deterministic: a local-time
// morning, mid-year. Expected labels are built with the same `toLocale*`
// calls the formatter uses, so assertions hold in any test-runner locale —
// what's under test is the branch selection, not the locale's rendering.
const NOW = new Date(2026, 2, 6, 10, 0, 0);

function time(date: Date): string {
  return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

describe("formatMessageTime", () => {
  it("renders time-only for a same-day stamp", () => {
    const at = new Date(2026, 2, 6, 15, 42);
    expect(formatMessageTime(at.getTime() / 1000, NOW)).toBe(time(at));
  });

  it('prefixes "Yesterday" for the previous calendar day', () => {
    // 23:59 the day before — calendar-day comparison, not a 24h window.
    const at = new Date(2026, 2, 5, 23, 59);
    expect(formatMessageTime(at.getTime() / 1000, NOW)).toBe(`Yesterday ${time(at)}`);
  });

  it("renders month + day for an older same-year stamp", () => {
    const at = new Date(2026, 0, 15, 9, 5);
    const day = at.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    expect(formatMessageTime(at.getTime() / 1000, NOW)).toBe(`${day}, ${time(at)}`);
  });

  it("includes the year for a prior-year stamp", () => {
    const at = new Date(2025, 11, 31, 18, 0);
    const day = at.toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
    });
    expect(formatMessageTime(at.getTime() / 1000, NOW)).toBe(`${day}, ${time(at)}`);
  });

  it("returns an empty string for malformed stamps", () => {
    // A zeroed/absent server field must render nothing, not "Invalid Date"
    // or a 1970 epoch label.
    expect(formatMessageTime(Number.NaN, NOW)).toBe("");
    expect(formatMessageTime(0, NOW)).toBe("");
    expect(formatMessageTime(-5, NOW)).toBe("");
    expect(formatMessageTime(Number.POSITIVE_INFINITY, NOW)).toBe("");
  });
});
