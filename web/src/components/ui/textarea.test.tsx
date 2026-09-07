import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Textarea } from "./textarea";
import { setOmnigentHostConfig } from "@/lib/host";

afterEach(() => {
  cleanup();
  setOmnigentHostConfig({});
});

describe("Textarea analytics (componentId)", () => {
  it("reports a value-change with the value redacted and still calls onChange", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    const onChange = vi.fn();
    render(<Textarea componentId="plan.feedback" onChange={onChange} aria-label="Feedback" />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "some private text" } });
    // Textarea text is PII: only that the field changed is reported, never the value.
    expect(analytics).toHaveBeenCalledExactlyOnceWith({
      type: "value_change",
      componentId: "plan.feedback",
      componentKind: "textarea",
      value: undefined,
    });
    expect(onChange).toHaveBeenCalledOnce();
  });

  it("emits nothing when componentId is absent", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    render(<Textarea aria-label="Feedback" />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "x" } });
    expect(analytics).not.toHaveBeenCalled();
  });
});
