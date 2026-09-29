import { describe, expect, it } from "vitest";

import {
  CODEX_NATIVE_APPROVAL_MODE_LABEL_KEY,
  CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS,
  CODEX_NATIVE_RUNTIME_PERMISSION_OPTIONS,
  codexApprovalModeFromSession,
  codexApprovalModeLabel,
} from "@/lib/codexApprovalMode";

describe("codexApprovalMode", () => {
  it("offers the runtime /permissions superset, in Codex's popup order", () => {
    // Newer builds add "Read Only" after "Full Access"; 0.146 lacks it.
    expect(CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS.map((m) => m.value)).toEqual([
      "ask-for-approval",
      "approve-for-me",
      "full-access",
      "read-only",
    ]);
  });

  it("offers the full-bypass stance after the popup presets", () => {
    // The running-session picker adds bypass as its last row, matching the
    // create-time picker; the server delivers it via the Full Access row.
    expect(CODEX_NATIVE_RUNTIME_PERMISSION_OPTIONS.map((m) => m.value)).toEqual([
      "ask-for-approval",
      "approve-for-me",
      "full-access",
      "read-only",
      "bypass",
    ]);
    expect(codexApprovalModeLabel("bypass")).toBe("Bypass approvals & sandbox");
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
    });

    it("reflects an armed bypass label before any switch", () => {
      // A bypass-launched session runs in bypass from its first turn; the
      // pill must say so instead of showing an unset picker.
      expect(
        codexApprovalModeFromSession({
          labels: { "omnigent.codex_native.bypass_sandbox": "1" },
        }),
      ).toBe("bypass");
      // A confirmed switch away from bypass outranks a stale armed label.
      expect(
        codexApprovalModeFromSession({
          labels: {
            "omnigent.codex_native.bypass_sandbox": "1",
            [CODEX_NATIVE_APPROVAL_MODE_LABEL_KEY]: "read-only",
          },
        }),
      ).toBe("read-only");
    });
  });
});
