import { describe, expect, it } from "vitest";
import {
  CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY,
  codexApprovalModeFromSession,
} from "./codexApprovalMode";

describe("codexApprovalModeFromSession", () => {
  it("reports the launch-only bypass label ahead of preset args", () => {
    expect(
      codexApprovalModeFromSession({
        labels: { [CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY]: "1" },
        terminalLaunchArgs: ["--sandbox", "read-only", "--ask-for-approval", "on-request"],
      }),
    ).toBe("bypass");
  });

  it("reports the persisted preset after bypass is cleared", () => {
    expect(
      codexApprovalModeFromSession({
        labels: {},
        terminalLaunchArgs: ["--sandbox", "read-only", "--ask-for-approval", "on-request"],
      }),
    ).toBe("read-only");
  });
});
