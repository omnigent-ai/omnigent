import { describe, expect, it } from "vitest";

import {
  applyCodexApprovalSelection,
  codexApprovalSelectionValue,
  codexCreateApprovalOptions,
} from "@/lib/codexApprovalOptions";
import {
  CODEX_NATIVE_BYPASS_APPROVAL_VALUE,
  CODEX_NATIVE_DEFAULT_APPROVAL_MODE,
} from "@/lib/nativeHarnessModes";

describe("codexCreateApprovalOptions", () => {
  it("includes the bypass entry as the 4th create-time option", () => {
    const values = codexCreateApprovalOptions().map((o) => o.value);
    expect(values).toEqual([
      "default",
      "full-access",
      "read-only",
      CODEX_NATIVE_BYPASS_APPROVAL_VALUE,
    ]);
  });

  it("exposes only { value, label } (no CLI args leak into the picker)", () => {
    for (const option of codexCreateApprovalOptions()) {
      expect(Object.keys(option).sort()).toEqual(["label", "value"]);
    }
  });
});

describe("codexApprovalSelectionValue", () => {
  it("shows the approval preset when bypass is off", () => {
    expect(codexApprovalSelectionValue("read-only", false)).toBe("read-only");
  });

  it("lets bypass win when armed", () => {
    expect(codexApprovalSelectionValue("read-only", true)).toBe(CODEX_NATIVE_BYPASS_APPROVAL_VALUE);
  });
});

describe("applyCodexApprovalSelection", () => {
  it("arms bypass and keeps the prior preset so unchecking restores it", () => {
    expect(applyCodexApprovalSelection(CODEX_NATIVE_BYPASS_APPROVAL_VALUE, "read-only")).toEqual({
      approvalMode: "read-only",
      bypass: true,
    });
  });

  it("selecting a preset clears bypass", () => {
    expect(applyCodexApprovalSelection("full-access", "read-only")).toEqual({
      approvalMode: "full-access",
      bypass: false,
    });
  });

  it("defaults the prior preset to the safe default (no auto-bypass)", () => {
    expect(applyCodexApprovalSelection("read-only")).toEqual({
      approvalMode: "read-only",
      bypass: false,
    });
    // Selecting bypass with no prior preset keeps the safe default underneath.
    expect(applyCodexApprovalSelection(CODEX_NATIVE_BYPASS_APPROVAL_VALUE)).toEqual({
      approvalMode: CODEX_NATIVE_DEFAULT_APPROVAL_MODE,
      bypass: true,
    });
  });
});
