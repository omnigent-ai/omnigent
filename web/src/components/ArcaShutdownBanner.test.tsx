import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { ArcaShutdownBanner } from "./ArcaShutdownBanner";
import { useArcaShutdownBanner } from "@/hooks/useArcaShutdownBanner";
import { isWarnedToday, resetArcaWarningStorageForTests } from "@/lib/arcaShutdownWarning";
import { copyText } from "@/lib/clipboard";

const MON_5PM = new Date(2026, 9, 5, 17, 0);
const WED_5PM = new Date(2026, 9, 7, 17, 0);
const THU_5PM = new Date(2026, 9, 8, 17, 0);
const TUE_9AM = new Date(2026, 9, 6, 9, 0);
const state = vi.hoisted(() => ({
  enabled: true,
  now: new Date(2026, 9, 5, 17, 0),
  hosts: [{ host_id: "arca", name: "jackson's arca", status: "online" }],
}));

vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({ data: state.hosts }),
}));
vi.mock("@/hooks/useNow", () => ({
  useNowSelector: (select: (now: Date) => string, { enabled }: { enabled: boolean }) =>
    enabled ? select(state.now) : "",
}));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => ({}) }));
vi.mock("@/lib/capabilities", () => ({ isFeatureEnabled: () => state.enabled }));
vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn() }));
vi.mock("sonner", () => ({ toast: { dismiss: vi.fn() } }));

function Banner({ hostId = "arca" }: { hostId?: string }) {
  const warning = useArcaShutdownBanner();
  return warning.showForHost(hostId) ? <ArcaShutdownBanner warning={warning} /> : null;
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(MON_5PM);
  localStorage.clear();
  resetArcaWarningStorageForTests();
  state.enabled = true;
  state.now = MON_5PM;
  state.hosts = [{ host_id: "arca", name: "jackson's arca", status: "online" }];
  vi.mocked(toast.dismiss).mockClear();
  vi.mocked(copyText).mockReset().mockResolvedValue(undefined);
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
});

describe("ArcaShutdownBanner", () => {
  it("copies the selected command and briefly confirms it", async () => {
    render(<Banner />);
    fireEvent.click(screen.getByRole("button", { name: "Copy arca extend overnight" }));
    expect(copyText).toHaveBeenCalledWith("arca extend overnight");
    expect(
      await screen.findByRole("button", { name: "Copied arca extend overnight" }),
    ).toBeTruthy();
  });

  it("shows workweek on Wednesday and hides it Thursday", () => {
    state.now = WED_5PM;
    vi.setSystemTime(WED_5PM);
    const view = render(<Banner />);
    expect(screen.getByText("arca extend workweek")).toBeTruthy();
    state.now = THU_5PM;
    vi.setSystemTime(THU_5PM);
    view.rerender(<Banner />);
    expect(screen.queryByText("arca extend workweek")).toBeNull();
  });

  it("shows only Not now beside desktop extend actions", () => {
    (window as unknown as { omnigentDesktop: unknown }).omnigentDesktop = {
      kind: "electron",
      extendArca: vi.fn(),
    };
    render(<Banner />);
    expect(screen.getByRole("button", { name: "Keep running tonight" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Keep running this week" })).toBeTruthy();
    expect(screen.queryByText("Run this on your laptop.")).toBeNull();
    expect(screen.queryByRole("button", { name: /Copy arca/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Not now" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Don't remind me" })).toBeNull();
  });

  it("disables both desktop actions while extending and hides workweek on Thursday", async () => {
    let finish!: (result: { ok: boolean; shutdownAt: null }) => void;
    const extendArca = vi.fn().mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    (window as unknown as { omnigentDesktop: unknown }).omnigentDesktop = {
      kind: "electron",
      extendArca,
    };
    render(<Banner />);
    fireEvent.click(screen.getByRole("button", { name: "Keep running tonight" }));
    expect(screen.getByRole("button", { name: "Extending Arca…" })).toHaveProperty(
      "disabled",
      true,
    );
    expect(screen.getByRole("button", { name: "Keep running this week" })).toHaveProperty(
      "disabled",
      true,
    );
    await act(async () => finish({ ok: true, shutdownAt: null }));
    cleanup();
    state.now = THU_5PM;
    vi.setSystemTime(THU_5PM);
    render(<Banner />);
    expect(screen.queryByRole("button", { name: "Keep running this week" })).toBeNull();
  });

  it("extends, confirms the new shutdown time, then dismisses today", async () => {
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
    const extendArca = vi.fn().mockResolvedValue({ ok: true, shutdownAt: Date.UTC(2026, 9, 6, 1) });
    (window as unknown as { omnigentDesktop: unknown }).omnigentDesktop = {
      kind: "electron",
      extendArca,
    };
    render(<Banner />);
    fireEvent.click(screen.getByRole("button", { name: "Keep running tonight" }));
    expect(extendArca).toHaveBeenCalledWith("overnight");
    await act(async () => {});
    expect(screen.getByText(/Arca will keep running until/)).toBeTruthy();
    act(() => vi.advanceTimersByTime(2000));
    expect(screen.queryByText(/Your Arca will shut down/)).toBeNull();
  });

  it("shows the command after desktop extend fails", async () => {
    const extendArca = vi.fn().mockResolvedValue({ ok: false, error: "Arca is offline" });
    (window as unknown as { omnigentDesktop: unknown }).omnigentDesktop = {
      kind: "electron",
      extendArca,
    };
    render(<Banner />);
    fireEvent.click(screen.getByRole("button", { name: "Keep running tonight" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Arca is offline");
    expect(screen.getByRole("button", { name: "Copy arca extend overnight" })).toBeTruthy();
  });

  it("marks and dismisses the toast only when the banner becomes visible", async () => {
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    render(<Banner />);
    expect(isWarnedToday(state.now)).toBe(false);
    expect(toast.dismiss).not.toHaveBeenCalled();
    visibility.mockReturnValue("visible");
    fireEvent(document, new Event("visibilitychange"));
    await waitFor(() => expect(isWarnedToday(state.now)).toBe(true));
    expect(toast.dismiss).toHaveBeenCalledWith("arca-shutdown:2026-10-05");
  });

  it("does not mark a new day when a sleeping tab wakes before its banner updates", () => {
    render(<Banner />);
    expect(isWarnedToday(MON_5PM)).toBe(true);
    vi.mocked(toast.dismiss).mockClear();

    vi.setSystemTime(TUE_9AM);
    fireEvent(document, new Event("visibilitychange"));
    expect(isWarnedToday(TUE_9AM)).toBe(false);
    expect(toast.dismiss).not.toHaveBeenCalled();
  });
});
