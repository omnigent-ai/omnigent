import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { emitBrowserActionRequest } from "@/lib/browserActionBus";
import type * as NativeBridgeModule from "@/lib/nativeBridge";
import { useRevealBrowserOnAgentTabs } from "./useRevealBrowserOnAgentTabs";

const { supportsBrowser } = vi.hoisted(() => ({ supportsBrowser: vi.fn(() => true) }));
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridgeModule>()),
  supportsBrowser,
}));

beforeEach(() => supportsBrowser.mockReturnValue(true));
afterEach(cleanup);

const request = (action: string, args: Record<string, unknown>, conversationId: string) =>
  emitBrowserActionRequest(
    { type: "browser_action_request", actionId: "a1", action, args },
    conversationId,
  );

it("reveals the pane when an agent opens or selects a tab in the shown session", () => {
  const reveal = vi.fn();
  renderHook(() => useRevealBrowserOnAgentTabs("conv_a", reveal));
  request("tabs", { action: "new" }, "conv_a");
  request("tabs", { action: "select", tab_id: "t1" }, "conv_a");
  expect(reveal).toHaveBeenCalledTimes(2);
  // Listing or closing tabs, other actions and other sessions leave the pane alone.
  request("tabs", { action: "list" }, "conv_a");
  request("tabs", { action: "close" }, "conv_a");
  request("click", { selector: "#x" }, "conv_a");
  request("tabs", { action: "new" }, "conv_b");
  expect(reveal).toHaveBeenCalledTimes(2);
});

it("leaves a viewer without the embedded browser alone", () => {
  // A web viewer of the same session gets the desktop agent's requests too.
  supportsBrowser.mockReturnValue(false);
  const reveal = vi.fn();
  renderHook(() => useRevealBrowserOnAgentTabs("conv_a", reveal));
  request("tabs", { action: "new" }, "conv_a");
  request("tabs", { action: "select", tab_id: "t1" }, "conv_a");
  expect(reveal).not.toHaveBeenCalled();
});
