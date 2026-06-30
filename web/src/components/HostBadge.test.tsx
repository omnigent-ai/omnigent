import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HostBadge, resolveHostBadge } from "./HostBadge";
import type { Host } from "@/hooks/useHosts";

function host(overrides: Partial<Host> = {}): Host {
  return {
    host_id: "host_a1b2",
    name: "mac-laptop",
    owner: "alice",
    status: "online",
    sandbox_provider: null,
    ...overrides,
  };
}

describe("resolveHostBadge", () => {
  it("returns null when the session is not host-bound", () => {
    expect(resolveHostBadge({ hostId: null, host: undefined, online: null })).toBeNull();
    expect(resolveHostBadge({ hostId: undefined, host: undefined, online: undefined })).toBeNull();
  });

  it("uses the friendly host name and online status for a connected host", () => {
    expect(resolveHostBadge({ hostId: "host_a1b2", host: host(), online: true })).toEqual({
      label: "mac-laptop",
      status: "online",
    });
  });

  it("reports offline status for a connected host", () => {
    expect(resolveHostBadge({ hostId: "host_a1b2", host: host(), online: false })).toEqual({
      label: "mac-laptop",
      status: "offline",
    });
  });

  it("labels a sandbox host by provider, not its managed-* name", () => {
    expect(
      resolveHostBadge({
        hostId: "host_sb",
        host: host({ host_id: "host_sb", name: "managed-abc123", sandbox_provider: "lakebox" }),
        online: true,
      }),
    ).toEqual({ label: "Databricks Sandbox", status: "online" });
  });

  it("falls back to the raw host_id when the host record is unresolved", () => {
    // Shared session on another owner's host, or hosts not loaded yet:
    // there's no name to show, but the session IS host-bound, so the
    // badge must still answer "which host" with the id.
    expect(resolveHostBadge({ hostId: "host_x9", host: undefined, online: true })).toEqual({
      label: "host_x9",
      status: "online",
    });
  });

  it("reports unknown status while liveness is still settling", () => {
    // null = not-host-bound signal from the health stream; undefined =
    // not yet observed. Neither should flash a red 'offline' circle.
    expect(
      resolveHostBadge({ hostId: "host_a1b2", host: host(), online: undefined }),
    ).toMatchObject({ status: "unknown" });
    expect(resolveHostBadge({ hostId: "host_a1b2", host: host(), online: null })).toMatchObject({
      status: "unknown",
    });
  });
});

// Stub the data hooks so the component test drives label/status purely
// from inputs (resolveHostBadge is covered separately above).
const useSessionMock = vi.fn();
const useHostsMock = vi.fn();
const useSessionHostOnlineMock = vi.fn();

vi.mock("@/hooks/useSession", () => ({
  useSession: (id: string | null | undefined) => useSessionMock(id),
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: (opts: unknown) => useHostsMock(opts),
}));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionHostOnline: (id: string | undefined) => useSessionHostOnlineMock(id),
}));

beforeEach(() => {
  useSessionMock.mockReset();
  useHostsMock.mockReset();
  useSessionHostOnlineMock.mockReset();
  // Sensible defaults; each test overrides what it cares about.
  useSessionMock.mockReturnValue({
    session: { hostId: "host_a1b2" },
    isLoading: false,
    error: null,
  });
  useHostsMock.mockReturnValue({
    data: [
      {
        host_id: "host_a1b2",
        name: "mac-laptop",
        owner: "alice",
        status: "online",
        sandbox_provider: null,
      },
    ],
  });
  useSessionHostOnlineMock.mockReturnValue(true);
});

afterEach(() => cleanup());

describe("HostBadge", () => {
  it("renders the host name with an online status when reachable", () => {
    render(<HostBadge sessionId="conv_1" />);
    const badge = screen.getByTestId("host-badge");
    // Status is conveyed by the visible name + an sr-only status word
    // (read together as "mac-laptop, online"), with `title` for hover.
    expect(badge.textContent).toBe("mac-laptop, online");
    expect(badge.getAttribute("title")).toBe("Host mac-laptop, online");
  });

  it("renders nothing when the session is not host-bound", () => {
    useSessionMock.mockReturnValue({ session: { hostId: null }, isLoading: false, error: null });
    render(<HostBadge sessionId="conv_1" />);
    expect(screen.queryByTestId("host-badge")).toBeNull();
  });

  it("labels a sandbox session by provider, requesting sandbox hosts", () => {
    useSessionMock.mockReturnValue({
      session: { hostId: "host_sb" },
      isLoading: false,
      error: null,
    });
    useHostsMock.mockReturnValue({
      data: [
        {
          host_id: "host_sb",
          name: "managed-abc123",
          owner: "alice",
          status: "online",
          sandbox_provider: "lakebox",
        },
      ],
    });
    render(<HostBadge sessionId="conv_1" />);
    expect(screen.getByTestId("host-badge").textContent).toContain("Databricks Sandbox");
    // The badge must opt into sandbox hosts — the default filtered call
    // would drop this host and the label would regress to the raw id.
    expect(useHostsMock).toHaveBeenCalledWith(expect.objectContaining({ includeSandbox: true }));
  });

  it("falls back to the raw host_id when the host isn't in the list", () => {
    useSessionMock.mockReturnValue({
      session: { hostId: "host_x9" },
      isLoading: false,
      error: null,
    });
    useHostsMock.mockReturnValue({ data: [] });
    render(<HostBadge sessionId="conv_1" />);
    expect(screen.getByTestId("host-badge").textContent).toContain("host_x9");
  });

  it("falls back to the host record's status when liveness is unobserved", () => {
    // liveOnline === undefined (stream hasn't reported yet) → trust the
    // stored host.status so an offline record reads offline, not unknown.
    useHostsMock.mockReturnValue({
      data: [
        {
          host_id: "host_a1b2",
          name: "mac-laptop",
          owner: "alice",
          status: "offline",
          sandbox_provider: null,
        },
      ],
    });
    useSessionHostOnlineMock.mockReturnValue(undefined);
    render(<HostBadge sessionId="conv_1" />);
    expect(screen.getByTestId("host-badge").textContent).toBe("mac-laptop, offline");
  });

  it("lets a live offline signal override a stale 'online' host record", () => {
    // host record says online, but the live stream says the host is down
    // (false). The live signal wins — false must not be discarded.
    useHostsMock.mockReturnValue({
      data: [
        {
          host_id: "host_a1b2",
          name: "mac-laptop",
          owner: "alice",
          status: "online",
          sandbox_provider: null,
        },
      ],
    });
    useSessionHostOnlineMock.mockReturnValue(false);
    render(<HostBadge sessionId="conv_1" />);
    expect(screen.getByTestId("host-badge").textContent).toBe("mac-laptop, offline");
  });

  it("shows unknown (not a stale green) when the stream reports not-host-bound", () => {
    // Shared-session race: the host record says online, but the live
    // stream returns null ("not host-bound"). null must reach the badge
    // as "unknown" rather than falling through to the record's online.
    useHostsMock.mockReturnValue({
      data: [
        {
          host_id: "host_a1b2",
          name: "mac-laptop",
          owner: "alice",
          status: "online",
          sandbox_provider: null,
        },
      ],
    });
    useSessionHostOnlineMock.mockReturnValue(null);
    render(<HostBadge sessionId="conv_1" />);
    expect(screen.getByTestId("host-badge").textContent).toBe("mac-laptop, status unknown");
  });

  it("shows unknown while the host list is still loading", () => {
    // hosts === undefined and no live signal yet: nothing to derive
    // status from → unknown, and the raw id stands in for the name.
    useSessionMock.mockReturnValue({
      session: { hostId: "host_x9" },
      isLoading: false,
      error: null,
    });
    useHostsMock.mockReturnValue({ data: undefined });
    useSessionHostOnlineMock.mockReturnValue(undefined);
    render(<HostBadge sessionId="conv_1" />);
    expect(screen.getByTestId("host-badge").textContent).toBe("host_x9, status unknown");
  });
});
