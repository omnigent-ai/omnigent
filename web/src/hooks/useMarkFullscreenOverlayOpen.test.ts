import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { setEmbedScopeRoot } from "@/lib/host";
import {
  FULLSCREEN_OVERLAY_OPEN_ATTRIBUTE,
  useMarkFullscreenOverlayOpen,
} from "./useMarkFullscreenOverlayOpen";

afterEach(() => {
  cleanup();
  setEmbedScopeRoot(null);
});

it("keeps the root marked until the last of overlapping full-window views closes", () => {
  const root = document.documentElement;
  const first = renderHook(({ open }) => useMarkFullscreenOverlayOpen(open), {
    initialProps: { open: false },
  });
  expect(root).not.toHaveAttribute(FULLSCREEN_OVERLAY_OPEN_ATTRIBUTE);

  first.rerender({ open: true });
  const second = renderHook(() => useMarkFullscreenOverlayOpen(true));
  expect(root).toHaveAttribute(FULLSCREEN_OVERLAY_OPEN_ATTRIBUTE);

  first.rerender({ open: false });
  expect(root).toHaveAttribute(FULLSCREEN_OVERLAY_OPEN_ATTRIBUTE);
  second.unmount();
  expect(root).not.toHaveAttribute(FULLSCREEN_OVERLAY_OPEN_ATTRIBUTE);
});

it("marks the embed scope root, which the scoped stylesheet matches for html", () => {
  const scope = document.createElement("div");
  setEmbedScopeRoot(scope);
  const { unmount } = renderHook(() => useMarkFullscreenOverlayOpen(true));
  expect(scope).toHaveAttribute(FULLSCREEN_OVERLAY_OPEN_ATTRIBUTE);
  expect(document.documentElement).not.toHaveAttribute(FULLSCREEN_OVERLAY_OPEN_ATTRIBUTE);

  unmount();
  expect(scope).not.toHaveAttribute(FULLSCREEN_OVERLAY_OPEN_ATTRIBUTE);
});
