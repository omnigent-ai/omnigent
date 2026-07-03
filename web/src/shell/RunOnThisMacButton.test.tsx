// Unit tests for RunOnThisMacButton — the one-click "spawn a host on this Mac"
// affordance shown inside the Electron desktop shell.
//
// What we lock:
//   1. Outside the desktop shell → renders nothing.
//   2. Shell reports no capability (null status) → renders nothing.
//   3. Runtime not installed → a non-actionable hint, not the active button.
//   4. Click → spinner, then on a successful spawn it polls and flips to
//      "online", calling onHostOnline.
//   5. A failed spawn surfaces the error.
//   6. needsLogin shows a soft warning but leaves the button enabled.
//
// The native bridge is mocked: this pins the component's own state machine,
// not the IPC plumbing (covered by nativeBridge.test.ts) or the daemon spawn
// (covered by localHost/e2e).

import { cleanup, fireEvent, render, screen, waitFor, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RunOnThisMacButton } from "./RunOnThisMacButton";
import {
  getLocalHostStatus,
  isElectronShell,
  startLocalHost,
  type LocalHostStatus,
} from "@/lib/nativeBridge";

vi.mock("@/lib/nativeBridge", () => ({
  isElectronShell: vi.fn(),
  getLocalHostStatus: vi.fn(),
  startLocalHost: vi.fn(),
}));

const mockIsElectron = vi.mocked(isElectronShell);
const mockGetStatus = vi.mocked(getLocalHostStatus);
const mockStart = vi.mocked(startLocalHost);

function status(overrides: Partial<LocalHostStatus> = {}): LocalHostStatus {
  return {
    running: false,
    runtimeAvailable: true,
    mode: "local",
    needsLogin: false,
    serverUrl: "http://localhost:6767",
    logPath: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  // Default: in the desktop shell, runtime available, no hosts online yet.
  mockIsElectron.mockReturnValue(true);
  mockGetStatus.mockResolvedValue(status());
  mockStart.mockResolvedValue({ ok: true, logPath: "/tmp/d.log" });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("RunOnThisMacButton", () => {
  it("renders nothing outside the desktop shell", async () => {
    mockIsElectron.mockReturnValue(false);
    const { container } = render(<RunOnThisMacButton serverUrl="http://localhost:6767" />);
    await waitFor(() => expect(mockIsElectron).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByTestId("run-on-this-mac")).toBeNull();
  });

  it("renders nothing when the shell reports no capability", async () => {
    mockGetStatus.mockResolvedValue(null);
    const { container } = render(<RunOnThisMacButton serverUrl="http://localhost:6767" />);
    await waitFor(() => expect(mockGetStatus).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("shows a hint (not the active button) when the runtime is missing", async () => {
    mockGetStatus.mockResolvedValue(status({ runtimeAvailable: false }));
    render(<RunOnThisMacButton serverUrl="http://localhost:6767" />);
    expect(await screen.findByTestId("run-on-this-mac-no-runtime")).toBeInTheDocument();
    expect(screen.queryByTestId("run-on-this-mac")).toBeNull();
  });

  it("starts a host on click, then flips to online and calls onHostOnline", async () => {
    vi.useFakeTimers();
    const onHostOnline = vi.fn();
    render(<RunOnThisMacButton serverUrl="http://localhost:6767" onHostOnline={onHostOnline} />);
    // Let the mount-time capability probe resolve.
    await act(async () => {
      await Promise.resolve();
    });
    const button = screen.getByTestId("run-on-this-mac");

    // Click → spawn. After the spawn resolves, the component enters polling.
    // Make the next status poll report the host as running.
    mockGetStatus.mockResolvedValue(status({ running: true }));
    await act(async () => {
      fireEvent.click(button);
      await Promise.resolve();
    });
    expect(mockStart).toHaveBeenCalledWith("http://localhost:6767");

    // Advance to the first poll tick.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1600);
    });
    expect(screen.getByTestId("run-on-this-mac-online")).toBeInTheDocument();
    expect(onHostOnline).toHaveBeenCalledTimes(1);
  });

  it("surfaces an error when the spawn fails", async () => {
    mockStart.mockResolvedValue({ ok: false, error: "No CLI found" });
    render(<RunOnThisMacButton serverUrl="http://localhost:6767" />);
    const button = await screen.findByTestId("run-on-this-mac");
    await act(async () => {
      fireEvent.click(button);
      await Promise.resolve();
    });
    expect(await screen.findByTestId("run-on-this-mac-error")).toHaveTextContent("No CLI found");
  });

  it("shows a soft login warning but keeps the button enabled", async () => {
    mockGetStatus.mockResolvedValue(status({ mode: "remote", needsLogin: true }));
    render(<RunOnThisMacButton serverUrl="https://example.com" />);
    expect(await screen.findByTestId("run-on-this-mac-needs-login")).toBeInTheDocument();
    expect(screen.getByTestId("run-on-this-mac")).not.toBeDisabled();
  });

  it("seeds straight to online when a host is already running", async () => {
    mockGetStatus.mockResolvedValue(status({ running: true }));
    render(<RunOnThisMacButton serverUrl="http://localhost:6767" />);
    expect(await screen.findByTestId("run-on-this-mac-online")).toBeInTheDocument();
    expect(mockStart).not.toHaveBeenCalled();
  });
});
