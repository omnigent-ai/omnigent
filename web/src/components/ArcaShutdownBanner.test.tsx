import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { ArcaShutdownBanner } from "./ArcaShutdownBanner";
import { useArcaShutdownBanner } from "@/hooks/useArcaShutdownBanner";
import { isWarnedToday, resetArcaWarningStorageForTests } from "@/lib/arcaShutdownWarning";
import { copyText } from "@/lib/clipboard";

const MON_5PM = new Date(2026, 9, 5, 17, 0);
const WED_5PM = new Date(2026, 9, 7, 17, 0);
const THU_5PM = new Date(2026, 9, 8, 17, 0);
const state = vi.hoisted(() => ({
  enabled: true,
  now: new Date(2026, 9, 5, 17, 0),
  hosts: [{ host_id: "arca", name: "jackson's arca", status: "online" }],
}));

vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({ data: state.hosts }),
}));
vi.mock("@/hooks/useNow", () => ({ useNow: () => state.now }));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => ({}) }));
vi.mock("@/lib/capabilities", () => ({ isFeatureEnabled: () => state.enabled }));
vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn() }));
vi.mock("sonner", () => ({ toast: { dismiss: vi.fn() } }));

function Banner({ hostId = "arca" }: { hostId?: string }) {
  const warning = useArcaShutdownBanner();
  return warning.showForHost(hostId) ? <ArcaShutdownBanner warning={warning} /> : null;
}

beforeEach(() => {
  localStorage.clear();
  resetArcaWarningStorageForTests();
  state.enabled = true;
  state.now = MON_5PM;
  state.hosts = [{ host_id: "arca", name: "jackson's arca", status: "online" }];
  vi.mocked(toast.dismiss).mockClear();
  vi.mocked(copyText).mockReset().mockResolvedValue(undefined);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
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
    const view = render(<Banner />);
    expect(screen.getByText("arca extend workweek")).toBeTruthy();
    state.now = THU_5PM;
    view.rerender(<Banner />);
    expect(screen.queryByText("arca extend workweek")).toBeNull();
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
});
