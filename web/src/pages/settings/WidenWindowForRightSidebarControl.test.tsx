import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { WidenWindowForRightSidebarControl } from "./WidenWindowForRightSidebarControl";

const toastError = vi.hoisted(() => vi.fn());
vi.mock("sonner", () => ({ toast: { error: toastError } }));

const SWITCH_NAME = "Widen window when opening the right sidebar";

function installBridge(saved: boolean, saves = true) {
  const rightSidebarWindow = {
    getWidensWindow: vi.fn().mockResolvedValue(saved),
    setWidensWindow: vi.fn().mockResolvedValue(saves),
    opened: vi.fn(),
    closed: vi.fn(),
  };
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    setBadgeCount: vi.fn(),
    notify: vi.fn(),
    rightSidebarWindow,
  };
  return rightSidebarWindow;
}

const renderControl = () =>
  render(<WidenWindowForRightSidebarControl />, { wrapper: MemoryRouter });

afterEach(() => {
  cleanup();
  toastError.mockReset();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

describe("WidenWindowForRightSidebarControl", () => {
  it("shows the shell's saved setting", async () => {
    installBridge(true);
    renderControl();
    const toggle = screen.getByRole("switch", { name: SWITCH_NAME });
    // Disabled until the shell answers.
    expect(toggle).toBeDisabled();
    await waitFor(() => expect(toggle).toBeChecked());
    expect(toggle).toBeEnabled();
  });

  it("saves the setting in the shell when turned on", async () => {
    const bridge = installBridge(false);
    renderControl();
    const toggle = screen.getByRole("switch", { name: SWITCH_NAME });
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(toggle).not.toBeChecked();

    await act(async () => fireEvent.click(toggle));

    expect(bridge.setWidensWindow).toHaveBeenCalledWith(true);
    expect(toggle).toBeChecked();
    expect(toastError).not.toHaveBeenCalled();
  });

  it("reverts and says so when the shell can't save it", async () => {
    installBridge(false, false);
    renderControl();
    const toggle = screen.getByRole("switch", { name: SWITCH_NAME });
    await waitFor(() => expect(toggle).toBeEnabled());

    await act(async () => fireEvent.click(toggle));

    expect(toggle).not.toBeChecked();
    expect(toastError).toHaveBeenCalledWith("Couldn't save this setting. Try again.");
  });
});
