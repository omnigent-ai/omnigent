import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { usePaletteCommandsStore } from "@/store/paletteCommands";

const rows = () =>
  Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) => group.commands);

const chunk = vi.hoisted(() => ({ fails: true }));
vi.mock("./model", async (importOriginal) => {
  if (chunk.fails) throw new Error("chunk failed to load");
  return importOriginal();
});

afterEach(() => {
  cleanup();
  usePaletteCommandsStore.setState({ groups: {} });
  vi.restoreAllMocks();
});

describe("useWorkspaceFullScreenCommand when its code fails to load", () => {
  it("says so, offers nothing, and loads again on the next mount", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { useWorkspaceFullScreenCommand } = await import("./useWorkspaceFullScreenCommand");
    const props = { conversationId: "conv", fullScreen: false, toggle: vi.fn() };
    const first = renderHook(() => useWorkspaceFullScreenCommand(props, true));
    await waitFor(() =>
      expect(warn).toHaveBeenCalledWith(
        "[workspace] full screen is unavailable:",
        expect.any(Error),
      ),
    );
    expect(rows()).toEqual([]);
    first.unmount();

    chunk.fails = false;
    vi.resetModules();
    renderHook(() => useWorkspaceFullScreenCommand(props, true));
    await waitFor(() => expect(rows()).toHaveLength(1));
  });
});
