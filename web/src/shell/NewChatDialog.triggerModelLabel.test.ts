import { describe, expect, it } from "vitest";

import { compactHarnessTriggerValue } from "./NewChatDialog";

// The landing composer's pill / harness-row text must name a model exactly as
// the Models flyout does (the catalog display name), so one selection never
// reads two different ways across composer surfaces.
describe("compactHarnessTriggerValue", () => {
  it("keeps a context-qualified display name verbatim", () => {
    expect(compactHarnessTriggerValue("Fable 5.1 (1M context)")).toBe("Fable 5.1 (1M context)");
  });

  it("passes plain display names and knob values through unchanged", () => {
    expect(compactHarnessTriggerValue("Sonnet 5")).toBe("Sonnet 5");
    expect(compactHarnessTriggerValue("High")).toBe("High");
    expect(compactHarnessTriggerValue("Default")).toBe("Default");
  });

  it("unwraps the Default sentinel to the model it resolves to", () => {
    expect(compactHarnessTriggerValue("Default (Sonnet 5)")).toBe("Sonnet 5");
  });

  it("unwraps the Default sentinel without dropping a context qualifier", () => {
    expect(compactHarnessTriggerValue("Default (Fable 5.1 (1M context))")).toBe(
      "Fable 5.1 (1M context)",
    );
  });
});
