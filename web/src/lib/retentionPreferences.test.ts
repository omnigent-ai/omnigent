import { describe, expect, it } from "vitest";
import { archivedAtSeconds } from "@/lib/retentionPreferences";
import { ARCHIVED_AT_LABEL_KEY } from "@/lib/sessionListCache";

describe("archivedAtSeconds", () => {
  it("prefers the archived_at label over updated_at", () => {
    expect(
      archivedAtSeconds({ labels: { [ARCHIVED_AT_LABEL_KEY]: "1000" }, updated_at: 5000 }),
    ).toBe(1000);
  });

  it("falls back to updated_at when the label is absent", () => {
    expect(archivedAtSeconds({ labels: {}, updated_at: 5000 })).toBe(5000);
    expect(archivedAtSeconds({ updated_at: 5000 })).toBe(5000);
  });

  it("falls back to updated_at on an unparseable or non-positive label", () => {
    // A stale empty value, or junk, must not read as epoch 0 — that would
    // mark every such session expired and feed it to the bulk delete.
    for (const raw of ["", "not-a-number", "0", "-1"]) {
      expect(
        archivedAtSeconds({ labels: { [ARCHIVED_AT_LABEL_KEY]: raw }, updated_at: 5000 }),
      ).toBe(5000);
    }
  });
});
