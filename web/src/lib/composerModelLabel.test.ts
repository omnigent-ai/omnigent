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

  it("folds catalog-prefixed ids the same way", () => {
    expect(nativeModelLabel({ id: "system.ai.claude-opus-4-8[1m]" })).toBe("Opus 4.8 (1M context)");
    expect(nativeModelLabel({ id: "databricks-claude-sonnet-4-6" })).toBe("Sonnet 4.6");
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

describe("compactModelTriggerLabel", () => {
  it("collapses Default (X) to X", () => {
    expect(compactModelTriggerLabel("Default (Opus 4.8)")).toBe("Opus 4.8");
  });

  it("keeps the (1M context) variant instead of stripping it", () => {
    expect(compactModelTriggerLabel("Opus 4.8 (1M context)")).toBe("Opus 4.8 (1M context)");
    expect(compactModelTriggerLabel("Default (Opus 4.8 (1M context))")).toBe(
      "Opus 4.8 (1M context)",
    );
  });

  it("passes a plain label through unchanged", () => {
    expect(compactModelTriggerLabel("Sonnet 5")).toBe("Sonnet 5");
  });
});

describe("landing ↔ chat label parity", () => {
  it("the trigger label matches the chat status label for the same picked row", () => {
    const row: NativeModelOption = { id: "opus[1m]", model: "claude-opus-4-8[1m]" };
    // Landing derives its trigger from nativeModelLabel; chat derives from the
    // raw model id. Both must land on the same string.
    const landing = compactModelTriggerLabel(nativeModelLabel(row));
    const chat = formatStatusModelLabel(row.model!, [row]);
    expect(landing).toBe("Opus 4.8 (1M context)");
    expect(chat).toBe("Opus 4.8 (1M context)");
    expect(landing).toBe(chat);
  });
});

describe("nativeModelLabel — managed catalog rows (wire model carries the variant)", () => {
  // Exact row shapes from the harness producer: the server strips [1m] when
  // building displayName, so the (1M context) variant lives only on the wire
  // model and the label must recover it from there.
  it("keeps (1M context) for a 1M row whose displayName omits it", () => {
    expect(
      nativeModelLabel({
        id: "opus",
        model: "system.ai.claude-opus-4-8[1m]",
        displayName: "Opus 4.8",
      }),
    ).toBe("Opus 4.8 (1M context)");
  });

  it("has no suffix for the ordinary (non-1M) row", () => {
    expect(
      nativeModelLabel({ id: "opus", model: "system.ai.claude-opus-4-8", displayName: "Opus 4.8" }),
    ).toBe("Opus 4.8");
  });

  it("catalog-free id and catalog row render identically (no suffix loss on arrival)", () => {
    const wire = "system.ai.claude-opus-4-8[1m]";
    const catalogFree = formatStatusModelLabel(wire, []);
    const withCatalog = formatStatusModelLabel(wire, [
      { id: "opus", model: wire, displayName: "Opus 4.8" },
    ]);
    expect(catalogFree).toBe("Opus 4.8 (1M context)");
    expect(withCatalog).toBe("Opus 4.8 (1M context)");
    expect(catalogFree).toBe(withCatalog);
  });

  it("keeps a genuinely custom displayName verbatim, even on a 1M wire model", () => {
    expect(
      nativeModelLabel({
        id: "custom",
        model: "system.ai.claude-sonnet-4-6[1m]",
        displayName: "Research Brain",
      }),
    ).toBe("Research Brain");
  });

  it("preserves the advertised version instead of rewriting it to the wire version", () => {
    expect(
      nativeModelLabel({ id: "custom", model: "claude-sonnet-4-6", displayName: "Sonnet 5" }),
    ).toBe("Sonnet 5");
  });
});

describe("formatStatusModelLabel — Claude id folding (catalog-free)", () => {
  it("folds a full Claude id without any catalog", () => {
    expect(formatStatusModelLabel("claude-opus-4-8[1m]", [])).toBe("Opus 4.8 (1M context)");
    expect(formatStatusModelLabel("claude-sonnet-4-6", [])).toBe("Sonnet 4.6");
  });

  it("folds catalog-prefixed ids without any catalog", () => {
    expect(formatStatusModelLabel("system.ai.claude-opus-4-8[1m]", [])).toBe(
      "Opus 4.8 (1M context)",
    );
    expect(formatStatusModelLabel("databricks-claude-sonnet-4-6", [])).toBe("Sonnet 4.6");
  });

  it("is stable for a full Claude id across the pre-catalog → catalog window", () => {
    const before = formatStatusModelLabel("claude-opus-4-8[1m]", []);
    const after = formatStatusModelLabel("claude-opus-4-8[1m]", [
      { id: "opus[1m]", model: "claude-opus-4-8[1m]" },
    ]);
    expect(before).toBe("Opus 4.8 (1M context)");
    expect(after).toBe("Opus 4.8 (1M context)");
    expect(before).toBe(after);
  });
});

describe("formatStatusModelLabel — source transitions", () => {
  it("fills the version for a bare alias once the catalog arrives, keeping the variant", () => {
    // A bare alias legitimately withholds the version the client can't yet
    // know; the catalog supplies it. The (1M context) variant is present in
    // both, so the distinguishing suffix never pops in or out — but the label
    // does change (version fills in), so this is a real transition, not stable.
    const before = formatStatusModelLabel("opus[1m]", []);
    const after = formatStatusModelLabel("opus[1m]", [
      { id: "opus[1m]", model: "claude-opus-4-8[1m]" },
    ]);
    expect(before).toBe("Opus (1M context)");
    expect(after).toBe("Opus 4.8 (1M context)");
    expect(before).toContain("(1M context)");
    expect(after).toContain("(1M context)");
  });

  it("resolves a Codex raw id to its display name once the catalog arrives", () => {
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

describe("effort normalization (single source)", () => {
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
