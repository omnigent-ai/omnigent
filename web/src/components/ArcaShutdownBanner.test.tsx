import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ArcaShutdownBanner } from "./ArcaShutdownBanner";
import { useArcaShutdownWarning } from "@/hooks/useArcaShutdownWarning";
import { isToastedToday } from "@/lib/arcaShutdownWarning";

const state = vi.hoisted(() => ({
  enabled: true,
  now: new Date(2026, 9, 5, 17, 0),
  hosts: [{ host_id: "arca", name: "jackson's arca", status: "online" }],
  toast: Object.assign(vi.fn(), { success: vi.fn() }),
}));

vi.mock("@/hooks/useHosts", () => ({ useHosts: () => ({ data: state.hosts }) }));
vi.mock("@/hooks/useNow", () => ({ useNow: () => state.now }));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => ({}) }));
vi.mock("@/lib/capabilities", () => ({ isFeatureEnabled: () => state.enabled }));
vi.mock("@/lib/arcaHost", () => ({ readArcaHostId: () => null }));
vi.mock("sonner", () => ({ toast: state.toast }));

beforeEach(() => {
  localStorage.clear();
  state.enabled = true;
  state.now = new Date(2026, 9, 5, 17, 0);
  state.hosts = [{ host_id: "arca", name: "jackson's arca", status: "online" }];
  state.toast.mockClear();
  state.toast.success.mockClear();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function mountToastDescription(index = state.toast.mock.calls.length - 1) {
  return render(state.toast.mock.calls[index][1].description);
}

describe("ArcaShutdownBanner", () => {
  it("renders nothing when the flag is off or host is offline", () => {
    state.enabled = false;
    const view = render(<ArcaShutdownBanner hostId="arca" />);
    expect(screen.queryByText(/Your Arca will shut down/)).toBeNull();
    state.enabled = true;
    state.hosts = [{ ...state.hosts[0], status: "offline" }];
    view.rerender(<ArcaShutdownBanner hostId="arca" />);
    expect(screen.queryByText(/Your Arca will shut down/)).toBeNull();
  });

  it("hides until tomorrow after Not now", () => {
    const view = render(<ArcaShutdownBanner hostId="arca" />);
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    expect(screen.queryByText(/Your Arca will shut down/)).toBeNull();
    state.now = new Date(2026, 9, 6, 17, 0);
    view.rerender(<ArcaShutdownBanner hostId="arca" />);
    expect(screen.getByText(/Your Arca will shut down/)).toBeTruthy();
  });

  it("permanently hides after Don't remind me", () => {
    const view = render(<ArcaShutdownBanner hostId="arca" />);
    fireEvent.click(screen.getByRole("button", { name: "Don't remind me" }));
    state.now = new Date(2026, 9, 6, 17, 0);
    view.rerender(<ArcaShutdownBanner hostId="arca" />);
    expect(screen.queryByText(/Your Arca will shut down/)).toBeNull();
  });

  it("copies the selected command", () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<ArcaShutdownBanner hostId="arca" />);
    fireEvent.click(screen.getByRole("button", { name: "Copy arca extend overnight" }));
    expect(writeText).toHaveBeenCalledWith("arca extend overnight");
  });

  it("shows workweek on Wednesday and hides it Thursday", () => {
    state.now = new Date(2026, 9, 7, 17, 0);
    const view = render(<ArcaShutdownBanner hostId="arca" />);
    expect(screen.getByText("arca extend workweek")).toBeTruthy();
    state.now = new Date(2026, 9, 8, 17, 0);
    view.rerender(<ArcaShutdownBanner hostId="arca" />);
    expect(screen.queryByText("arca extend workweek")).toBeNull();
  });

  it("marks a background banner only after its tab becomes visible", () => {
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    render(<ArcaShutdownBanner hostId="arca" />);
    expect(screen.getByText(/Your Arca will shut down/)).toBeTruthy();
    expect(isToastedToday(state.now)).toBe(false);
    visibility.mockReturnValue("visible");
    fireEvent(document, new Event("visibilitychange"));
    expect(isToastedToday(state.now)).toBe(true);
  });
});

describe("Arca toast", () => {
  it("fires once per date across two hook instances sharing storage", () => {
    const first = renderHook(() => useArcaShutdownWarning(true));
    const second = renderHook(() => useArcaShutdownWarning(true));
    expect(state.toast).toHaveBeenCalledTimes(1);
    expect(state.toast).toHaveBeenCalledWith(
      "Arca shuts down at about 6 PM",
      expect.objectContaining({
        id: "arca-shutdown:2026-10-05",
        duration: Infinity,
        closeButton: true,
        classNames: expect.objectContaining({
          toast: expect.stringContaining("!grid"),
          content: expect.stringContaining("!col-span-2"),
        }),
      }),
    );
    expect(isToastedToday(state.now)).toBe(false);
    const descriptionView = mountToastDescription();
    expect(descriptionView.container.querySelector("code")?.textContent).toBe(
      "arca extend overnight",
    );
    expect(isToastedToday(state.now)).toBe(true);
    state.now = new Date(2026, 9, 6, 17, 0);
    first.rerender();
    second.rerender();
    expect(state.toast).toHaveBeenCalledTimes(2);
  });

  it("records today only after the active Arca banner mounts", () => {
    renderHook(() => useArcaShutdownWarning(true, "arca"));
    expect(state.toast).not.toHaveBeenCalled();
    expect(isToastedToday(state.now)).toBe(false);
    render(<ArcaShutdownBanner hostId="arca" />);
    expect(screen.getByText(/Your Arca will shut down/)).toBeTruthy();
    expect(isToastedToday(state.now)).toBe(true);
  });

  it("still toasts on a non-Arca page", () => {
    renderHook(() => useArcaShutdownWarning(true, "other-host"));
    expect(state.toast).toHaveBeenCalledTimes(1);
    expect(isToastedToday(state.now)).toBe(false);
    mountToastDescription();
    expect(isToastedToday(state.now)).toBe(true);
  });

  it("waits for the active session's host before deciding which warning to show", () => {
    const view = renderHook(
      ({ loading, hostId }) => useArcaShutdownWarning(true, hostId, loading),
      { initialProps: { loading: true, hostId: null as string | null } },
    );
    expect(state.toast).not.toHaveBeenCalled();
    expect(isToastedToday(state.now)).toBe(false);
    view.rerender({ loading: false, hostId: "arca" });
    expect(state.toast).not.toHaveBeenCalled();
    expect(isToastedToday(state.now)).toBe(false);
    render(<ArcaShutdownBanner hostId="arca" />);
    expect(isToastedToday(state.now)).toBe(true);
  });

  it("toasts when a transient Arca host resolves to no host on a non-Arca page", () => {
    const view = renderHook(({ hostId }) => useArcaShutdownWarning(true, hostId), {
      initialProps: { hostId: "arca" as string | null },
    });
    expect(state.toast).not.toHaveBeenCalled();
    expect(isToastedToday(state.now)).toBe(false);
    view.rerender({ hostId: null });
    expect(state.toast).toHaveBeenCalledTimes(1);
    mountToastDescription();
    expect(isToastedToday(state.now)).toBe(true);
  });

  it("does not toast after Not now in the active Arca session", () => {
    localStorage.setItem("omnigent:arca-shutdown:dismissed", "2026-10-05");
    renderHook(() => useArcaShutdownWarning(true, "arca"));
    render(<ArcaShutdownBanner hostId="arca" />);
    expect(state.toast).not.toHaveBeenCalled();
    expect(screen.queryByText(/Your Arca will shut down/)).toBeNull();
  });

  it("waits for a hidden tab to become visible before calling toast", () => {
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    renderHook(() => useArcaShutdownWarning(true));
    expect(state.toast).not.toHaveBeenCalled();
    expect(isToastedToday(state.now)).toBe(false);
    visibility.mockReturnValue("visible");
    fireEvent(document, new Event("visibilitychange"));
    expect(state.toast).toHaveBeenCalledTimes(1);
    mountToastDescription();
    expect(isToastedToday(state.now)).toBe(true);
  });

  it("does not mark toast content hidden before it mounted", () => {
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    renderHook(() => useArcaShutdownWarning(true));
    expect(state.toast).toHaveBeenCalledTimes(1);
    visibility.mockReturnValue("hidden");
    mountToastDescription();
    expect(isToastedToday(state.now)).toBe(false);
    visibility.mockReturnValue("visible");
    fireEvent(document, new Event("visibilitychange"));
    expect(isToastedToday(state.now)).toBe(true);
  });

  it.each([
    ["before 17:00", new Date(2026, 9, 5, 16, 59), true, "online", false],
    ["on weekends", new Date(2026, 9, 10, 17, 0), true, "online", false],
    ["with flag off", new Date(2026, 9, 5, 17, 0), false, "online", false],
    ["without online Arca", new Date(2026, 9, 5, 17, 0), true, "offline", false],
    ["after opt out", new Date(2026, 9, 5, 17, 0), true, "online", true],
  ] as const)("does not fire %s", (_name, now, enabled, status, optedOut) => {
    state.now = now;
    state.enabled = enabled;
    state.hosts = [{ ...state.hosts[0], status }];
    if (optedOut) localStorage.setItem("omnigent:arca-shutdown:opted-out", "true");
    renderHook(() => useArcaShutdownWarning(true));
    expect(state.toast).not.toHaveBeenCalled();
  });

  it("copies the overnight command from the toast action", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    renderHook(() => useArcaShutdownWarning(true));
    const options = state.toast.mock.calls[0][1];
    options.action.onClick();
    expect(writeText).toHaveBeenCalledWith("arca extend overnight");
    await waitFor(() => expect(state.toast.success).toHaveBeenCalledWith("Copied"));
  });

  it("hides the chat banner for today when Not now is clicked in the toast", () => {
    renderHook(() => useArcaShutdownWarning(true));
    render(<ArcaShutdownBanner hostId="arca" />);
    expect(screen.getByText(/Your Arca will shut down/)).toBeTruthy();
    act(() => state.toast.mock.calls[0][1].cancel.onClick());
    expect(screen.queryByText(/Your Arca will shut down/)).toBeNull();
  });

  it("retries next tick if a toast was never mounted by Toaster", () => {
    const view = renderHook(() => useArcaShutdownWarning(true));
    expect(state.toast).toHaveBeenCalledTimes(1);
    expect(isToastedToday(state.now)).toBe(false);
    state.now = new Date(2026, 9, 5, 17, 30);
    view.rerender();
    expect(state.toast).toHaveBeenCalledTimes(2);
    mountToastDescription();
    state.now = new Date(2026, 9, 5, 18, 0);
    view.rerender();
    expect(state.toast).toHaveBeenCalledTimes(2);
  });

  it("does not repeat on the next clock tick when storage rejects writes", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("storage unavailable");
    });
    const view = renderHook(() => useArcaShutdownWarning(true));
    expect(state.toast).toHaveBeenCalledTimes(1);
    mountToastDescription();
    state.now = new Date(2026, 9, 5, 17, 30);
    view.rerender();
    expect(state.toast).toHaveBeenCalledTimes(1);
  });
});
