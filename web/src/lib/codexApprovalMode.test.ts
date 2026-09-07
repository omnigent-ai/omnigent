import { describe, expect, it } from "vitest";

import {
  CODEX_NATIVE_APPROVAL_MODE_LABEL_KEY,
  CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS,
  codexApprovalModeFromSession,
  codexApprovalModeLabel,
} from "@/lib/codexApprovalMode";

describe("codexApprovalMode", () => {
  it("offers the runtime /permissions superset, in Codex's popup order", () => {
    // Newer builds add "Read Only" after "Full Access"; 0.146 lacks it. Only
    // the full-bypass sandbox stance stays launch-only (the server 400s it on
    // a runtime switch), so it's absent here.
    expect(CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS.map((m) => m.value)).toEqual([
      "ask-for-approval",
      "approve-for-me",
      "full-access",
      "read-only",
    ]);
  });

  describe("codexApprovalModeLabel", () => {
    it("labels the runtime presets", () => {
      expect(codexApprovalModeLabel("ask-for-approval")).toBe("Ask for approval");
      expect(codexApprovalModeLabel("approve-for-me")).toBe("Approve for me");
      expect(codexApprovalModeLabel("full-access")).toBe("Full Access");
      expect(codexApprovalModeLabel("read-only")).toBe("Read Only");
    });

    it("falls back to the raw value for an unknown mode and empty for none", () => {
      expect(codexApprovalModeLabel("someFutureMode")).toBe("someFutureMode");
      expect(codexApprovalModeLabel(null)).toBe("");
      expect(codexApprovalModeLabel("")).toBe("");
    });
  });

  describe("codexApprovalModeFromSession", () => {
    it("returns the label the server stamps after a confirmed switch", () => {
      expect(
        codexApprovalModeFromSession({
          labels: { [CODEX_NATIVE_APPROVAL_MODE_LABEL_KEY]: "approve-for-me" },
        }),
      ).toBe("approve-for-me");
    });

    it("returns null when the label is absent — it never guesses from launch args", () => {
      // Runtime approval no longer rides terminal_launch_args, so args that
      // look like a preset must not resolve to one; the picker stays unset
      // until a real switch or a TUI-observed value arrives.
      expect(codexApprovalModeFromSession({})).toBeNull();
      expect(codexApprovalModeFromSession(null)).toBeNull();
      expect(codexApprovalModeFromSession({ labels: {} })).toBeNull();
      expect(
        codexApprovalModeFromSession({
          labels: { "omnigent.codex_native.bypass_sandbox": "1" },
        }),
      ).toBeNull();
    });
  });
});
