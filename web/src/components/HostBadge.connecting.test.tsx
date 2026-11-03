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
  onReconnect,
}: {
  sessionId: string;
  createdAtS: number;
  turnActive: boolean;
  onReconnect: () => void;
}) {
  const row: LivenessRow = {
    host_id: HOST_ID,
    permission_level: null,
    created_at: createdAtS,
    host_resumable: false,
  };
  const liveness = useSessionLiveness(sessionId, row, { turnActive });
  const sandboxStatus = useChatStore((s) => s.sandboxStatus);
  const connecting = useSessionHostConnecting(sessionId, liveness, sandboxStatus, [
    createdAtS * 1000,
  ]);
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
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
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
});
