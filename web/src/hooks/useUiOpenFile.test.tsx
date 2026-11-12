import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { parseEvent } from "@/lib/sse";
import { handleSessionEvent } from "@/store/chatStore";
import { useUiOpenFile } from "./useUiOpenFile";

function deliver(sessionId: string, path = "docs/guide.md") {
  const event = parseEvent("ui.open_file", { type: "ui.open_file", path });
  expect(event).toEqual({ type: "ui_open_file", path });
  act(() => handleSessionEvent(event!, sessionId));
}

afterEach(cleanup);

it("routes live stream requests only to viewers of the delivering session", () => {
  const first = vi.fn();
  const second = vi.fn();
  renderHook(() => useUiOpenFile("first", true, first));
  renderHook(() => useUiOpenFile("second", true, second));
  deliver("first");
  expect(first).toHaveBeenCalledWith("docs/guide.md");
  expect(second).not.toHaveBeenCalled();
});

it("does not replay background or disabled requests after a switch or remount", () => {
  const open = vi.fn();
  const hook = renderHook(({ id, enabled }) => useUiOpenFile(id, enabled, open), {
    initialProps: { id: "first", enabled: true },
  });
  deliver("second");
  hook.rerender({ id: "second", enabled: true });
  expect(open).not.toHaveBeenCalled();
  deliver("first");
  expect(open).not.toHaveBeenCalled();
  hook.rerender({ id: "second", enabled: false });
  deliver("second");
  hook.rerender({ id: "second", enabled: true });
  expect(open).not.toHaveBeenCalled();
  deliver("second");
  expect(open).toHaveBeenCalledTimes(1);
  hook.unmount();
  deliver("second");
  renderHook(() => useUiOpenFile("second", true, open));
  expect(open).toHaveBeenCalledTimes(1);
});

it("fans out to matching viewers and isolates navigation failures", () => {
  vi.spyOn(console, "warn").mockImplementation(() => {});
  const open = vi.fn();
  renderHook(() =>
    useUiOpenFile("first", true, () => {
      throw new Error("failed");
    }),
  );
  renderHook(() => useUiOpenFile("first", true, open));
  deliver("first");
  expect(open).toHaveBeenCalledTimes(1);
  vi.restoreAllMocks();
});

it.each([undefined, "", 123])("ignores malformed file paths: %s", (path) => {
  expect(parseEvent("ui.open_file", { path })).toBeNull();
});
