import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useGoalRefreshOnTurnChange } from "./useGoalRefreshOnTurnChange";
import type { SessionStatus } from "@/lib/types";

function render(
  sessionId: string | null,
  sessionStatus: SessionStatus,
  enabled: boolean,
  onRefresh: () => void,
) {
  return renderHook(
    ({ id, status, en }: { id: string | null; status: SessionStatus; en: boolean }) =>
      useGoalRefreshOnTurnChange(id, status, en, onRefresh),
    { initialProps: { id: sessionId, status: sessionStatus, en: enabled } },
  );
}

describe("useGoalRefreshOnTurnChange", () => {
  it("calls onRefresh when status transitions running → idle", () => {
    const onRefresh = vi.fn();
    const { rerender } = render("conv", "running", true, onRefresh);
    expect(onRefresh).not.toHaveBeenCalled();

    rerender({ id: "conv", status: "idle", en: true });
    expect(onRefresh).toHaveBeenCalledOnce();
  });

  it("calls onRefresh when status transitions waiting → idle", () => {
    const onRefresh = vi.fn();
    const { rerender } = render("conv", "waiting", true, onRefresh);

    rerender({ id: "conv", status: "idle", en: true });
    expect(onRefresh).toHaveBeenCalledOnce();
  });

  it("calls onRefresh on idle → running so a typed /goal shows as the turn starts", () => {
    const onRefresh = vi.fn();
    const { rerender } = render("conv", "idle", true, onRefresh);

    rerender({ id: "conv", status: "running", en: true });
    expect(onRefresh).toHaveBeenCalledOnce();
  });

  it("does not call onRefresh for running ↔ waiting within a turn", () => {
    const onRefresh = vi.fn();
    const { rerender } = render("conv", "running", true, onRefresh);

    rerender({ id: "conv", status: "waiting", en: true });
    rerender({ id: "conv", status: "running", en: true });
    expect(onRefresh).not.toHaveBeenCalled();
  });

  it("does not call onRefresh when disabled (non-codex-native sessions)", () => {
    const onRefresh = vi.fn();
    const { rerender } = render("conv", "running", false, onRefresh);

    rerender({ id: "conv", status: "idle", en: false });
    expect(onRefresh).not.toHaveBeenCalled();
  });

  it("does not call onRefresh on session switch (prevents cross-session false positive)", () => {
    const onRefresh = vi.fn();
    // Session A is running; switching to session B which is idle.
    const { rerender } = render("conv_a", "running", true, onRefresh);

    rerender({ id: "conv_b", status: "idle", en: true });
    expect(onRefresh).not.toHaveBeenCalled();
  });
});
