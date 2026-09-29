import { describe, expect, it } from "vitest";

import { permissionModeConcept } from "./nativeHarnessModes";

describe("permissionModeConcept", () => {
  it("maps Claude and Codex vocabularies onto shared concepts", () => {
    expect(permissionModeConcept("claude-native", "default")).toBe("manual");
    expect(permissionModeConcept("claude-native", "plan")).toBe("read-only");
    expect(permissionModeConcept("codex-native", "default")).toBe("automatic");
    expect(permissionModeConcept("codex-native", "read-only")).toBe("read-only");
    expect(permissionModeConcept("codex-native", "full-access")).toBe("full-access");
    expect(permissionModeConcept("codex-native", "bypass")).toBe("full-access");
  });

  it("supports native harness aliases", () => {
    expect(permissionModeConcept("native-codex", "read-only")).toBe("read-only");
    expect(permissionModeConcept("native-agy", "skip")).toBe("full-access");
  });

  it("falls back safely for missing or future values", () => {
    expect(permissionModeConcept(null, "default")).toBe("default");
    expect(permissionModeConcept("codex-native", "future-mode")).toBe("default");
  });
});
