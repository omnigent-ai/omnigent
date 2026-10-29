import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { GlobalShortcutSetting } from "@/lib/nativeBridge";
import { GlobalShortcutControl } from "./GlobalShortcutControl";

const SWITCH_NAME = "Show Omnigent from anywhere";
const RECORDER_TEST_ID = "global-shortcut-recorder";

/** Installs a fake Electron preload with the shortcut calls; `saveResult` is what a save resolves to. */
function installBridge(
  setting: GlobalShortcutSetting = { enabled: false, accelerator: "Alt+Space" },
  saveResult: { ok: boolean; error?: string } = { ok: true },
) {
  const bridge = {
    kind: "electron",
    setBadgeCount: vi.fn(),
    notify: vi.fn(),
    getGlobalShortcut: vi.fn().mockResolvedValue(setting),
    setGlobalShortcut: vi.fn().mockResolvedValue(saveResult),
  };
  (window as unknown as Record<string, unknown>).omnigentDesktop = bridge;
  return bridge;
}

afterEach(() => {
  cleanup();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

describe("GlobalShortcutControl", () => {
  it("renders nothing without the desktop bridge", async () => {
    const { container } = render(<GlobalShortcutControl />);
    await act(async () => {});
    expect(container).toBeEmptyDOMElement();
  });

  it("saves Alt+Space when the switch is turned on", async () => {
    const bridge = installBridge();
    render(<GlobalShortcutControl />);
    const toggle = await screen.findByRole("switch", { name: SWITCH_NAME });

    fireEvent.click(toggle);

    await waitFor(() => expect(toggle).toBeChecked());
    expect(bridge.setGlobalShortcut).toHaveBeenCalledWith({
      enabled: true,
      accelerator: "Alt+Space",
    });
  });

  it.each([
    ["Alt+Space", { code: "Space", altKey: true }],
    ["Control+Shift+K", { code: "KeyK", ctrlKey: true, shiftKey: true }],
  ])("records %s from the keyboard", async (accelerator, keys) => {
    const bridge = installBridge({ enabled: true, accelerator: "Alt+J" });
    render(<GlobalShortcutControl />);
    fireEvent.click(await screen.findByTestId(RECORDER_TEST_ID));
    expect(screen.getByTestId(RECORDER_TEST_ID)).toHaveTextContent("Press a shortcut…");

    fireEvent.keyDown(window, keys);

    await waitFor(() =>
      expect(bridge.setGlobalShortcut).toHaveBeenCalledWith({ enabled: true, accelerator }),
    );
  });

  it("ignores chords without Ctrl, Alt, or Cmd, and Escape cancels recording", async () => {
    const bridge = installBridge({ enabled: true, accelerator: "Alt+J" });
    render(<GlobalShortcutControl />);
    fireEvent.click(await screen.findByTestId(RECORDER_TEST_ID));

    fireEvent.keyDown(window, { code: "KeyK", shiftKey: true });
    expect(screen.getByTestId(RECORDER_TEST_ID)).toHaveTextContent("Press a shortcut…");

    fireEvent.keyDown(window, { key: "Escape", code: "Escape" });
    expect(screen.getByTestId(RECORDER_TEST_ID)).toHaveTextContent("Alt+J");
    expect(bridge.setGlobalShortcut).not.toHaveBeenCalled();
  });

  it("shows the shell's error and keeps the saved chord when the save is refused", async () => {
    installBridge(
      { enabled: false, accelerator: "Alt+Space" },
      {
        ok: false,
        error: "Couldn't use that shortcut. It may be invalid or already in use by another app.",
      },
    );
    render(<GlobalShortcutControl />);
    const toggle = await screen.findByRole("switch", { name: SWITCH_NAME });

    fireEvent.click(toggle);

    expect(await screen.findByRole("alert")).toHaveTextContent("already in use by another app");
    expect(toggle).not.toBeChecked();
    expect(screen.getByTestId(RECORDER_TEST_ID)).toHaveTextContent("Alt+Space");
  });
});
