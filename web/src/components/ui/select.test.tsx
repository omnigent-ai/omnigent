import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "./select";
import { setOmnigentHostConfig } from "@/lib/host";

afterEach(() => {
  cleanup();
  setOmnigentHostConfig({});
});

function renderSelect(props: {
  componentId?: string;
  valueHasNoPii?: boolean;
  onValueChange?: (v: string) => void;
}) {
  return render(
    <Select {...props}>
      <SelectTrigger aria-label="Effort">
        <SelectValue placeholder="Pick" />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="low">Low</SelectItem>
        <SelectItem value="high">High</SelectItem>
      </SelectContent>
    </Select>,
  );
}

function pickHigh() {
  fireEvent.click(screen.getByRole("combobox"));
  fireEvent.click(screen.getByRole("option", { name: "High" }));
}

describe("Select analytics (componentId)", () => {
  it("forwards the value when the call site declares it PII-free", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    const onValueChange = vi.fn();
    renderSelect({ componentId: "chat.composer.effort", valueHasNoPii: true, onValueChange });
    pickHigh();
    expect(analytics).toHaveBeenLastCalledWith({
      type: "value_change",
      componentId: "chat.composer.effort",
      componentKind: "select",
      value: "high",
    });
    expect(onValueChange).toHaveBeenLastCalledWith("high");
  });

  it("redacts the value by default (no valueHasNoPii)", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    renderSelect({ componentId: "chat.composer.effort" });
    pickHigh();
    expect(analytics).toHaveBeenLastCalledWith({
      type: "value_change",
      componentId: "chat.composer.effort",
      componentKind: "select",
      value: undefined,
    });
  });

  it("emits nothing when componentId is absent", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    renderSelect({});
    pickHigh();
    expect(analytics).not.toHaveBeenCalled();
  });
});
