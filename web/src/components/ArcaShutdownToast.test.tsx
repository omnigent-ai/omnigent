import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast, Toaster } from "sonner";
import type * as SonnerModule from "sonner";
import { ArcaShutdownToast } from "./ArcaShutdownToast";
import { ArcaShutdownBanner } from "./ArcaShutdownBanner";
import { useArcaShutdownBanner } from "@/hooks/useArcaShutdownBanner";
import {
  isDismissedToday,
  isWarnedToday,
  resetArcaWarningStorageForTests,
} from "@/lib/arcaShutdownWarning";
import { copyText } from "@/lib/clipboard";

const MON_459PM = new Date(2026, 9, 5, 16, 59);
const MON_5PM = new Date(2026, 9, 5, 17, 0);
const TUE_5PM = new Date(2026, 9, 6, 17, 0);
const TUE_MIDNIGHT = new Date(2026, 9, 6, 0, 0);
const SAT_5PM = new Date(2026, 9, 10, 17, 0);
const state = vi.hoisted(() => ({
  enabled: true,
  now: new Date(2026, 9, 5, 17, 0),
  hosts: [{ host_id: "arca", name: "jackson's arca", status: "online" }],
  toastCalls: 0,
}));

vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => {
    return { data: state.hosts };
  },
}));
vi.mock("@/hooks/useNow", () => ({
  useNow: () => state.now,
  useNowSelector: (select: (now: Date) => string, { enabled }: { enabled: boolean }) =>
    enabled ? select(state.now) : "",
}));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => ({}) }));
vi.mock("@/lib/capabilities", () => ({ isFeatureEnabled: () => state.enabled }));
vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn() }));
vi.mock("sonner", async (importOriginal) => {
  const actual = await importOriginal<typeof SonnerModule>();
  const wrapped = ((title: unknown, options?: { id?: string }) => {
    if (options?.id?.startsWith("arca-shutdown:")) state.toastCalls += 1;
    return (actual.toast as (title: unknown, options?: unknown) => unknown)(title, options);
  }) as typeof actual.toast;
  Object.assign(wrapped, actual.toast);
  return { ...actual, toast: wrapped };
});

function ToastPage({ showBanner = false }: { showBanner?: boolean }) {
  return (
    <>
      {showBanner && <BannerOnArcaHost />}
      <ArcaShutdownToast />
      <Toaster position="top-center" visibleToasts={100} />
    </>
  );
}

function BannerOnArcaHost() {
  const warning = useArcaShutdownBanner();
  return warning.showForHost("arca") ? <ArcaShutdownBanner warning={warning} /> : null;
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(MON_5PM);
  localStorage.clear();
  resetArcaWarningStorageForTests();
  state.enabled = true;
  state.now = MON_5PM;
  state.hosts = [{ host_id: "arca", name: "jackson's arca", status: "online" }];
  state.toastCalls = 0;
  vi.mocked(copyText).mockReset().mockResolvedValue(undefined);
  toast.dismiss();
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
});
afterEach(() => {
  toast.dismiss();
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
});

describe("ArcaShutdownToast", () => {
  it("dismisses the Home toast when the Arca chat banner mounts", async () => {
    const view = render(<ToastPage />);
    expect(await screen.findByText("Arca shuts down at about 6 PM")).toBeInTheDocument();
    view.rerender(<ToastPage showBanner />);
    expect(screen.getByText(/Your Arca will shut down/)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByText("Arca shuts down at about 6 PM")).not.toBeInTheDocument(),
    );
  });

  it("shows the toast in terminal view, where the transcript banner is unmounted", async () => {
    render(
      <>
        <ArcaShutdownToast />
        <div>Terminal view</div>
        <Toaster />
      </>,
    );
    expect(await screen.findByText("Arca shuts down at about 6 PM")).toBeInTheDocument();
    expect(isWarnedToday(state.now)).toBe(true);
  });

  it("offers desktop extend actions and hides copy until failure", async () => {
    const extendArca = vi.fn().mockResolvedValue({ ok: false, error: "Instance unavailable" });
    (window as unknown as { omnigentDesktop: unknown }).omnigentDesktop = {
      kind: "electron",
      extendArca,
      getDesktopFeatures: vi.fn().mockResolvedValue({ canExtendArca: true }),
    };
    render(<ToastPage />);
    expect(await screen.findByRole("button", { name: "Keep running tonight" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Keep running this week" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy command" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Keep running this week" }));
    expect(extendArca).toHaveBeenCalledWith("workweek");
    expect(await screen.findByRole("alert")).toHaveTextContent("Instance unavailable");
    expect(screen.getByRole("button", { name: "Copy arca extend workweek" })).toBeInTheDocument();
  });

  it("offers copy commands when the desktop gate is disabled", async () => {
    (window as unknown as { omnigentDesktop: unknown }).omnigentDesktop = {
      kind: "electron",
      extendArca: vi.fn(),
      getDesktopFeatures: vi.fn().mockResolvedValue({ canExtendArca: false }),
    };
    render(<ToastPage />);
    expect(await screen.findByRole("button", { name: "Copy command" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Keep running tonight" })).toBeNull();
    expect(screen.getByText(/on your laptop to keep it running/)).toBeInTheDocument();
  });

  it("dismisses a shown toast when the host goes offline", async () => {
    const view = render(<ToastPage />);
    expect(await screen.findByText("Arca shuts down at about 6 PM")).toBeInTheDocument();
    state.hosts = [{ ...state.hosts[0], status: "offline" }];
    view.rerender(<ToastPage />);
    await waitFor(() =>
      expect(screen.queryByText("Arca shuts down at about 6 PM")).not.toBeInTheDocument(),
    );
  });

  it("dismisses on cross-tab Not now", async () => {
    const view = render(<ToastPage />);
    expect(await screen.findByText("Arca shuts down at about 6 PM")).toBeInTheDocument();
    act(() => {
      localStorage.setItem("omnigent:arca-shutdown:dismissed", "2026-10-05");
      window.dispatchEvent(
        new StorageEvent("storage", { key: "omnigent:arca-shutdown:dismissed" }),
      );
    });
    await waitFor(() =>
      expect(screen.queryByText("Arca shuts down at about 6 PM")).not.toBeInTheDocument(),
    );
    view.rerender(<ToastPage showBanner />);
    expect(screen.queryByText(/Your Arca will shut down/)).not.toBeInTheDocument();
  });

  it("hides the banner for today when the toast's Not now is clicked", async () => {
    const view = render(<ToastPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Not now" }));
    expect(isDismissedToday(MON_5PM)).toBe(true);
    view.rerender(<ToastPage showBanner />);
    expect(screen.queryByText(/Your Arca will shut down/)).not.toBeInTheDocument();
  });

  it("keeps the toast when copying fails", async () => {
    vi.mocked(copyText).mockRejectedValue(new Error("clipboard unavailable"));
    render(<ToastPage />);
    expect(await screen.findByText("Arca shuts down at about 6 PM")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Copy command" }));
    expect(copyText).toHaveBeenCalledWith("arca extend overnight");
    expect(await screen.findByText("Could not copy command")).toBeInTheDocument();
    expect(screen.getByText("Arca shuts down at about 6 PM")).toBeInTheDocument();
  });

  it("copies the command, then dismisses and confirms success", async () => {
    render(<ToastPage />);
    expect(await screen.findByText("Arca shuts down at about 6 PM")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Copy command" }));
    expect(copyText).toHaveBeenCalledWith("arca extend overnight");
    expect(await screen.findByText("Copied")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByText("Arca shuts down at about 6 PM")).not.toBeInTheDocument(),
    );
  });

  it("dismisses the day of the click if Not now is pressed after midnight", async () => {
    render(<ToastPage />);
    const notNow = await screen.findByRole("button", { name: "Not now" });
    vi.setSystemTime(TUE_MIDNIGHT);
    fireEvent.click(notNow);
    expect(isDismissedToday(TUE_MIDNIGHT)).toBe(true);
  });

  it("deduplicates across two instances through the warned storage key", () => {
    const view = render(
      <>
        <ArcaShutdownToast />
        <ArcaShutdownToast />
        <Toaster />
      </>,
    );
    expect(state.toastCalls).toBe(1);
    expect(localStorage.getItem("omnigent:arca-shutdown:warned")).toBe("2026-10-05");
    state.now = TUE_5PM;
    view.rerender(
      <>
        <ArcaShutdownToast />
        <ArcaShutdownToast />
        <Toaster />
      </>,
    );
    expect(state.toastCalls).toBe(2);
  });

  it("waits for a hidden tab to become visible", async () => {
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    render(<ToastPage />);
    expect(state.toastCalls).toBe(0);
    expect(isWarnedToday(state.now)).toBe(false);
    visibility.mockReturnValue("visible");
    fireEvent(document, new Event("visibilitychange"));
    expect(await screen.findByText("Arca shuts down at about 6 PM")).toBeInTheDocument();
  });

  it("keeps the toast visible when StrictMode remounts with cached hosts", async () => {
    render(
      <StrictMode>
        <ToastPage />
      </StrictMode>,
    );
    expect(await screen.findByText("Arca shuts down at about 6 PM")).toBeInTheDocument();
    expect(isWarnedToday(MON_5PM)).toBe(true);
    expect(state.toastCalls).toBe(1);
  });

  it("dismisses yesterday's toast before showing today's after a sleep jump", async () => {
    const view = render(<ToastPage />);
    expect(await screen.findByText("Arca shuts down at about 6 PM")).toBeInTheDocument();
    state.now = TUE_5PM;
    vi.setSystemTime(TUE_5PM);
    view.rerender(<ToastPage />);
    await waitFor(() =>
      expect(screen.getAllByText("Arca shuts down at about 6 PM")).toHaveLength(1),
    );
    expect(state.toastCalls).toBe(2);
  });

  it("withdraws the toast at midnight", async () => {
    const view = render(<ToastPage />);
    expect(await screen.findByText("Arca shuts down at about 6 PM")).toBeInTheDocument();
    state.now = TUE_MIDNIGHT;
    view.rerender(<ToastPage />);
    await waitFor(() =>
      expect(screen.queryByText("Arca shuts down at about 6 PM")).not.toBeInTheDocument(),
    );
  });

  it.each([
    ["before 17:00", MON_459PM, "online"],
    ["on weekends", SAT_5PM, "online"],
    ["with no online Arca host", MON_5PM, "offline"],
  ] as const)("does not show %s", (_name, now, status) => {
    state.now = now;
    state.hosts = [{ ...state.hosts[0], status }];
    render(<ToastPage />);
    expect(state.toastCalls).toBe(0);
  });
});
