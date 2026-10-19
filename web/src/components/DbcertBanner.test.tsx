import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DbcertBanner } from "./DbcertBanner";
import type * as HostModule from "@/lib/host";
import { isDatabricksWorkspace } from "@/lib/host";
import type * as HostDbcertModule from "@/lib/hostDbcert";
import {
  HOST_DBCERT_REFRESH_MS,
  readHostDbcertExpiry,
  startHostDbcertRefresh,
} from "@/lib/hostDbcert";

const NOW = Date.UTC(2026, 9, 18, 21);
const EXPIRED = Date.UTC(2026, 9, 18, 20, 8, 15);
const EXPIRED_LATER = Date.UTC(2026, 9, 18, 20, 30);
const RENEWED = Date.UTC(2026, 9, 19, 17);
interface TestHost {
  host_id: string;
  name: string;
  status: string;
  sandbox_provider?: string;
}
const state = vi.hoisted(() => ({
  hosts: [{ host_id: "arca", name: "jackson's arca", status: "online" }] as TestHost[],
}));

// Like the real hook, the host list hides sandbox hosts (Arclets) unless asked.
vi.mock("@/hooks/useHosts", () => ({
  useHosts: ({ includeSandbox = false }: { includeSandbox?: boolean } = {}) => ({
    data: state.hosts.filter((host) => includeSandbox || !host.sandbox_provider),
  }),
}));
vi.mock("@/hooks/useNow", () => ({
  useNowSelector: (select: (now: Date) => boolean) => select(new Date()),
}));
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof HostModule>()),
  isDatabricksWorkspace: vi.fn(),
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

// StrictMode, as main.tsx renders the app: its simulated remount must not drop reads.
function banner(hostId = "arca") {
  return (
    <StrictMode>
      <DbcertBanner conversationId="conv" hostId={hostId} />
    </StrictMode>
  );
}

function renderBanner() {
  return render(banner());
}

const ARCLET: TestHost = {
  host_id: "arclet",
  name: "omnigent-arclet-891349936151385",
  status: "online",
  sandbox_provider: "arclet",
};

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  state.hosts = [{ host_id: "arca", name: "jackson's arca", status: "online" }];
  vi.mocked(isDatabricksWorkspace).mockReset().mockReturnValue(true);
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
    expect(screen.getByText(/The dbcert on jackson's arca has expired/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Refresh dbcert" })).toBeTruthy();
  });

  it("stays hidden while the cert is valid or the host has none", async () => {
    vi.mocked(readHostDbcertExpiry).mockResolvedValue(RENEWED);
    renderBanner();
    await settle();
    expect(screen.queryByText(/has expired/)).toBeNull();
    cleanup();
    vi.mocked(readHostDbcertExpiry).mockResolvedValue(null);
    renderBanner();
    await settle();
    expect(screen.queryByText(/has expired/)).toBeNull();
  });

  it("warns on a managed Arclet, which is a sandbox host", async () => {
    state.hosts = [
      {
        host_id: "arca",
        name: "omnigent-arclet-891349936151385",
        status: "online",
        sandbox_provider: "arclet",
      },
    ];
    renderBanner();
    await settle();
    expect(
      screen.getByText(/The dbcert on omnigent-arclet-891349936151385 has expired/),
    ).toBeTruthy();
  });

  it("warns on any host with an expired dbcert, whatever its name", async () => {
    state.hosts = [{ host_id: "arca", name: "ip-10-90-21-195", status: "online" }];
    renderBanner();
    await settle();
    expect(screen.getByText(/The dbcert on ip-10-90-21-195 has expired/)).toBeTruthy();
  });

  it("never asks hosts outside a Databricks deployment", async () => {
    vi.mocked(isDatabricksWorkspace).mockReturnValue(false);
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
    expect(screen.queryByText(/has expired/)).toBeNull();
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
    await settle(HOST_DBCERT_REFRESH_MS);
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
    expect(screen.queryByText(/has expired/)).toBeNull();
  });

  it("stays dismissed for the cert it was dismissed for", async () => {
    renderBanner();
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText(/has expired/)).toBeNull();
    await settle(60_000);
    expect(screen.queryByText(/has expired/)).toBeNull();
  });

  it("starts fresh for a later expired cert after a failed refresh and a dismissal", async () => {
    vi.mocked(startHostDbcertRefresh).mockResolvedValue(false);
    renderBanner();
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Refresh dbcert" }));
    await settle();
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    // Renewed elsewhere, then expired again.
    vi.mocked(readHostDbcertExpiry).mockResolvedValue(RENEWED);
    await settle(60_000);
    vi.mocked(readHostDbcertExpiry).mockResolvedValue(EXPIRED_LATER);
    await settle(5 * 60_000);
    expect(screen.getByText(/The dbcert on jackson's arca has expired/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Refresh dbcert" })).toBeTruthy();
    expect(screen.queryByText(/Couldn't refresh it from here/)).toBeNull();
  });

  it("follows a host switch within the same session right away", async () => {
    state.hosts.push(ARCLET);
    const view = renderBanner();
    await settle();
    expect(screen.getByText(/The dbcert on jackson's arca has expired/)).toBeTruthy();
    // Expired to valid: no stale warning from the previous host.
    vi.mocked(readHostDbcertExpiry).mockResolvedValue(RENEWED);
    view.rerender(banner("arclet"));
    expect(screen.queryByText(/has expired/)).toBeNull();
    await settle();
    expect(screen.queryByText(/has expired/)).toBeNull();
    // Valid to expired: reported without waiting for the next poll.
    vi.mocked(readHostDbcertExpiry).mockResolvedValue(EXPIRED);
    view.rerender(banner("arca"));
    await settle();
    expect(screen.getByText(/The dbcert on jackson's arca has expired/)).toBeTruthy();
  });

  it("drops a refresh still running for the previous host", async () => {
    state.hosts.push(ARCLET);
    const view = renderBanner();
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Refresh dbcert" }));
    await settle();
    expect(screen.getByRole("button", { name: "Refreshing…" })).toBeTruthy();
    vi.mocked(readHostDbcertExpiry).mockResolvedValue(EXPIRED_LATER);
    view.rerender(banner("arclet"));
    await settle();
    expect(
      screen.getByText(/The dbcert on omnigent-arclet-891349936151385 has expired/),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Refresh dbcert" })).toBeTruthy();
    await settle(HOST_DBCERT_REFRESH_MS);
    expect(screen.getByRole("button", { name: "Refresh dbcert" })).toBeTruthy();
    expect(screen.queryByText(/Couldn't refresh it from here/)).toBeNull();
  });
});
