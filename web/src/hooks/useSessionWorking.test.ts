import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { conversationRegistry } from "@/store/conversationRegistry";
import { useSessionWorking } from "./useSessionWorking";

const SESSION = "d".repeat(32);

afterEach(() => {
  cleanup();
  conversationRegistry.clear();
});

describe("useSessionWorking", () => {
  it("counts a streamed session's sandbox launch until the sandbox is up or fails", () => {
    const entry = conversationRegistry.acquire(SESSION);
    entry.setState({
      sessionStatus: "idle",
      sandboxStatus: { stage: "provisioning", error: null },
    });
    const { result } = renderHook(() => useSessionWorking(SESSION));
    expect(result.current).toBe(true);

    act(() => entry.setState({ sandboxStatus: { stage: "failed", error: "No capacity" } }));
    expect(result.current).toBe(false);

    act(() => entry.setState({ sandboxStatus: { stage: "connecting", error: null } }));
    expect(result.current).toBe(true);

    // A `ready` stage clears the launch.
    act(() => entry.setState({ sandboxStatus: null }));
    expect(result.current).toBe(false);
  });
});
