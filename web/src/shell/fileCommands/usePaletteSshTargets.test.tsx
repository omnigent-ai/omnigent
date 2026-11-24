import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const asked = vi.hoisted(() => [] as [string | null, string | null][]);
const focused = vi.hoisted(() => ({
  target: null as { hostId: string; conversationId: string } | null,
}));

vi.mock("./useFileCommandFacts", () => ({
  useFileSshTargetDemand: (
    hostId: string | null,
    conversationId: string | null,
    wanted: boolean,
  ) => {
    if (wanted) asked.push([hostId, conversationId]);
  },
}));
vi.mock("./focusedFile", () => ({ useFocusedFile: () => focused.target }));
vi.mock("../RevealInFileManager", () => ({
  useSessionHostId: (conversationId: string | null) =>
    conversationId ? `host_of_${conversationId}` : null,
}));

import { usePaletteSshTargets } from "./usePaletteSshTargets";

beforeEach(() => {
  asked.length = 0;
  focused.target = null;
});

describe("usePaletteSshTargets", () => {
  it("asks for nothing while ⌘K is closed", () => {
    renderHook(() => usePaletteSshTargets(false, "conv_1"));
    expect(asked).toEqual([]);
  });

  it("asks for the focused file's host and the session's host as ⌘K opens", () => {
    focused.target = { hostId: "host_other", conversationId: "conv_other" };
    const { rerender } = renderHook(({ open }) => usePaletteSshTargets(open, "conv_1"), {
      initialProps: { open: false },
    });
    rerender({ open: true });
    expect(asked).toEqual([
      ["host_other", "conv_other"],
      ["host_of_conv_1", "conv_1"],
    ]);
  });
});
