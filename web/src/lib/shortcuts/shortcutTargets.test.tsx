import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { shortcutTargetsStore, useBindShortcutTarget, useShortcutTargets } from "./shortcutTargets";

afterEach(() => {
  cleanup();
  shortcutTargetsStore.setState({ targets: {} });
});

const renameTarget = () => shortcutTargetsStore.getState().targets["session.rename"];

describe("shortcut targets", () => {
  it("reports the target while bound and enabled, and releases it after", () => {
    const { rerender, unmount } = renderHook(
      ({ enabled }) => useBindShortcutTarget("session.rename", "conv-1", enabled),
      { initialProps: { enabled: true } },
    );
    expect(renameTarget()).toBe("conv-1");
    rerender({ enabled: false });
    expect(renameTarget()).toBeUndefined();
    rerender({ enabled: true });
    unmount();
    expect(renameTarget()).toBeUndefined();
  });

  it("follows a change of target and ignores a missing one", () => {
    const { rerender } = renderHook(
      ({ target }: { target: string | null }) => useBindShortcutTarget("session.rename", target),
      { initialProps: { target: "conv-1" as string | null } },
    );
    rerender({ target: "conv-2" });
    expect(renameTarget()).toBe("conv-2");
    rerender({ target: null });
    expect(renameTarget()).toBeUndefined();
  });

  it("keeps a newer binder's target when an older one unmounts", () => {
    const older = renderHook(() => useBindShortcutTarget("session.rename", "conv-1"));
    renderHook(() => useBindShortcutTarget("session.rename", "conv-2"));
    older.unmount();
    expect(renameTarget()).toBe("conv-2");
  });

  it("lets a surface read every target", () => {
    renderHook(() => useBindShortcutTarget("session.rename", "conv-1"));
    const { result } = renderHook(() => useShortcutTargets());
    expect(result.current).toEqual({ "session.rename": "conv-1" });
  });
});
