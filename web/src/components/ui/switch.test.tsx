import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Switch } from "./switch";
import { setOmnigentHostConfig } from "@/lib/host";

afterEach(() => {
  cleanup();
  setOmnigentHostConfig({});
});

describe("Switch analytics (componentId)", () => {
  it("reports the new checked state and still calls the caller's onCheckedChange", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    const onCheckedChange = vi.fn();
    render(
      <Switch
        componentId="settings.translucent_sidebar"
        onCheckedChange={onCheckedChange}
        aria-label="Toggle"
      />,
    );
    fireEvent.click(screen.getByRole("switch"));
    // Boolean carries no PII, so the value is forwarded.
    expect(analytics).toHaveBeenCalledExactlyOnceWith({
      type: "value_change",
      componentId: "settings.translucent_sidebar",
      componentKind: "toggle",
      value: true,
    });
    expect(onCheckedChange).toHaveBeenCalledExactlyOnceWith(true);
  });

  it("emits nothing when componentId is absent", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    render(<Switch aria-label="Toggle" />);
    fireEvent.click(screen.getByRole("switch"));
    expect(analytics).not.toHaveBeenCalled();
  });
});
