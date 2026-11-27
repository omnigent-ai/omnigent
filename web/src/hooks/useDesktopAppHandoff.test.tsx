import { renderHook } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import { useDesktopAppHandoff } from "@/hooks/useDesktopAppHandoff";
import { handOffToDesktopApp } from "@/lib/desktopAppHandoff";

vi.mock("@/lib/desktopAppHandoff", () => ({
  handOffToDesktopApp: vi.fn(),
  subscribeToDesktopAppHandoff: () => () => {},
  handedOverConversationId: () => null,
}));

beforeEach(() => vi.mocked(handOffToDesktopApp).mockReset());

it("hands over the conversation the tab was opened on, and none the user moves to", () => {
  const { rerender } = renderHook(({ id }) => useDesktopAppHandoff(id, true), {
    initialProps: { id: "conv_landing" },
  });
  rerender({ id: "conv_next" });

  expect(vi.mocked(handOffToDesktopApp).mock.calls).toEqual([["conv_landing"]]);
});

it.each([
  ["no conversation", undefined, true],
  // The app would open the plain conversation, not the file or the Canvas view.
  ["a file or Canvas address", "conv_landing", false],
])("leaves a tab opened on %s in the browser", (_label, id, plainConversation) => {
  const { rerender } = renderHook(
    (props: { id: string | undefined }) => useDesktopAppHandoff(props.id, plainConversation),
    { initialProps: { id } },
  );
  // Reaching a conversation later doesn't count as the landing.
  rerender({ id: "conv_later" });

  expect(handOffToDesktopApp).not.toHaveBeenCalled();
});
