import { cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RunnerHealthInput, SessionLiveness } from "@/hooks/useRunnerHealth";

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
}));
vi.mock("@/hooks/useRunnerHealth", () => ({
  useRunnerHealth: vi.fn(),
}));
vi.mock("@/hooks/useSession", () => ({
  useSession: vi.fn(),
}));

import { useConversations } from "@/hooks/useConversations";
import { useRunnerHealth } from "@/hooks/useRunnerHealth";
import { useSession } from "@/hooks/useSession";
import {
  RunnerHealthProvider,
  useRunnerHealthRegistration,
  useSessionHostOnline,
  useSessionHostVersion,
  useSessionRunnerOnline,
} from "./RunnerHealthProvider";

const useConvMock = vi.mocked(useConversations);
const useRunnerHealthMock = vi.mocked(useRunnerHealth);
const useSessionMock = vi.mocked(useSession);

function liveness(
  runner_online: boolean,
  host_online: boolean | null = null,
  host_version: string | null = null,
): SessionLiveness {
  return { runner_online, host_online, host_version };
}

function OnlineProbe({ sessionId }: { sessionId: string | undefined }) {
  const online = useSessionRunnerOnline(sessionId);
  return <span data-testid="probe">{String(online)}</span>;
}

function HostProbe({ sessionId }: { sessionId: string | undefined }) {
  const host = useSessionHostOnline(sessionId);
  return <span data-testid="host-probe">{String(host)}</span>;
}

function HostVersionProbe({ sessionId }: { sessionId: string | undefined }) {
  const version = useSessionHostVersion(sessionId);
  return <span data-testid="host-version-probe">{String(version)}</span>;
}

// Reads the shared runner map back via a no-op registration (empty array
// registers nothing but returns the same merged map the registry serves).
const NO_SESSIONS: RunnerHealthInput[] = [];
function MapKeysProbe() {
  const map = useRunnerHealthRegistration(NO_SESSIONS);
  const keys = [...map.keys()].sort().join(",");
  return <span data-testid="keys">{keys}</span>;
}

function RegisterProbe({ sessions }: { sessions: RunnerHealthInput[] }) {
  // The registration is the side effect we assert on (via the mocked
  // useRunnerHealth input).
  useRunnerHealthRegistration(sessions);
  return null;
}

// Render inside a router (the provider reads the active conv from the
// URL via `useLocation`) and the provider itself. `initialEntries`
// drives which conversation is "active".
function renderInProvider(ui: ReactNode, initialEntries: string[] = ["/"]) {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <RunnerHealthProvider>{ui}</RunnerHealthProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  useConvMock.mockReturnValue({
    data: { pages: [{ data: [] }] },
  } as unknown as ReturnType<typeof useConversations>);
  // The fallback poll resolves these (used by tests that put the open
  // session through the poll path).
  useRunnerHealthMock.mockReturnValue(new Map<string, SessionLiveness>());
  useSessionMock.mockReturnValue({
    session: null,
    isLoading: false,
    error: null,
  });
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("useSessionRunnerOnline (stream-sourced for sidebar rows)", () => {
  it("returns runner_online from the streamed sidebar row", () => {
    // The WS stream lands runner_online on the cached conversation rows;
    // the provider reads it directly (no poll for sidebar sessions).
    useConvMock.mockReturnValue({
      data: { pages: [{ data: [{ id: "conv_online", runner_online: true }] }] },
    } as unknown as ReturnType<typeof useConversations>);
    renderInProvider(<OnlineProbe sessionId="conv_online" />);
    expect(screen.getByTestId("probe").textContent).toBe("true");
  });

  it("returns false for a streamed-offline row", () => {
    // Load-bearing: callers gate on `=== false`. If this ever returned
    // `undefined` for offline sessions the gate would break.
    useConvMock.mockReturnValue({
      data: { pages: [{ data: [{ id: "conv_offline", runner_online: false }] }] },
    } as unknown as ReturnType<typeof useConversations>);
    renderInProvider(<OnlineProbe sessionId="conv_offline" />);
    expect(screen.getByTestId("probe").textContent).toBe("false");
  });

  it("returns undefined for an unknown session", () => {
    renderInProvider(<OnlineProbe sessionId="conv_never_seen" />);
    expect(screen.getByTestId("probe").textContent).toBe("undefined");
  });

  it("returns undefined when sessionId is undefined", () => {
    renderInProvider(<OnlineProbe sessionId={undefined} />);
    expect(screen.getByTestId("probe").textContent).toBe("undefined");
  });

  it("returns undefined outside the provider", () => {
    render(<OnlineProbe sessionId="conv_online" />);
    expect(screen.getByTestId("probe").textContent).toBe("undefined");
  });
});

describe("useSessionHostOnline (host tunnel, tri-state)", () => {
  it("exposes host_online=true from the streamed sidebar row", () => {
    useConvMock.mockReturnValue({
      data: {
        pages: [{ data: [{ id: "conv_a", runner_online: false, host_online: true }] }],
      },
    } as unknown as ReturnType<typeof useConversations>);
    renderInProvider(<HostProbe sessionId="conv_a" />);
    // The dead-runner-on-live-host case the open-session view keys on.
    expect(screen.getByTestId("host-probe").textContent).toBe("true");
  });

  it("exposes host_online=null for a non-host-bound row", () => {
    useConvMock.mockReturnValue({
      data: {
        pages: [{ data: [{ id: "conv_a", runner_online: true, host_online: null }] }],
      },
    } as unknown as ReturnType<typeof useConversations>);
    renderInProvider(<HostProbe sessionId="conv_a" />);
    expect(screen.getByTestId("host-probe").textContent).toBe("null");
  });

  it("returns undefined when the row carries no host_online field", () => {
    // Older server: host_online absent (undefined) → unknown, not null.
    useConvMock.mockReturnValue({
      data: { pages: [{ data: [{ id: "conv_a", runner_online: true }] }] },
    } as unknown as ReturnType<typeof useConversations>);
    renderInProvider(<HostProbe sessionId="conv_a" />);
    expect(screen.getByTestId("host-probe").textContent).toBe("undefined");
  });

  it("surfaces host_online from the fallback poll for the open session", () => {
    // Off-sidebar open child: not in the conversations cache, so its
    // liveness comes from the /health fallback poll.
    useSessionMock.mockReturnValue({
      session: { id: "conv_child" },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSession>);
    useRunnerHealthMock.mockReturnValue(
      new Map<string, SessionLiveness>([["conv_child", liveness(false, true)]]),
    );
    renderInProvider(<HostProbe sessionId="conv_child" />, ["/c/conv_child"]);
    expect(screen.getByTestId("host-probe").textContent).toBe("true");
  });

  it("surfaces the host version from the fallback poll for the open session", () => {
    // The info-popover footer reads the bound host's version; it rides the
    // same /health poll as host_online (poll-only — not the sidebar stream).
    useSessionMock.mockReturnValue({
      session: { id: "conv_child" },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSession>);
    useRunnerHealthMock.mockReturnValue(
      new Map<string, SessionLiveness>([["conv_child", liveness(false, true, "0.1.0")]]),
    );
    renderInProvider(<HostVersionProbe sessionId="conv_child" />, ["/c/conv_child"]);
    expect(screen.getByTestId("host-version-probe").textContent).toBe("0.1.0");
  });
});

describe("RunnerHealthProvider deduplication", () => {
  it("invokes the underlying hooks once regardless of consumer count", () => {
    // The point of hoisting into a provider — a regression here means
    // multiple batch /health pollers run in parallel.
    renderInProvider(
      <>
        <OnlineProbe sessionId="conv_a" />
        <HostProbe sessionId="conv_a" />
        <MapKeysProbe />
      </>,
    );
    expect(useConvMock).toHaveBeenCalledTimes(1);
    expect(useRunnerHealthMock).toHaveBeenCalledTimes(1);
  });
});

describe("RunnerHealthProvider open-session-scoped fallback poll", () => {
  it("polls the open session but NOT the sidebar list (stream covers it)", () => {
    // Sidebar carries its own rows; those are stream-sourced, never polled.
    // Only the open session (off-sidebar child here) goes through /health.
    useConvMock.mockReturnValue({
      data: { pages: [{ data: [{ id: "conv_parent", runner_online: true }] }] },
    } as unknown as ReturnType<typeof useConversations>);
    useSessionMock.mockReturnValue({
      session: { id: "conv_child" },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSession>);

    renderInProvider(<MapKeysProbe />, ["/c/conv_child"]);

    const polled = useRunnerHealthMock.mock.calls.at(-1)?.[0];
    // The narrowed fallback set is the open session only — the sidebar
    // parent is no longer polled fleet-wide.
    expect(polled?.map((s) => s.id)).toEqual(["conv_child"]);
  });

  it("polls nothing when no session is open", () => {
    useConvMock.mockReturnValue({
      data: { pages: [{ data: [{ id: "conv_a", runner_online: true }] }] },
    } as unknown as ReturnType<typeof useConversations>);
    renderInProvider(<MapKeysProbe />, ["/"]);
    const polled = useRunnerHealthMock.mock.calls.at(-1)?.[0];
    expect(polled).toEqual([]);
  });

  it("always polls the open session even when the stream covers it", () => {
    // Stream connected AND the open session is a sidebar row the stream
    // reports — but the open session is STILL polled directly. The stream
    // only re-emits on DB changes, so a runner-tunnel drop (in-memory) would
    // leave its stream runner_online stale-online; the /health poll is the
    // tunnel-accurate source for the session the user is looking at.
    useConvMock.mockReturnValue({
      data: { pages: [{ data: [{ id: "conv_open", runner_online: true }] }] },
    } as unknown as ReturnType<typeof useConversations>);
    useSessionMock.mockReturnValue({
      session: { id: "conv_open" },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSession>);

    renderInProvider(<MapKeysProbe />, ["/c/conv_open"]);

    const polled = useRunnerHealthMock.mock.calls.at(-1)?.[0];
    expect(polled?.map((s) => s.id)).toEqual(["conv_open"]);
    expect(screen.getByTestId("keys").textContent).toBe("conv_open");
  });

  it("still polls the open session while stream is connected if it's off-sidebar", () => {
    // Connected but the open child isn't a sidebar row → stream doesn't
    // cover it, so the fallback poll must still pick it up.
    useConvMock.mockReturnValue({
      data: { pages: [{ data: [{ id: "conv_sidebar", runner_online: true }] }] },
    } as unknown as ReturnType<typeof useConversations>);
    useSessionMock.mockReturnValue({
      session: { id: "conv_child" },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSession>);

    renderInProvider(<MapKeysProbe />, ["/c/conv_child"]);

    const polled = useRunnerHealthMock.mock.calls.at(-1)?.[0];
    expect(polled?.map((s) => s.id)).toEqual(["conv_child"]);
  });
});

describe("RunnerHealthProvider stream + poll merge", () => {
  it("exposes both stream-covered sidebar rows and the polled open child", () => {
    useConvMock.mockReturnValue({
      data: { pages: [{ data: [{ id: "conv_sidebar", runner_online: true }] }] },
    } as unknown as ReturnType<typeof useConversations>);
    useSessionMock.mockReturnValue({
      session: { id: "conv_child" },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSession>);
    useRunnerHealthMock.mockReturnValue(
      new Map<string, SessionLiveness>([["conv_child", liveness(false, true)]]),
    );

    renderInProvider(<MapKeysProbe />, ["/c/conv_child"]);

    // The exposed runner map unions the stream-covered sidebar row and the
    // poll-covered open child.
    expect(screen.getByTestId("keys").textContent).toBe("conv_child,conv_sidebar");
  });

  it("lets the poll win over the stream for a session both cover", () => {
    // A session the stream reports online (stale, e.g. its runner tunnel
    // just dropped — an in-memory event the stream never pushed) while the
    // direct /health poll reports offline. The poll is the tunnel-accurate
    // source for a covered session, so its fresh offline must override the
    // stale stream online — otherwise the reconnect/fork banner never shows.
    useConvMock.mockReturnValue({
      data: { pages: [{ data: [{ id: "conv_x", runner_online: true }] }] },
    } as unknown as ReturnType<typeof useConversations>);
    useRunnerHealthMock.mockReturnValue(
      new Map<string, SessionLiveness>([["conv_x", liveness(false, false)]]),
    );

    renderInProvider(<OnlineProbe sessionId="conv_x" />);
    expect(screen.getByTestId("probe").textContent).toBe("false");
  });
});

describe("RunnerHealthProvider registration", () => {
  it("folds a registrant's extra sessions into the fallback poll", () => {
    // The whole point: a transient view's sessions ride the one poller
    // instead of standing up a second /health loop.
    useConvMock.mockReturnValue({
      data: { pages: [{ data: [{ id: "conv_sidebar", runner_online: true }] }] },
    } as unknown as ReturnType<typeof useConversations>);

    renderInProvider(<RegisterProbe sessions={[{ id: "conv_extra", runner_id: "runner_e" }]} />);

    const polled = useRunnerHealthMock.mock.calls.at(-1)?.[0];
    // The sidebar row is stream-covered (not polled); the registrant rides
    // the fallback poll.
    expect(polled?.map((s) => s.id)).toEqual(["conv_extra"]);
  });

  it("unions a registrant with the open session in the fallback poll", () => {
    useSessionMock.mockReturnValue({
      session: { id: "conv_open" },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSession>);

    renderInProvider(<RegisterProbe sessions={[{ id: "conv_extra", runner_id: "runner_e" }]} />, [
      "/c/conv_open",
    ]);

    const polled = useRunnerHealthMock.mock.calls.at(-1)?.[0];
    expect(polled?.map((s) => s.id).sort()).toEqual(["conv_extra", "conv_open"]);
  });

  it("does not duplicate a registrant that is also the open session", () => {
    useSessionMock.mockReturnValue({
      session: { id: "conv_shared" },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof useSession>);

    renderInProvider(<RegisterProbe sessions={[{ id: "conv_shared", runner_id: "runner_x" }]} />, [
      "/c/conv_shared",
    ]);

    const polled = useRunnerHealthMock.mock.calls.at(-1)?.[0];
    expect(polled?.map((s) => s.id)).toEqual(["conv_shared"]);
  });

  it("drops the registrant's sessions from the poll on unmount", () => {
    const { rerender } = renderInProvider(
      <RegisterProbe sessions={[{ id: "conv_extra", runner_id: "runner_e" }]} />,
    );
    expect(useRunnerHealthMock.mock.calls.at(-1)?.[0]?.map((s) => s.id)).toEqual(["conv_extra"]);

    // Unmount the registrant — its key is removed so the extra session
    // leaves the poll set (no leaked background polling once the view closes).
    rerender(
      <MemoryRouter initialEntries={["/"]}>
        <RunnerHealthProvider>{null}</RunnerHealthProvider>
      </MemoryRouter>,
    );
    expect(useRunnerHealthMock.mock.calls.at(-1)?.[0]).toEqual([]);
  });
});
