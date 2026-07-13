import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useRefreshSessionStateOnRunnerOnline } from "./useSessionOnlineRefresh";

const mocks = vi.hoisted(() => ({
  refreshSessionState: vi.fn(() => Promise.resolve()),
}));

vi.mock("@/store/chatStore", () => ({
  useChatStore: {
    getState: () => ({
      refreshSessionState: mocks.refreshSessionState,
    }),
  },
}));

describe("useRefreshSessionStateOnRunnerOnline", () => {
  beforeEach(() => {
    mocks.refreshSessionState.mockClear();
  });

  it("refreshes the session when runner liveness flips online", () => {
    const { rerender } = renderHook(
      ({ online }: { online: boolean | undefined }) =>
        useRefreshSessionStateOnRunnerOnline("conv_abc", online),
      { initialProps: { online: undefined as boolean | undefined } },
    );

    expect(mocks.refreshSessionState).not.toHaveBeenCalled();

    rerender({ online: true });
    expect(mocks.refreshSessionState).toHaveBeenCalledOnce();
    expect(mocks.refreshSessionState).toHaveBeenLastCalledWith("conv_abc");

    rerender({ online: true });
    expect(mocks.refreshSessionState).toHaveBeenCalledOnce();

    rerender({ online: false });
    rerender({ online: true });
    expect(mocks.refreshSessionState).toHaveBeenCalledTimes(2);
    expect(mocks.refreshSessionState).toHaveBeenLastCalledWith("conv_abc");
  });

  it("refreshes a new conversation even when both runners read online", () => {
    const { rerender } = renderHook(
      ({ conversationId, online }: { conversationId: string; online: boolean | undefined }) =>
        useRefreshSessionStateOnRunnerOnline(conversationId, online),
      { initialProps: { conversationId: "conv_a", online: true } },
    );

    expect(mocks.refreshSessionState).toHaveBeenCalledOnce();
    expect(mocks.refreshSessionState).toHaveBeenLastCalledWith("conv_a");

    rerender({ conversationId: "conv_b", online: true });

    expect(mocks.refreshSessionState).toHaveBeenCalledTimes(2);
    expect(mocks.refreshSessionState).toHaveBeenLastCalledWith("conv_b");
  });

  it("does not refresh while no conversation is active", () => {
    const { rerender } = renderHook(
      ({
        conversationId,
        online,
      }: {
        conversationId: string | null;
        online: boolean | undefined;
      }) => useRefreshSessionStateOnRunnerOnline(conversationId, online),
      { initialProps: { conversationId: null, online: true } },
    );

    rerender({ conversationId: null, online: false });
    rerender({ conversationId: null, online: true });

    expect(mocks.refreshSessionState).not.toHaveBeenCalled();
  });
});
