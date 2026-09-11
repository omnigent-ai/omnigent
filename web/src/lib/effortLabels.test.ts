import { describe, expect, it } from "vitest";

import { formatEffortLabel } from "./effortLabels";

describe("formatEffortLabel", () => {
  it("title-cases plain levels", () => {
    expect(formatEffortLabel("low")).toBe("Low");
    expect(formatEffortLabel("medium")).toBe("Medium");
    expect(formatEffortLabel("high")).toBe("High");
    expect(formatEffortLabel("max")).toBe("Max");
    expect(formatEffortLabel("none")).toBe("None");
  });

  it("camel-cases xhigh regardless of input casing", () => {
    expect(formatEffortLabel("xhigh")).toBe("xHigh");
    expect(formatEffortLabel("xHigh")).toBe("xHigh");
    expect(formatEffortLabel("XHIGH")).toBe("xHigh");
  });

  it("keeps already-cased values stable", () => {
    expect(formatEffortLabel("High")).toBe("High");
  });
});
