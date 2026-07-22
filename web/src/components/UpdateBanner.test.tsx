import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ElectronUpdateBridge, UpdateConfig, UpdateStatus } from "@/lib/nativeBridge";
import { UpdateBanner } from "./UpdateBanner";

const DEFAULT_CONFIG: UpdateConfig = {
  mode: "default",
  autoInstall: true,
  skippedVersion: null,
};

function installBridge(status: UpdateStatus, config: UpdateConfig = DEFAULT_CONFIG) {
  let onStatus: ((status: UpdateStatus) => void) | null = null;
  const unsubscribe = vi.fn();
  const bridge: ElectronUpdateBridge = {
    getConfig: vi.fn().mockResolvedValue(config),
    getStatus: vi.fn().mockResolvedValue(status),
    check: vi.fn().mockResolvedValue(undefined),
    download: vi.fn().mockResolvedValue(undefined),
    installNow: vi.fn().mockResolvedValue(undefined),
    setConfig: vi.fn().mockResolvedValue(config),
    onStatus: vi.fn((cb) => {
      onStatus = cb;
      return unsubscribe;
    }),
  };
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    setBadgeCount: vi.fn(),
    notify: vi.fn(),
    updates: bridge,
  };
  return {
    bridge,
    emit: (next: UpdateStatus) => onStatus?.(next),
    unsubscribe,
  };
}

beforeEach(() => {
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

afterEach(() => {
  cleanup();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

describe("UpdateBanner", () => {
  it("renders nothing outside the Electron shell", () => {
    render(<UpdateBanner />);
    expect(screen.queryByRole("region", { name: "Desktop update" })).toBeNull();
  });

  it("renders the correct controls for available, downloading, and downloaded states", async () => {
    const { bridge, emit } = installBridge({
      state: "available",
      info: { version: "0.4.0", releaseNotes: "Fixes and polish." },
    });
    render(<UpdateBanner />);

    expect(await screen.findByText("Omnigent 0.4.0 is available")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Update now" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Skip this version" })).toBeInTheDocument();
    expect(screen.getByText("Release notes")).toBeInTheDocument();
    expect(vi.mocked(bridge.getStatus).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(bridge.onStatus).mock.invocationCallOrder[0],
    );

    emit({ state: "downloading", progress: { percent: 42 } });
    expect(await screen.findByText("Downloading Omnigent update… 42%")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Update now" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Skip this version" })).toBeNull();

    emit({ state: "downloaded", info: { version: "0.4.0" } });
    expect(await screen.findByText("Omnigent 0.4.0 is ready to install")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restart to update" })).toBeInTheDocument();
    expect(screen.getByText("Installs automatically on next quit.")).toBeInTheDocument();
  });

  it("does not promise install-on-quit when auto install is off", async () => {
    installBridge(
      {
        state: "downloaded",
        info: { version: "0.4.0" },
      },
      { ...DEFAULT_CONFIG, autoInstall: false },
    );

    render(<UpdateBanner />);

    expect(await screen.findByText("Omnigent 0.4.0 is ready to install")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restart to update" })).toBeInTheDocument();
    expect(screen.queryByText("Installs automatically on next quit.")).toBeNull();
  });

  it("unsubscribes from update status events when it unmounts", async () => {
    const { unsubscribe } = installBridge({
      state: "available",
      info: { version: "0.4.0" },
    });

    const { unmount } = render(<UpdateBanner />);
    expect(await screen.findByText("Omnigent 0.4.0 is available")).toBeInTheDocument();

    unmount();

    expect(unsubscribe).toHaveBeenCalledTimes(1);
  });

  it("suppresses a skipped version after persisting it", async () => {
    const skippedConfig: UpdateConfig = {
      ...DEFAULT_CONFIG,
      skippedVersion: "0.4.0",
    };
    const { bridge } = installBridge({
      state: "available",
      info: { version: "0.4.0" },
    });
    vi.mocked(bridge.setConfig).mockResolvedValueOnce(skippedConfig);

    render(<UpdateBanner />);
    expect(await screen.findByText("Omnigent 0.4.0 is available")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Skip this version" }));
    await waitFor(() => {
      expect(bridge.setConfig).toHaveBeenCalledWith({ skippedVersion: "0.4.0" });
      expect(screen.queryByText("Omnigent 0.4.0 is available")).toBeNull();
    });
  });
});
