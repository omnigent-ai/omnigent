import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import type { GlobalShortcutSaveResult, GlobalShortcutStatus } from "@/lib/nativeBridge";
import { GlobalShortcutControl } from "./GlobalShortcutControl";

const SWITCH_NAME = "Show Omnigent from anywhere";
const renderControl = (props: Parameters<typeof GlobalShortcutControl>[0] = {}) =>
  render(<GlobalShortcutControl {...props} />, { wrapper: MemoryRouter });
const RECORDER_TEST_ID = "global-shortcut-recorder";

/** Installs a fake Electron preload with the shortcut calls; `saveResult` is what a save resolves to. */
function installBridge(
  setting: GlobalShortcutStatus = { enabled: false, accelerator: "Alt+Space" },
  saveResult: GlobalShortcutSaveResult = { ok: true },
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
    const { container } = renderControl();
    await act(async () => {});
    expect(container).toBeEmptyDOMElement();
  });

  it.each([
    ["show", "global-shortcut", {}],
    ["floatingComposer", "floating-composer-shortcut", { floatingComposerShortcut: true }],
  ] as const)(
    "marks the %s shortcut system-wide, since it works from any app",
    async (shortcutId, prefix, features) => {
      (window as unknown as Record<string, unknown>).omnigentDesktop = {
        ...installBridge(),
        getDesktopFeatures: vi.fn().mockResolvedValue(features),
      };
      renderControl({ shortcutId });

      expect(await screen.findByTestId(`${prefix}-system-wide`)).toHaveTextContent("System-wide");
      expect(screen.getByTestId(`${prefix}-recorder`)).toHaveAccessibleDescription(
        /system-wide shortcut.*any app.*background/i,
      );
    },
  );

  it("saves Alt+Space when the switch is turned on", async () => {
    const bridge = installBridge();
    renderControl();
    const toggle = await screen.findByRole("switch", { name: SWITCH_NAME });

    fireEvent.click(toggle);

    await waitFor(() => expect(toggle).toBeChecked());
    expect(bridge.setGlobalShortcut).toHaveBeenCalledWith({
      enabled: true,
      accelerator: "Alt+Space",
    });
  });

  it("turns off without naming a chord, and shows the chord the shell kept", async () => {
    // Another window changed the chord to Control+K after this one loaded.
    const bridge = installBridge(
      { enabled: true, accelerator: "Alt+Space" },
      { ok: true, setting: { enabled: false, accelerator: "Control+K" } },
    );
    renderControl();
    const toggle = await screen.findByRole("switch", { name: SWITCH_NAME });

    fireEvent.click(toggle);

    await waitFor(() => expect(toggle).not.toBeChecked());
    expect(bridge.setGlobalShortcut).toHaveBeenCalledExactlyOnceWith({ enabled: false });
    expect(screen.getByTestId(RECORDER_TEST_ID)).toHaveTextContent(/^Ctrl\+K$|^⌃K$/);
  });

  it("saves a chord recorded while off with the chord, so the shell can ask about it", async () => {
    const bridge = installBridge({ enabled: false, accelerator: "Alt+Space" });
    renderControl();
    fireEvent.click(await screen.findByTestId(RECORDER_TEST_ID));

    fireEvent.keyDown(window, { code: "KeyK", ctrlKey: true, shiftKey: true });

    await waitFor(() =>
      expect(bridge.setGlobalShortcut).toHaveBeenCalledExactlyOnceWith({
        enabled: false,
        accelerator: "Control+Shift+K",
      }),
    );
  });

  it("names the recorder by its chord, and by the prompt while it records", async () => {
    installBridge({ enabled: true, accelerator: "Alt+Space" });
    renderControl();
    const recorder = await screen.findByRole("button", { name: "Shortcut: Alt+Space" });
    expect(recorder).toHaveTextContent("Alt+Space");

    fireEvent.click(recorder);

    expect(screen.getByRole("button", { name: "Shortcut: Press a shortcut…" })).toHaveTextContent(
      "Press a shortcut…",
    );
  });

  it.each([
    ["Alt+Space", { code: "Space", altKey: true }],
    ["Control+Shift+K", { code: "KeyK", ctrlKey: true, shiftKey: true }],
  ])("records %s from the keyboard", async (accelerator, keys) => {
    const bridge = installBridge({ enabled: true, accelerator: "Alt+J" });
    renderControl();
    fireEvent.click(await screen.findByTestId(RECORDER_TEST_ID));
    expect(screen.getByTestId(RECORDER_TEST_ID)).toHaveTextContent("Press a shortcut…");

    fireEvent.keyDown(window, keys);

    await waitFor(() =>
      expect(bridge.setGlobalShortcut).toHaveBeenCalledWith({ enabled: true, accelerator }),
    );
  });

  it("ignores chords without Ctrl, Alt, or Cmd, and Escape cancels recording", async () => {
    const bridge = installBridge({ enabled: true, accelerator: "Alt+J" });
    renderControl();
    fireEvent.click(await screen.findByTestId(RECORDER_TEST_ID));

    fireEvent.keyDown(window, { code: "KeyK", shiftKey: true });
    expect(screen.getByTestId(RECORDER_TEST_ID)).toHaveTextContent("Press a shortcut…");

    fireEvent.keyDown(window, { key: "Escape", code: "Escape" });
    expect(screen.getByTestId(RECORDER_TEST_ID)).toHaveTextContent("Alt+J");
    expect(bridge.setGlobalShortcut).not.toHaveBeenCalled();
  });

  it("records through the shell when it can, so the app's own shortcuts don't also run", async () => {
    const bridge = installBridge({ enabled: true, accelerator: "Alt+J" });
    let finish: (accelerator: string | null) => void = () => {};
    const recordGlobalShortcut = vi.fn(
      () =>
        new Promise<string | null>((resolve) => {
          finish = resolve;
        }),
    );
    Object.assign(bridge, { recordGlobalShortcut, cancelGlobalShortcutRecording: vi.fn() });
    renderControl();
    fireEvent.click(await screen.findByTestId(RECORDER_TEST_ID));
    expect(recordGlobalShortcut).toHaveBeenCalledTimes(1);

    // The shell takes the chord before the page sees it, so the page doesn't record one.
    fireEvent.keyDown(window, { code: "KeyK", ctrlKey: true });
    expect(bridge.setGlobalShortcut).not.toHaveBeenCalled();

    await act(async () => finish("Control+Shift+K"));
    expect(bridge.setGlobalShortcut).toHaveBeenCalledWith({
      enabled: true,
      accelerator: "Control+Shift+K",
    });
  });

  it("starts one shell recording however often the recorder is clicked", async () => {
    const bridge = installBridge({ enabled: true, accelerator: "Alt+J" });
    let finish: (accelerator: string | null) => void = () => {};
    const recordGlobalShortcut = vi.fn(
      () =>
        new Promise<string | null>((resolve) => {
          finish = resolve;
        }),
    );
    Object.assign(bridge, { recordGlobalShortcut, cancelGlobalShortcutRecording: vi.fn() });
    renderControl();
    const recorder = await screen.findByTestId(RECORDER_TEST_ID);
    fireEvent.click(recorder);
    fireEvent.click(recorder);
    expect(recordGlobalShortcut).toHaveBeenCalledTimes(1);
    expect(recorder).toHaveTextContent("Press a shortcut…");

    await act(async () => finish("Control+Shift+K"));
    expect(bridge.setGlobalShortcut).toHaveBeenCalledTimes(1);
    expect(recorder).not.toHaveTextContent("Press a shortcut…");
  });

  it.each([
    ["the recorder loses focus", (recorder: HTMLElement) => fireEvent.blur(recorder)],
    ["Settings closes", () => cleanup()],
  ])("saves nothing from a shell recording that ends when %s", async (_when, end) => {
    const bridge = installBridge({ enabled: true, accelerator: "Alt+J" });
    let finish: (accelerator: string | null) => void = () => {};
    const cancelGlobalShortcutRecording = vi.fn().mockResolvedValue(true);
    Object.assign(bridge, {
      recordGlobalShortcut: vi.fn(
        () =>
          new Promise<string | null>((resolve) => {
            finish = resolve;
          }),
      ),
      cancelGlobalShortcutRecording,
    });
    renderControl();
    const recorder = await screen.findByTestId(RECORDER_TEST_ID);
    fireEvent.click(recorder);
    end(recorder);
    expect(cancelGlobalShortcutRecording).toHaveBeenCalledTimes(1);

    // A chord the shell already took before the cancel arrived.
    await act(async () => finish("Control+Shift+K"));
    expect(bridge.setGlobalShortcut).not.toHaveBeenCalled();
  });

  it("cancels the shell's recording when the recorder loses focus", async () => {
    const bridge = installBridge();
    const cancelGlobalShortcutRecording = vi.fn().mockResolvedValue(true);
    Object.assign(bridge, {
      recordGlobalShortcut: vi.fn(() => new Promise(() => {})),
      cancelGlobalShortcutRecording,
    });
    renderControl();
    const recorder = await screen.findByTestId(RECORDER_TEST_ID);
    fireEvent.click(recorder);
    fireEvent.blur(recorder);
    expect(cancelGlobalShortcutRecording).toHaveBeenCalledTimes(1);
    expect(recorder).not.toHaveTextContent("Press a shortcut…");
  });

  it("says so when the saved shortcut is on but isn't working", async () => {
    installBridge({
      enabled: true,
      accelerator: "Alt+Space",
      error: "This shortcut isn't working. Another app may be using it, so choose a different one.",
    });
    renderControl();
    expect(await screen.findByRole("alert")).toHaveTextContent("This shortcut isn't working.");
    expect(screen.getByRole("switch", { name: SWITCH_NAME })).toBeChecked();
  });

  it("names no id for the show shortcut, so the call is the one every shell takes", async () => {
    const bridge = installBridge();
    const recordGlobalShortcut = vi.fn().mockResolvedValue(null);
    Object.assign(bridge, { recordGlobalShortcut, cancelGlobalShortcutRecording: vi.fn() });
    renderControl();

    fireEvent.click(await screen.findByTestId(RECORDER_TEST_ID));

    await waitFor(() => expect(recordGlobalShortcut).toHaveBeenCalledExactlyOnceWith());
    expect(bridge.getGlobalShortcut).toHaveBeenCalledExactlyOnceWith();
  });

  it("reads and saves the New Agent shortcut by its id, once the shell says it takes ids", async () => {
    const bridge = {
      ...installBridge({ enabled: true, accelerator: "CommandOrControl+Shift+Space" }),
      getDesktopFeatures: vi.fn().mockResolvedValue({ floatingComposerShortcut: true }),
    };
    (window as unknown as Record<string, unknown>).omnigentDesktop = bridge;
    renderControl({ shortcutId: "floatingComposer" });
    const toggle = await screen.findByRole("switch", { name: "Open New Agent from anywhere" });

    fireEvent.click(toggle);

    await waitFor(() => expect(toggle).not.toBeChecked());
    expect(bridge.getGlobalShortcut).toHaveBeenCalledExactlyOnceWith("floatingComposer");
    expect(bridge.setGlobalShortcut).toHaveBeenCalledExactlyOnceWith(
      { enabled: false },
      "floatingComposer",
    );
    expect(screen.getByTestId("floating-composer-shortcut-recorder")).toBeInTheDocument();
  });

  it("renders no New Agent row on a shell that would read the show shortcut instead", async () => {
    const bridge = {
      ...installBridge(),
      getDesktopFeatures: vi.fn().mockResolvedValue({ floatingComposer: true }),
    };
    (window as unknown as Record<string, unknown>).omnigentDesktop = bridge;
    const { container } = renderControl({ shortcutId: "floatingComposer" });
    await act(async () => {});

    expect(container).toBeEmptyDOMElement();
    expect(bridge.getGlobalShortcut).not.toHaveBeenCalled();
  });

  it("records the New Agent chord in the shell by id, and shows why a taken one is refused", async () => {
    const error = "Another Omnigent shortcut already uses that chord. Choose a different one.";
    const bridge = {
      ...installBridge(
        { enabled: true, accelerator: "CommandOrControl+Shift+Space" },
        { ok: false, error },
      ),
      getDesktopFeatures: vi.fn().mockResolvedValue({ floatingComposerShortcut: true }),
      recordGlobalShortcut: vi.fn().mockResolvedValue("Alt+Space"),
    };
    (window as unknown as Record<string, unknown>).omnigentDesktop = bridge;
    renderControl({ shortcutId: "floatingComposer" });

    fireEvent.click(await screen.findByTestId("floating-composer-shortcut-recorder"));

    expect(await screen.findByRole("alert")).toHaveTextContent(error);
    expect(bridge.recordGlobalShortcut).toHaveBeenCalledExactlyOnceWith("floatingComposer");
    expect(bridge.setGlobalShortcut).toHaveBeenCalledExactlyOnceWith(
      { enabled: true, accelerator: "Alt+Space" },
      "floatingComposer",
    );
  });

  it("shows the shell's error and keeps the saved chord when the save is refused", async () => {
    installBridge(
      { enabled: false, accelerator: "Alt+Space" },
      {
        ok: false,
        error: "Couldn't use that shortcut. It may be invalid or already in use by another app.",
      },
    );
    renderControl();
    const toggle = await screen.findByRole("switch", { name: SWITCH_NAME });

    fireEvent.click(toggle);

    expect(await screen.findByRole("alert")).toHaveTextContent("already in use by another app");
    expect(toggle).not.toBeChecked();
    expect(screen.getByTestId(RECORDER_TEST_ID)).toHaveTextContent("Alt+Space");
  });

  it("offers the free chord the shell proposed, and saves it on one click", async () => {
    const bridge = installBridge({
      enabled: true,
      accelerator: "Alt+Space",
      error: "Alt+Space isn't working. Another app may be using it.",
      suggestion: "Control+Alt+Shift+Space",
    });
    renderControl();
    expect(await screen.findByRole("alert")).toHaveTextContent("Alt+Space isn't working.");

    fireEvent.click(screen.getByRole("button", { name: /^Use .+ instead$/ }));

    await waitFor(() =>
      expect(bridge.setGlobalShortcut).toHaveBeenCalledExactlyOnceWith({
        enabled: true,
        accelerator: "Control+Alt+Shift+Space",
      }),
    );
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("button", { name: /^Use .+ instead$/ })).toBeNull();
  });

  it("offers a proposed chord when the shell refuses the one that was just turned on", async () => {
    const bridge = installBridge(
      { enabled: false, accelerator: "Alt+Space" },
      { ok: true, setting: { enabled: true, accelerator: "Control+Alt+Shift+Space" } },
    );
    bridge.setGlobalShortcut.mockResolvedValueOnce({
      ok: false,
      error: "Couldn't use Alt+Space. It may be invalid or already in use by another app.",
      suggestion: "Control+Alt+Shift+Space",
    });
    renderControl();
    const toggle = await screen.findByRole("switch", { name: SWITCH_NAME });

    fireEvent.click(toggle);

    expect(await screen.findByRole("alert")).toHaveTextContent("already in use by another app");
    expect(toggle).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: /^Use .+ instead$/ }));

    await waitFor(() => expect(toggle).toBeChecked());
    expect(bridge.setGlobalShortcut).toHaveBeenLastCalledWith({
      enabled: true,
      accelerator: "Control+Alt+Shift+Space",
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("ignores the row's other saves while the proposed chord's save is pending", async () => {
    const bridge = installBridge({
      enabled: true,
      accelerator: "Alt+Space",
      error: "Alt+Space isn't working. Another app may be using it.",
      suggestion: "Control+Alt+Shift+Space",
    });
    let finish: ((result: GlobalShortcutSaveResult) => void) | undefined;
    bridge.setGlobalShortcut.mockImplementationOnce(
      () =>
        new Promise<GlobalShortcutSaveResult>((resolve) => {
          finish = resolve;
        }),
    );
    renderControl();
    const proposal = await screen.findByRole("button", { name: /^Use .+ instead$/ });

    fireEvent.click(proposal);
    // aria-disabled controls still get clicks, so these reach the row's save guard.
    fireEvent.click(proposal);
    fireEvent.click(screen.getByRole("switch", { name: SWITCH_NAME }));
    fireEvent.click(screen.getByTestId(RECORDER_TEST_ID));

    // The whole row waits for the pending approval, and says so.
    expect(proposal).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("switch", { name: SWITCH_NAME })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(screen.getByTestId(RECORDER_TEST_ID)).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByTestId(RECORDER_TEST_ID)).not.toHaveTextContent("Press a shortcut…");
    expect(bridge.setGlobalShortcut).toHaveBeenCalledTimes(1);
    await act(async () => {
      finish?.({ ok: true, setting: { enabled: true, accelerator: "Control+Alt+Shift+Space" } });
    });
    expect(bridge.setGlobalShortcut).toHaveBeenCalledTimes(1);
    // Accepted: the warning and proposal go, and the keyboard lands on the chord it set.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Use .+ instead$/ })).not.toBeInTheDocument();
    expect(screen.getByTestId(RECORDER_TEST_ID)).toHaveTextContent(/Space$/);
    expect(screen.getByTestId(RECORDER_TEST_ID)).toHaveFocus();
  });

  it("still records a chord while off after a refused enable", async () => {
    const bridge = installBridge({ enabled: false, accelerator: "Alt+Space" });
    bridge.setGlobalShortcut.mockResolvedValueOnce({
      ok: false,
      error: "Couldn't use Alt+Space. It may be invalid or already in use by another app.",
      suggestion: "Control+Alt+Shift+Space",
    });
    renderControl();
    fireEvent.click(await screen.findByRole("switch", { name: SWITCH_NAME }));
    await screen.findByRole("alert");

    fireEvent.click(screen.getByTestId(RECORDER_TEST_ID));
    fireEvent.keyDown(window, { code: "KeyK", ctrlKey: true, shiftKey: true });

    await waitFor(() =>
      expect(bridge.setGlobalShortcut).toHaveBeenLastCalledWith({
        enabled: false,
        accelerator: "Control+Shift+K",
      }),
    );
  });
});
