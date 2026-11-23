import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useLivePreview } from "./useLivePreview";

function setup(committed = "saved") {
  const apply = vi.fn();
  const commit = vi.fn();
  const readCommitted = vi.fn(() => committed);
  const view = renderHook(
    ({ active }: { active: boolean }) => useLivePreview({ active, readCommitted, apply, commit }),
    { initialProps: { active: true } },
  );
  return { ...view, apply, commit, readCommitted };
}

/** Runs pending animation frames. */
async function nextFrame() {
  await act(
    () =>
      new Promise<void>((resolve) => {
        requestAnimationFrame(() => resolve());
      }),
  );
}

afterEach(() => vi.restoreAllMocks());

describe("useLivePreview", () => {
  it("previews while active and reverts when it deactivates", async () => {
    const { result, rerender, apply } = setup();
    result.current.preview("other");
    await nextFrame();
    expect(apply.mock.calls).toEqual([["other"]]);
    rerender({ active: false });
    expect(apply.mock.calls).toEqual([["other"], ["saved"]]);
    // Inactive, previews do nothing.
    result.current.preview("ignored");
    await nextFrame();
    expect(apply).toHaveBeenCalledTimes(2);
  });

  it("reads the committed value again for each activation", () => {
    const { rerender, readCommitted } = setup();
    rerender({ active: false });
    rerender({ active: true });
    expect(readCommitted).toHaveBeenCalledTimes(2);
  });

  it("reverts on unmount and leaves a committed value alone", async () => {
    const first = setup();
    first.result.current.preview("other");
    await nextFrame();
    first.unmount();
    expect(first.apply).toHaveBeenLastCalledWith("saved");

    const second = setup();
    second.result.current.commit("chosen");
    second.unmount();
    expect(second.commit.mock.calls).toEqual([["chosen"]]);
    expect(second.apply).not.toHaveBeenCalled();
  });

  it("reverts while the window is in the background and restores the preview on focus", async () => {
    const { result, apply, unmount } = setup();
    result.current.preview("other");
    await nextFrame();
    act(() => {
      window.dispatchEvent(new Event("blur"));
    });
    expect(apply).toHaveBeenLastCalledWith("saved");
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    await nextFrame();
    expect(apply).toHaveBeenLastCalledWith("other");
    unmount();
    // Listeners go with the transaction.
    apply.mockClear();
    window.dispatchEvent(new Event("focus"));
    await nextFrame();
    expect(apply).not.toHaveBeenCalled();
  });
});
