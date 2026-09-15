import { describe, expect, it } from "vitest";
import { setupStepDoneDetail } from "./HarnessSetupDialog";

describe("setupStepDoneDetail", () => {
  it("describes local authentication setup without claiming vendor sign-in", () => {
    expect(setupStepDoneDetail("auth")).toBe("Authentication is configured locally on the host.");
  });
});
