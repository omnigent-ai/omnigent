import { describe, expect, it } from "vitest";

import {
  compactModelTriggerLabel,
  defaultModelLabel,
  formatModelEffortStatusLabel,
  formatStatusEffortLabel,
  formatStatusModelLabel,
  nativeModelLabel,
  normalizeEffortLabel,
} from "@/lib/composerModelLabel";
import type { NativeModelOption } from "@/lib/types";

describe("nativeModelLabel", () => {
  it("folds a Claude alias id to Family Major.Minor", () => {
    expect(nativeModelLabel({ id: "opus", model: "claude-opus-4-8" })).toBe("Opus 4.8");
  });

  it("preserves the (1M context) variant suffix", () => {
    expect(nativeModelLabel({ id: "opus[1m]", model: "claude-opus-4-8[1m]" })).toBe(
      "Opus 4.8 (1M context)",
    );
  });

  it("falls back to the advertised display name for a non-Claude row", () => {
    expect(nativeModelLabel({ id: "gpt-5.6-luna", displayName: "GPT-5.6 Luna" })).toBe(
      "GPT-5.6 Luna",
    );
  });
});

describe("defaultModelLabel", () => {
  it("names the marked default", () => {
    expect(defaultModelLabel([{ id: "opus", model: "claude-opus-4-8", isDefault: true }])).toBe(
      "Default (Opus 4.8)",
    );
  });

  it("stays plain Default when nothing is marked", () => {
    expect(defaultModelLabel([{ id: "opus", model: "claude-opus-4-8" }])).toBe("Default");
  });
});

describe("compactModelTriggerLabel (#7094)", () => {
  it("collapses Default (X) to X", () => {
    expect(compactModelTriggerLabel("Default (Opus 4.8)")).toBe("Opus 4.8");
  });

  it("KEEPS the (1M context) variant — the regression this fixes", () => {
    expect(compactModelTriggerLabel("Opus 4.8 (1M context)")).toBe("Opus 4.8 (1M context)");
    expect(compactModelTriggerLabel("Default (Opus 4.8 (1M context))")).toBe(
      "Opus 4.8 (1M context)",
    );
  });

  it("passes a plain label through unchanged", () => {
    expect(compactModelTriggerLabel("Sonnet 5")).toBe("Sonnet 5");
  });
});

describe("landing ↔ chat label parity (#7094)", () => {
  it("the trigger label matches the chat status label for the same picked row", () => {
    const row: NativeModelOption = { id: "opus[1m]", model: "claude-opus-4-8[1m]" };
    // Landing derives its trigger from nativeModelLabel; chat derives from the
    // raw model id via the catalog. Both must land on the same string.
    const landing = compactModelTriggerLabel(nativeModelLabel(row));
    const chat = formatStatusModelLabel(row.model!, [row]);
    expect(landing).toBe("Opus 4.8 (1M context)");
    expect(chat).toBe("Opus 4.8 (1M context)");
    expect(landing).toBe(chat);
  });
});

describe("formatStatusModelLabel catalog/source transitions (flicker)", () => {
  it("is STABLE for a Claude alias across the pre-catalog → catalog window", () => {
    // Before the catalog resolves (empty options) and after it arrives, a
    // Claude alias renders identically — no async flicker for this path.
    const before = formatStatusModelLabel("opus[1m]", []);
    const after = formatStatusModelLabel("opus[1m]", [
      { id: "opus[1m]", model: "claude-opus-4-8[1m]" },
    ]);
    expect(before).toBe("Opus (1M context)");
    expect(after).toBe("Opus 4.8 (1M context)");
    // The alias form omits the version the client can't yet know; the catalog
    // supplies it. Both keep the (1M context) variant, so the distinguishing
    // suffix never pops in or out — the documented residual is only the
    // version digits, which the alias intentionally withholds.
    expect(before).toContain("(1M context)");
    expect(after).toContain("(1M context)");
  });

  it("resolves a Codex raw id to its display name once the catalog arrives", () => {
    // Documented transition: raw id pre-catalog, friendly name post-catalog.
    // The #7039 seed guarantees this is the SELECTED model, never the previous
    // session's, so the transition is raw→pretty for the right model.
    expect(formatStatusModelLabel("gpt-5.6-luna", [])).toBe("gpt-5.6-luna");
    expect(
      formatStatusModelLabel("gpt-5.6-luna", [{ id: "gpt-5.6-luna", displayName: "GPT-5.6 Luna" }]),
    ).toBe("GPT-5.6 Luna");
  });

  it("returns null for an unknown/empty model", () => {
    expect(formatStatusModelLabel(null)).toBeNull();
    expect(formatStatusModelLabel("  ")).toBeNull();
  });
});

describe("effort normalization (#7026 single source)", () => {
  it("normalizes xhigh → xHigh", () => {
    expect(normalizeEffortLabel("xhigh")).toBe("xHigh");
    expect(normalizeEffortLabel("XHIGH")).toBe("xHigh");
    expect(formatStatusEffortLabel("xhigh")).toBe("xHigh");
  });

  it("capitalizes other efforts and null-guards", () => {
    expect(normalizeEffortLabel("high")).toBe("High");
    expect(formatStatusEffortLabel(null)).toBeNull();
    expect(formatStatusEffortLabel("")).toBeNull();
  });
});

describe("formatModelEffortStatusLabel", () => {
  it("joins model and effort", () => {
    expect(formatModelEffortStatusLabel("gpt-5.5", "xhigh")).toBe("gpt-5.5 xHigh");
  });

  it("drops missing parts and returns null when neither is present", () => {
    expect(formatModelEffortStatusLabel("gpt-5.5", null)).toBe("gpt-5.5");
    expect(formatModelEffortStatusLabel(null, "high")).toBe("High");
    expect(formatModelEffortStatusLabel(null, null)).toBeNull();
  });
});
