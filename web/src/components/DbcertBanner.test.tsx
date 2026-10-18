import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DbcertBanner } from "./DbcertBanner";
import type * as HostDbcertModule from "@/lib/hostDbcert";
import { readHostDbcertExpiry, startHostDbcertRefresh } from "@/lib/hostDbcert";

const NOW = Date.UTC(2026, 9, 18, 21);
const EXPIRED = Date.UTC(2026, 9, 18, 20, 8, 15);
const RENEWED = Date.UTC(2026, 9, 19, 17);
const state = vi.hoisted(() => ({
  hosts: [{ host_id: "arca", name: "jackson's arca", status: "online" }],
}));

vi.mock("@/hooks/useHosts", () => ({ useHosts: () => ({ data: state.hosts }) }));
vi.mock("@/hooks/useNow", () => ({
  useNowSelector: (select: (now: Date) => boolean) => select(new Date()),
}));
vi.mock("@/lib/hostDbcert", async (importOriginal) => ({
  ...(await importOriginal<typeof HostDbcertModule>()),
  readHostDbcertExpiry: vi.fn(),
  startHostDbcertRefresh: vi.fn(),
}));

/** Let pending promise callbacks (fetch stubs, the refresh loop) run. */
async function settle(ms = 0) {
  await act(() => vi.advanceTimersByTimeAsync(ms));
}

function renderBanner() {
  return render(<DbcertBanner conversationId="conv" hostId="arca" />);
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  state.hosts = [{ host_id: "arca", name: "jackson's arca", status: "online" }];
  vi.mocked(readHostDbcertExpiry).mockReset().mockResolvedValue(EXPIRED);
  vi.mocked(startHostDbcertRefresh).mockReset().mockResolvedValue(true);
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
});

describe("DbcertBanner", () => {
  it("warns about an expired dbcert on the session's Arca host", async () => {
    renderBanner();
    await settle();
    expect(readHostDbcertExpiry).toHaveBeenCalledWith("conv");
    expect(screen.getByText(/dbcert expired on jackson's arca/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Refresh dbcert" })).toBeTruthy();
  });

  it("stays hidden while the cert is valid or the host has none", async () => {
    vi.mocked(readHostDbcertExpiry).mockResolvedValueOnce(RENEWED).mockResolvedValueOnce(null);
    renderBanner();
    await settle();
    expect(screen.queryByText(/dbcert expired/)).toBeNull();
    cleanup();
    renderBanner();
    await settle();
    expect(screen.queryByText(/dbcert expired/)).toBeNull();
  });

  it("never asks hosts that aren't an Arca box or Arclet", async () => {
    state.hosts = [{ host_id: "arca", name: "laptop", status: "online" }];
    renderBanner();
    await settle();
    expect(readHostDbcertExpiry).not.toHaveBeenCalled();
  });

  it("refreshes on the host and clears once the new cert arrives", async () => {
    renderBanner();
    await settle();
    vi.mocked(readHostDbcertExpiry).mockResolvedValueOnce(EXPIRED).mockResolvedValue(RENEWED);
    fireEvent.click(screen.getByRole("button", { name: "Refresh dbcert" }));
    await settle();
    expect(startHostDbcertRefresh).toHaveBeenCalledWith("conv");
    expect(screen.getByText(/Finish the sign-in/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Refreshing…" })).toHaveProperty("disabled", true);
    await settle(4_000);
    expect(screen.queryByText(/dbcert expired/)).toBeNull();
  });

  it("points at Arca Companion when the host can't refresh", async () => {
    vi.mocked(startHostDbcertRefresh).mockResolvedValue(false);
    renderBanner();
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Refresh dbcert" }));
    await settle();
    expect(screen.getByText(/Click dbcert in the Arca Companion menu/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
  });

  it("gives up after the host's sign-in window", async () => {
    renderBanner();
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Refresh dbcert" }));
    await settle(300_000);
    expect(screen.getByText(/Click dbcert in the Arca Companion menu/)).toBeTruthy();
  });

  it("refreshes the laptop instead in the desktop app", async () => {
    const refreshDbcert = vi.fn().mockResolvedValue({ ok: true });
    (window as unknown as { omnigentDesktop: unknown }).omnigentDesktop = {
      kind: "electron",
      refreshDbcert,
      getDesktopFeatures: vi.fn().mockResolvedValue({ canRefreshDbcert: true }),
    };
    renderBanner();
    await settle();
    vi.mocked(readHostDbcertExpiry).mockResolvedValue(RENEWED);
    fireEvent.click(screen.getByRole("button", { name: "Refresh dbcert" }));
    await settle();
    expect(refreshDbcert).toHaveBeenCalledOnce();
    expect(startHostDbcertRefresh).not.toHaveBeenCalled();
    expect(screen.queryByText(/dbcert expired/)).toBeNull();
  });

  it("stays dismissed for the cert it was dismissed for", async () => {
    renderBanner();
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText(/dbcert expired/)).toBeNull();
    await settle(60_000);
    expect(screen.queryByText(/dbcert expired/)).toBeNull();
  });
});
