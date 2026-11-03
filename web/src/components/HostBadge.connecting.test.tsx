import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HostBadge } from "./HostBadge";
import {
  STARTING_GRACE_S,
  type LivenessRow,
  useSessionHostConnecting,
  useSessionLiveness,
} from "@/hooks/useSessionLiveness";
import type { Host } from "@/hooks/useHosts";
import { useChatStore } from "@/store/chatStore";

// Only the data sources are stubbed: runner/host liveness (the health poll),
// the session snapshot, and the host list. The liveness derivation, the
// bounded connecting window, and the badge are the real code ChatPage runs.
const { runnerOnlineMock, hostOnlineMock, useSessionMock, useHostsMock } = vi.hoisted(() => ({
  runnerOnlineMock: vi.fn<(id: string | undefined) => boolean | undefined>(),
  hostOnlineMock: vi.fn<(id: string | undefined) => boolean | null | undefined>(),
  useSessionMock: vi.fn(),
  useHostsMock: vi.fn(),
}));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: (id: string | undefined) => runnerOnlineMock(id),
  useSessionHostOnline: (id: string | undefined) => hostOnlineMock(id),
}));
vi.mock("@/hooks/useSession", () => ({
  useSession: (id: string | null | undefined) => useSessionMock(id),
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: (opts: unknown) => useHostsMock(opts),
}));
vi.mock("@/shell/SwitchHostDialog", () => ({ SwitchHostDialog: () => null }));

const HOST_ID = "host_a1b2";

function host(overrides: Partial<Host> = {}): Host {
  return {
    host_id: HOST_ID,
    name: "mac-laptop",
    owner: "alice",
    status: "online",
    sandbox_provider: null,
    ...overrides,
  };
}

/** ChatPage's wiring: liveness, then the bounded connecting signal, then the composer badge. */
function OpenSession({
  sessionId,
  createdAtS,
  turnActive,
  launchedAt = null,
  onReconnect,
}: {
  sessionId: string;
  createdAtS: number;
  turnActive: boolean;
  launchedAt?: number | null;
  onReconnect: () => void;
}) {
  const row: LivenessRow = {
    host_id: HOST_ID,
    permission_level: null,
    created_at: createdAtS,
    host_resumable: false,
  };
  const markers = { turnActive, launchedAt };
  const liveness = useSessionLiveness(sessionId, row, markers);
  const sandboxStatus = useChatStore((s) => s.sandboxStatus);
  const connecting = useSessionHostConnecting(sessionId, liveness, sandboxStatus, markers);
  return (
    <HostBadge
      sessionId={sessionId}
      appearance="composer"
      onReconnect={onReconnect}
      connecting={connecting}
    />
  );
}

const nowS = () => Math.floor(Date.now() / 1000);
const trigger = () => screen.getByTestId("composer-host-select");
const openHostMenu = () => {
  fireEvent.keyDown(trigger(), { key: "ArrowDown" });
  return screen.getByTestId("composer-host-menu");
};
const closeHostMenu = () =>
  fireEvent.keyDown(screen.getByTestId("composer-host-menu"), { key: "Escape" });

beforeEach(() => {
  // `performance` is the monotonic clock; `setSystemTime` moves only the wall clock.
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date", "performance"] });
  vi.setSystemTime(new Date("2026-11-03T12:00:00Z"));
  runnerOnlineMock.mockReset().mockReturnValue(false);
  hostOnlineMock.mockReset().mockReturnValue(true);
  useSessionMock.mockReset().mockReturnValue({ session: { hostId: HOST_ID } });
  useHostsMock.mockReset().mockReturnValue({ data: [host()] });
  useChatStore.setState({ conversationId: "conv_1", sandboxStatus: null });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  useChatStore.setState({ conversationId: null, sandboxStatus: null });
});

describe("composer host chip while a session connects", () => {
  it("says connecting after the first send until the runner is online", () => {
    const createdAtS = nowS();
    const props = { sessionId: "conv_1", createdAtS, turnActive: true, onReconnect: vi.fn() };
    const { rerender } = render(<OpenSession {...props} />);

    expect(trigger()).toHaveAccessibleName("Host mac-laptop, connecting");
    expect(within(trigger()).getByText("Connecting…")).toBeInTheDocument();
    const menu = openHostMenu();
    expect(within(menu).getByText("Connecting to this host…")).toBeInTheDocument();
    expect(screen.getByTestId("composer-host-menu-status")).toHaveAttribute(
      "data-status",
      "starting",
    );
    closeHostMenu();

    act(() => vi.advanceTimersByTime(8_000));
    runnerOnlineMock.mockReturnValue(true);
    rerender(<OpenSession {...props} />);
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, online");
    expect(within(trigger()).queryByText("Connecting…")).toBeNull();
    expect(within(openHostMenu()).queryByText("Connecting to this host…")).toBeNull();
  });

  it("falls back to offline with Reconnect on its own once the grace lapses", () => {
    hostOnlineMock.mockReturnValue(false);
    const onReconnect = vi.fn();
    render(
      <OpenSession sessionId="conv_1" createdAtS={nowS()} turnActive onReconnect={onReconnect} />,
    );
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, connecting");
    openHostMenu();
    expect(screen.queryByRole("menuitem", { name: "Reconnect host" })).toBeNull();
    closeHostMenu();

    // No rerender, poll, or prop change: only the clock moves.
    act(() => vi.advanceTimersByTime(STARTING_GRACE_S * 1000));
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, offline");
    expect(within(trigger()).queryByText("Connecting…")).toBeNull();
    openHostMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Reconnect host" }));
    expect(onReconnect).toHaveBeenCalledTimes(1);
  });

  it("keeps a bound sandbox waiting on the user out of connecting", () => {
    useHostsMock.mockReturnValue({
      data: [host({ name: "managed-sb", sandbox_provider: "lakebox" })],
    });
    hostOnlineMock.mockReturnValue(false);
    useChatStore.setState({
      sandboxStatus: { stage: "action_required", details: "Approve the sandbox" },
    });
    render(<OpenSession sessionId="conv_1" createdAtS={nowS()} turnActive onReconnect={vi.fn()} />);

    expect(trigger()).toHaveAccessibleName("Host Databricks Sandbox, offline");
    expect(within(trigger()).queryByText("Connecting…")).toBeNull();
    const menu = openHostMenu();
    expect(within(menu).queryByText("Connecting to this host…")).toBeNull();
    expect(screen.getByRole("menuitem", { name: "Reconnect host" })).toBeInTheDocument();
  });

  it("starts a fresh window for the session switched to", () => {
    hostOnlineMock.mockReturnValue(false);
    const first = { createdAtS: nowS(), turnActive: true, onReconnect: vi.fn() };
    const { rerender } = render(<OpenSession sessionId="conv_1" {...first} />);
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, connecting");

    act(() => vi.advanceTimersByTime(40_000));
    useChatStore.setState({ conversationId: "conv_2" });
    const second = { createdAtS: nowS(), turnActive: true, onReconnect: vi.fn() };
    rerender(<OpenSession sessionId="conv_2" {...second} />);
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, connecting");

    // Past the first session's deadline, inside the second's.
    act(() => vi.advanceTimersByTime(10_000));
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, connecting");

    act(() => vi.advanceTimersByTime((STARTING_GRACE_S - 10) * 1000));
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, offline");
  });
  it.each([
    ["44s behind", -44],
    ["10 minutes ahead of", 600],
  ])(
    "bounds connecting to the grace from first sight when server time is %s the client",
    (_skew, skewS) => {
      hostOnlineMock.mockReturnValue(false);
      const props = {
        sessionId: "conv_1",
        createdAtS: nowS() + skewS,
        turnActive: true,
        onReconnect: vi.fn(),
      };
      const { rerender } = render(<OpenSession {...props} />);
      expect(trigger()).toHaveAccessibleName("Host mac-laptop, connecting");

      // A health poll re-renders mid-boot, after a skewed server grace would have lapsed.
      act(() => vi.advanceTimersByTime(2_000));
      rerender(<OpenSession {...props} />);
      expect(trigger()).toHaveAccessibleName("Host mac-laptop, connecting");

      act(() => vi.advanceTimersByTime((STARTING_GRACE_S - 2) * 1000 - 1));
      expect(trigger()).toHaveAccessibleName("Host mac-laptop, connecting");
      act(() => vi.advanceTimersByTime(1));
      expect(trigger()).toHaveAccessibleName("Host mac-laptop, offline");
      openHostMenu();
      expect(screen.getByRole("menuitem", { name: "Reconnect host" })).toBeInTheDocument();
    },
  );

  it("ends connecting after the grace of elapsed time when the wall clock rolls back", () => {
    hostOnlineMock.mockReturnValue(false);
    const props = {
      sessionId: "conv_1",
      createdAtS: nowS(),
      turnActive: true,
      onReconnect: vi.fn(),
    };
    const { rerender } = render(<OpenSession {...props} />);

    act(() => vi.advanceTimersByTime(10_000));
    vi.setSystemTime(Date.now() - 10 * 60_000);
    rerender(<OpenSession {...props} />);
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, connecting");

    act(() => vi.advanceTimersByTime((STARTING_GRACE_S - 10) * 1000));
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, offline");
    act(() => vi.advanceTimersByTime(60_000));
    rerender(<OpenSession {...props} />);
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, offline");
    openHostMenu();
    expect(screen.getByRole("menuitem", { name: "Reconnect host" })).toBeInTheDocument();
  });

  it("gives a second host-switch launch its own full window", () => {
    hostOnlineMock.mockReturnValue(false);
    const base = { sessionId: "conv_1", createdAtS: nowS() - 3_600, turnActive: false };
    const firstLaunch = { ...base, launchedAt: Date.now(), onReconnect: vi.fn() };
    const { rerender } = render(<OpenSession {...firstLaunch} />);
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, connecting");

    act(() => vi.advanceTimersByTime(30_000));
    const secondLaunch = { ...firstLaunch, launchedAt: Date.now() };
    rerender(<OpenSession {...secondLaunch} />);

    // Past the first launch's deadline, inside the second's.
    act(() => vi.advanceTimersByTime(STARTING_GRACE_S * 1000 - 1));
    rerender(<OpenSession {...secondLaunch} />);
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, connecting");
    act(() => vi.advanceTimersByTime(1));
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, offline");
  });
  it("drops connecting as soon as a cancelled or failed send goes back to idle", () => {
    // An older session whose runner is asleep on a live host.
    const idle = { sessionId: "conv_1", createdAtS: nowS() - 3_600, onReconnect: vi.fn() };
    const { rerender } = render(<OpenSession {...idle} turnActive={false} />);
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, online");

    rerender(<OpenSession {...idle} turnActive />);
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, connecting");

    // The send is cancelled (or fails): the store returns to idle.
    act(() => vi.advanceTimersByTime(5_000));
    rerender(<OpenSession {...idle} turnActive={false} />);
    expect(within(trigger()).queryByText("Connecting…")).toBeNull();

    // A host-down poll lands well inside what would have been the window.
    act(() => vi.advanceTimersByTime(5_000));
    hostOnlineMock.mockReturnValue(false);
    rerender(<OpenSession {...idle} turnActive={false} />);
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, offline");
    openHostMenu();
    expect(screen.getByRole("menuitem", { name: "Reconnect host" })).toBeInTheDocument();
  });

  it("keeps a host-switch launch connecting when a send during it is cancelled", () => {
    const base = {
      sessionId: "conv_1",
      createdAtS: nowS() - 3_600,
      launchedAt: Date.now(),
      onReconnect: vi.fn(),
    };
    const { rerender } = render(<OpenSession {...base} turnActive={false} />);
    act(() => vi.advanceTimersByTime(2_000));
    rerender(<OpenSession {...base} turnActive />);
    act(() => vi.advanceTimersByTime(3_000));
    rerender(<OpenSession {...base} turnActive={false} />);
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, connecting");

    act(() => vi.advanceTimersByTime(5_000));
    hostOnlineMock.mockReturnValue(false);
    rerender(<OpenSession {...base} turnActive={false} />);
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, connecting");
    openHostMenu();
    expect(screen.queryByRole("menuitem", { name: "Reconnect host" })).toBeNull();
    closeHostMenu();

    // The send renewed the window at t=2, so it ends at t=2 + grace.
    act(() => vi.advanceTimersByTime((STARTING_GRACE_S - 8) * 1000));
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, offline");
    openHostMenu();
    expect(screen.getByRole("menuitem", { name: "Reconnect host" })).toBeInTheDocument();
  });
  it("gives a launch delivered with the turn's end its own full window", () => {
    // A send owns the attempt on an asleep session; a snapshot then lands the
    // turn's terminal status and a new launch marker in one update.
    const idle = { sessionId: "conv_1", createdAtS: nowS() - 3_600, onReconnect: vi.fn() };
    const { rerender } = render(<OpenSession {...idle} turnActive={false} />);
    rerender(<OpenSession {...idle} turnActive />);
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, connecting");

    act(() => vi.advanceTimersByTime(10_000));
    const relaunched = { ...idle, launchedAt: Date.now() };
    rerender(<OpenSession {...relaunched} turnActive={false} />);
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, connecting");

    act(() => vi.advanceTimersByTime(10_000));
    hostOnlineMock.mockReturnValue(false);
    rerender(<OpenSession {...relaunched} turnActive={false} />);
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, connecting");
    openHostMenu();
    expect(screen.queryByRole("menuitem", { name: "Reconnect host" })).toBeNull();
    closeHostMenu();

    act(() => vi.advanceTimersByTime((STARTING_GRACE_S - 10) * 1000 - 1));
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, connecting");
    act(() => vi.advanceTimersByTime(1));
    expect(trigger()).toHaveAccessibleName("Host mac-laptop, offline");
    openHostMenu();
    expect(screen.getByRole("menuitem", { name: "Reconnect host" })).toBeInTheDocument();
  });
});
