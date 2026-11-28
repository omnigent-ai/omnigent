import { describe, expect, it } from "vitest";
import {
  formatCents,
  formatLines,
  prsMergedZeroNote,
  sortStatusMembers,
  type ProjectStatusMember,
} from "./projectStatusApi";

function member(name: string, values: Partial<ProjectStatusMember> = {}): ProjectStatusMember {
  return {
    key: `user:${name}`,
    kind: "human",
    name,
    user_id: name,
    identity_id: null,
    tickets_done: 0,
    prs_merged: null,
    spend_cents: null,
    cost_per_merged_pr_cents: null,
    ...values,
  };
}

describe("formatCents", () => {
  it("formats whole cents as dollars and a missing source as a dash", () => {
    expect(formatCents(123456)).toBe("$1,234.56");
    expect(formatCents(0)).toBe("$0.00");
    expect(formatCents(null)).toBe("—");
  });
});

describe("sortStatusMembers", () => {
  const rows = [
    member("Reviewer", { spend_cents: 500 }),
    member("Ann", { spend_cents: 1200 }),
    member("Lead", { spend_cents: null }),
    member("Bo", { spend_cents: 500 }),
  ];

  it("sorts numbers either way, ties by name, missing values last", () => {
    expect(sortStatusMembers(rows, "spend_cents", "desc").map((row) => row.name)).toEqual([
      "Ann",
      "Bo",
      "Reviewer",
      "Lead",
    ]);
    expect(sortStatusMembers(rows, "spend_cents", "asc").map((row) => row.name)).toEqual([
      "Bo",
      "Reviewer",
      "Ann",
      "Lead",
    ]);
  });

  it("sorts by name and leaves the input untouched", () => {
    expect(sortStatusMembers(rows, "name", "asc").map((row) => row.name)).toEqual([
      "Ann",
      "Bo",
      "Lead",
      "Reviewer",
    ]);
    expect(rows[0].name).toBe("Reviewer");
  });
});

describe("prsMergedZeroNote", () => {
  it("explains a zero only when tickets still finished", () => {
    expect(prsMergedZeroNote(0, 12)).toBe("No finished ticket links a pull request");
    expect(prsMergedZeroNote(0, 0)).toBeUndefined();
    expect(prsMergedZeroNote(3, 12)).toBeUndefined();
    expect(prsMergedZeroNote(null, 12)).toBeUndefined();
  });
});

describe("formatLines", () => {
  it("shows added and removed as two numbers, never one total", () => {
    expect(formatLines(1234, 56)).toBe("+1,234 / −56");
    expect(formatLines(null, 3)).toBe("—");
    expect(formatLines(undefined, undefined)).toBe("—");
  });
});
