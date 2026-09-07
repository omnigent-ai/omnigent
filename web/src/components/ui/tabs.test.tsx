import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "./tabs";
import { setOmnigentHostConfig } from "@/lib/host";

afterEach(() => {
  cleanup();
  setOmnigentHostConfig({});
});

function renderTabs(props: { componentId?: string; onValueChange?: (v: string) => void }) {
  return render(
    <Tabs defaultValue="local" {...props}>
      <TabsList>
        <TabsTrigger value="local">Local</TabsTrigger>
        <TabsTrigger value="lakebox">Lakebox</TabsTrigger>
      </TabsList>
      <TabsContent value="local">local</TabsContent>
      <TabsContent value="lakebox">lakebox</TabsContent>
    </Tabs>,
  );
}

describe("Tabs analytics (componentId)", () => {
  it("reports the selected tab value and still calls the caller's onValueChange", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    const onValueChange = vi.fn();
    renderTabs({ componentId: "new_chat.host_tabs", onValueChange });
    // Radix Tabs (automatic activation) selects on focus; jsdom's synthetic
    // click doesn't move focus, so drive both.
    const tab = screen.getByRole("tab", { name: "Lakebox" });
    fireEvent.focus(tab);
    fireEvent.click(tab);
    // Tab values are a bounded set, so the value is forwarded.
    expect(analytics).toHaveBeenCalledExactlyOnceWith({
      type: "value_change",
      componentId: "new_chat.host_tabs",
      componentKind: "tabs",
      value: "lakebox",
    });
    expect(onValueChange).toHaveBeenCalledExactlyOnceWith("lakebox");
  });

  it("emits nothing when componentId is absent", () => {
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    renderTabs({});
    const tab = screen.getByRole("tab", { name: "Lakebox" });
    fireEvent.focus(tab);
    fireEvent.click(tab);
    expect(analytics).not.toHaveBeenCalled();
  });
});
