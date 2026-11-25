import { describe, expect, it, vi } from "vitest";

import type { ThemeSelection } from "./themePalette";
import { createLivePreview } from "./livePreview";

function setup(committed: ThemeSelection = "omni") {
  const frames = new Map<number, () => void>();
  let nextHandle = 1;
  const apply = vi.fn();
  const commit = vi.fn();
  const preview = createLivePreview<ThemeSelection>({
    committed,
    apply,
    commit,
    requestFrame: (callback) => {
      const handle = nextHandle++;
      frames.set(handle, callback);
      return handle;
    },
    cancelFrame: (handle) => frames.delete(handle),
  });
  const flushFrames = () => {
    const pending = [...frames.values()];
    frames.clear();
    for (const callback of pending) callback();
  };
  return { preview, apply, commit, flushFrames, frames };
}

describe("createLivePreview", () => {
  it("applies a highlighted palette on the next frame", () => {
    const { preview, apply, flushFrames } = setup();
    preview.preview("github");
    expect(apply).not.toHaveBeenCalled();
    flushFrames();
    expect(apply).toHaveBeenCalledWith("github");
  });

  it("paints only the last palette of a fast sweep", () => {
    const { preview, apply, flushFrames, frames } = setup();
    preview.preview("github");
    preview.preview("nord");
    preview.preview("dracula");
    expect(frames.size).toBe(1);
    flushFrames();
    expect(apply.mock.calls).toEqual([["dracula"]]);
  });

  it("skips repainting the palette already shown", () => {
    const { preview, apply, flushFrames } = setup();
    preview.preview(null);
    preview.preview("omni");
    flushFrames();
    expect(apply).not.toHaveBeenCalled();
  });

  it("previews the committed palette for a row that isn't a palette", () => {
    const { preview, apply, flushFrames } = setup("solarized");
    preview.preview("github");
    flushFrames();
    preview.preview(null);
    flushFrames();
    expect(apply.mock.calls).toEqual([["github"], ["solarized"]]);
  });

  it("restores the committed palette on cancel and drops a pending preview", () => {
    const { preview, apply, commit, flushFrames } = setup("nord");
    preview.preview("github");
    flushFrames();
    preview.preview("dracula");
    preview.cancel();
    flushFrames();
    expect(apply.mock.calls).toEqual([["github"], ["nord"]]);
    expect(commit).not.toHaveBeenCalled();
  });

  it("does not repaint on cancel when nothing was previewed", () => {
    const { preview, apply } = setup();
    preview.cancel();
    expect(apply).not.toHaveBeenCalled();
  });

  it("commits once and ignores everything after the transaction ends", () => {
    const { preview, apply, commit, flushFrames } = setup();
    preview.preview("github");
    preview.commit("catppuccin");
    flushFrames();
    preview.preview("nord");
    preview.cancel();
    preview.commit("nord");
    flushFrames();
    expect(commit.mock.calls).toEqual([["catppuccin"]]);
    expect(apply).not.toHaveBeenCalled();
  });

  it("shows the committed value right away while suspended and the latest preview on resume", () => {
    const { preview, apply, flushFrames, frames } = setup("nord");
    preview.preview("github");
    flushFrames();
    preview.preview("dracula");
    preview.suspend();
    expect(frames.size).toBe(0);
    expect(apply.mock.calls).toEqual([["github"], ["nord"]]);
    // Highlights while suspended are remembered, not painted.
    preview.preview("catppuccin");
    flushFrames();
    expect(apply.mock.calls).toHaveLength(2);
    preview.resume();
    flushFrames();
    expect(apply.mock.calls).toEqual([["github"], ["nord"], ["catppuccin"]]);
  });

  it("ignores resume without suspend and keeps cancel after suspend from repainting", () => {
    const { preview, apply, flushFrames } = setup("nord");
    preview.resume();
    flushFrames();
    expect(apply).not.toHaveBeenCalled();
    preview.preview("github");
    flushFrames();
    preview.suspend();
    preview.cancel();
    preview.resume();
    flushFrames();
    expect(apply.mock.calls).toEqual([["github"], ["nord"]]);
  });
});
