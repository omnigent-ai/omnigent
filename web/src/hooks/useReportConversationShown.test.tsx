import { renderHook } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import {
  type ConversationOnScreen,
  useReportConversationShown,
} from "@/hooks/useReportConversationShown";
import { reportConversationShown } from "@/lib/nativeBridge";

vi.mock("@/lib/nativeBridge", () => ({ reportConversationShown: vi.fn() }));

const reports = () => vi.mocked(reportConversationShown).mock.calls.map(([id]) => id);

/** Conversation A, fully on screen. */
const SHOWN: ConversationOnScreen = {
  conversationId: "conv_a",
  loadedSessionId: "conv_a",
  activeConversationId: "conv_a",
  loading: false,
  loadError: null,
};

beforeEach(() => vi.mocked(reportConversationShown).mockReset());

it("reports a conversation once it is on screen, and withdraws it on leaving", () => {
  const { rerender, unmount } = renderHook(
    (state: ConversationOnScreen) => useReportConversationShown(state),
    {
      initialProps: { ...SHOWN, loadedSessionId: undefined, loading: true } as ConversationOnScreen,
    },
  );
  expect(reports()).toEqual([null]);

  rerender(SHOWN);
  expect(reports().at(-1)).toBe("conv_a");

  // The route moved on while conversation A is still what the page holds.
  rerender({ ...SHOWN, conversationId: "conv_b" });
  expect(reports().at(-1)).toBeNull();

  unmount();
  expect(reports().at(-1)).toBeNull();
});

it.each([
  ["its messages are still loading", { loading: true }],
  ["its messages failed to load", { loadError: new Error("history unavailable") }],
  ["another conversation's messages are still on screen", { activeConversationId: "conv_b" }],
  ["its details haven't loaded", { loadedSessionId: undefined }],
  ["the page shows no conversation", { conversationId: undefined }],
] as const)("reports nothing while %s", (_label, missing) => {
  // The session's details alone say nothing about what is on screen.
  renderHook(() => useReportConversationShown({ ...SHOWN, ...missing }));

  expect(reports()).toEqual([null]);
});
