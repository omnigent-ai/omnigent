import { StrictMode } from "react";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChatStore } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import { useChatSessionBinding } from "./useChatSessionBinding";

const original = useChatStore.getState();
const switchTo = vi.fn<typeof original.switchTo>();
function View({ id }: { id: string }) {
  useChatSessionBinding(id);
  return null;
}
async function flushCleanup() {
  await act(async () => {
    await Promise.resolve();
  });
}

beforeEach(() => {
  switchTo.mockReset().mockImplementation(async (id) => {
    if (id === null) return original.switchTo(null);
    useChatStore.setState({ conversationId: id });
  });
  useChatStore.setState({ ...original, switchTo }, true);
});
afterEach(async () => {
  cleanup();
  await flushCleanup();
  useChatStore.setState(original, true);
  conversationRegistry.clear();
});

describe("visible chat binding", () => {
  it("deactivates a closed chat without disposing its background stream", async () => {
    const { unmount } = render(<View id="a" />);
    const controller = new AbortController();
    useChatStore.setState({ abortController: controller });
    unmount();
    await flushCleanup();
    expect(useChatStore.getState().conversationId).toBeNull();
    expect(controller.signal.aborted).toBe(false);
    expect(conversationRegistry.peek("a")).toBeDefined();
  });

  it("does not clear the incoming session after an in-place switch", async () => {
    const { rerender } = render(<View id="a" />);
    rerender(<View id="b" />);
    await flushCleanup();
    expect(useChatStore.getState().conversationId).toBe("b");
    expect(switchTo).not.toHaveBeenCalledWith(null);
  });

  it("does not clear a replacement view of the same session", async () => {
    const { rerender } = render(<View key="first" id="a" />);
    rerender(<View key="replacement" id="a" />);
    await flushCleanup();
    expect(useChatStore.getState().conversationId).toBe("a");
    expect(switchTo).not.toHaveBeenCalledWith(null);
  });

  it("does not deactivate during StrictMode effect replay", async () => {
    render(
      <StrictMode>
        <View id="a" />
      </StrictMode>,
    );
    await flushCleanup();
    expect(useChatStore.getState().conversationId).toBe("a");
    expect(switchTo).not.toHaveBeenCalledWith(null);
  });

  it("leaves an independently activated incoming session alone", async () => {
    const { unmount } = render(<View id="a" />);
    unmount();
    useChatStore.setState({ conversationId: "b" });
    await flushCleanup();
    expect(useChatStore.getState().conversationId).toBe("b");
  });

  it("can close while a session bind is still pending", async () => {
    let finish = () => {};
    const pending = new Promise<void>((resolve) => {
      finish = resolve;
    });
    switchTo.mockImplementation((id) => {
      if (id === null) return original.switchTo(null);
      useChatStore.setState({ conversationId: id });
      return pending;
    });
    const { unmount } = render(<View id="a" />);
    unmount();
    await flushCleanup();
    expect(useChatStore.getState().conversationId).toBeNull();
    finish();
    await pending;
  });
});
