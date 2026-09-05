import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  STARTING_GRACE_S,
  type LivenessRow,
  livenessRowFromSession,
  useSessionLiveness,
} from "./useSessionLiveness";
import { useSessionHostOnline, useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import type { Session } from "@/lib/types";

// Drive the two split signals directly so the test pins the derivation
// truth table, not the provider plumbing (covered by its own test).
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: vi.fn(),
  useSessionHostOnline: vi.fn(),
}));

const runnerMock = vi.mocked(useSessionRunnerOnline);
const hostMock = vi.mocked(useSessionHostOnline);

const SID = "sess-1";

// A `created_at` far in the past (Unix seconds, ~Nov 2023) so the startup
// grace (see STARTING_GRACE_S) never applies to these baseline fixtures —
// they exercise the steady-state truth table, not the cold-boot window. The
// grace itself is covered by its own describe block below with a fresh value.
const SOME_CREATED_AT = 1_700_000_000;

/** A `created_at` (Unix seconds) inside the startup grace window. */
function freshCreatedAt(): number {
  return Math.floor(Date.now() / 1000);
}

/** Build a minimal conv row carrying just the fields the hook reads. */
function conv(partial: Partial<LivenessRow>): LivenessRow {
  return {
    host_id: null,
    permission_level: null,
    created_at: SOME_CREATED_AT,
    host_resumable: false,
    ...partial,
  };
}

function derive(
  runner: boolean | undefined,
  host: boolean | null | undefined,
  c: LivenessRow | null,
  opts?: { turnActive?: boolean; launchedAt?: number | null },
) {
  runnerMock.mockReturnValue(runner);
  hostMock.mockReturnValue(host);
  return renderHook(() => useSessionLiveness(SID, c, opts)).result.current;
}

describe("useSessionLiveness — derivation truth table", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("online whenever the runner tunnel is up, regardless of host", () => {
    // A live runner short-circuits — host-down/null must not override it.
    expect(derive(true, true, conv({ host_id: "h1" }))).toEqual({ kind: "online" });
    expect(derive(true, false, conv({ host_id: "h1" }))).toEqual({ kind: "online" });
    expect(derive(true, null, conv({}))).toEqual({ kind: "online" });
  });

  it("unknown while the runner has not been observed (pre-poll)", () => {
    // undefined must not flash any reconnect affordance over a session
    // that may well be live.
    expect(derive(undefined, true, conv({ host_id: "h1" }))).toEqual({ kind: "unknown" });
    expect(derive(undefined, undefined, conv({}))).toEqual({ kind: "unknown" });
  });

  it("runner_asleep when the runner is down, host up, and no turn is in flight", () => {
    // The host relaunches the runner on the next message — composer stays
    // open, no banner. Holds whether or not host_id is on the row.
    expect(derive(false, true, conv({ host_id: "h1" }))).toEqual({ kind: "runner_asleep" });
    expect(derive(false, true, conv({}))).toEqual({ kind: "runner_asleep" });
  });

  it("starting (NOT runner_asleep) when the runner is down, host up, and a turn is in flight", () => {
    // A just-sent turn (turnActive) is relaunching the runner now — surface
    // the "Connecting…" intermediate instead of a silent gap.
    expect(derive(false, true, conv({ host_id: "h1" }), { turnActive: true })).toEqual({
      kind: "starting",
    });
    // Without a turn in flight the same inputs stay idle-asleep.
    expect(derive(false, true, conv({ host_id: "h1" }), { turnActive: false })).toEqual({
      kind: "runner_asleep",
    });
  });

  it("turnActive does not override a confirmed-dead host (host_offline wins)", () => {
    // A turn in flight can't relaunch a runner when the host itself is gone —
    // the actionable banner must still win.
    expect(derive(false, false, conv({ host_id: "h1" }), { turnActive: true })).toEqual({
      kind: "host_offline",
      isOwner: true,
    });
  });

  it("host_offline (owner) for a host-bound session with the host down", () => {
    // permission_level null = not shared = owner.
    expect(derive(false, false, conv({ host_id: "h1", permission_level: null }))).toEqual({
      kind: "host_offline",
      isOwner: true,
    });
    // level >= 4 is the owner grant.
    expect(derive(false, false, conv({ host_id: "h1", permission_level: 4 }))).toEqual({
      kind: "host_offline",
      isOwner: true,
    });
  });

  it("host_offline (non-owner) for a shared host-bound session with the host down", () => {
    expect(derive(false, false, conv({ host_id: "h1", permission_level: 1 }))).toEqual({
      kind: "host_offline",
      isOwner: false,
    });
    expect(derive(false, false, conv({ host_id: "h1", permission_level: 3 }))).toEqual({
      kind: "host_offline",
      isOwner: false,
    });
  });

  it("host_asleep when a resumable managed host is down — composer stays open", () => {
    // A dormant resumable managed host the server wakes on the next message:
    // NOT the host_offline dead-end. host_resumable flips row 3.
    expect(derive(false, false, conv({ host_id: "h1", host_resumable: true }))).toEqual({
      kind: "host_asleep",
    });
    // The wake is server-side, not owner-gated: resumable wins the offline
    // split even when shared (non-owner).
    expect(
      derive(false, false, conv({ host_id: "h1", permission_level: 1, host_resumable: true })),
    ).toEqual({ kind: "host_asleep" });
  });

  it("starting (NOT host_asleep) while a just-sent turn is waking the resumable host", () => {
    // turnActive means the send is resuming the sandbox now — show the
    // "Connecting…" intermediate through the cold wake, not a blank
    // host_asleep screen.
    expect(
      derive(false, false, conv({ host_id: "h1", host_resumable: true }), { turnActive: true }),
    ).toEqual({ kind: "starting" });
  });

  it("host_offline (not host_asleep) when the down host is NOT resumable", () => {
    // The default for an external/non-resumable host: the actionable
    // reconnect/fork dead-end, unchanged.
    expect(derive(false, false, conv({ host_id: "h1", host_resumable: false }))).toEqual({
      kind: "host_offline",
      isOwner: true,
    });
  });

  it("unknown for a host-bound session whose host liveness is not yet observed", () => {
    // host_id set but host_online undefined: don't guess host-down.
    expect(derive(false, undefined, conv({ host_id: "h1" }))).toEqual({ kind: "unknown" });
  });

  it("local_stranded when not host-bound and the runner is down", () => {
    // host_online is null for non-host-bound sessions; false is also
    // tolerated. No host exists to relaunch — restart from the machine.
    expect(derive(false, null, conv({ host_id: null }))).toEqual({ kind: "local_stranded" });
    expect(derive(false, false, conv({ host_id: null }))).toEqual({ kind: "local_stranded" });
  });

  it("runner_asleep (not local_stranded) for sub-agent sessions with dead runner", () => {
    // Sub-agents have no host binding but recover via their parent's live
    // runner (server-side heal on message send). They must never be shown
    // the CLI reconnect modal — keep the composer open.
    expect(derive(false, null, conv({ host_id: null, kind: "sub_agent" }))).toEqual({
      kind: "runner_asleep",
    });
    expect(derive(false, false, conv({ host_id: null, kind: "sub_agent" }))).toEqual({
      kind: "runner_asleep",
    });
  });

  describe("host-switch launch grace", () => {
    it("starting while a just-launched runner has not registered yet", () => {
      // A switch binds a runner on the new host, but nothing on the wire
      // says so: the session is old (no created_at grace) and no turn is in
      // flight. Steady-state with these inputs is runner_asleep, which
      // renders NO indicator — the move would land on a silent empty chat.
      expect(derive(false, true, conv({ host_id: "h2" }), { launchedAt: Date.now() })).toEqual({
        kind: "starting",
      });
    });

    it("applies even though a runner was previously online", () => {
      // Deliberately not gated on "ever online": the whole point of a switch
      // is that the old runner WAS up and is now gone, so the session
      // genuinely re-enters cold boot. Same-mount rerender so the hook's
      // ever-online ref carries the earlier `true`.
      const c = conv({ host_id: "h2" });
      hostMock.mockReturnValue(true);
      runnerMock.mockReturnValue(true);
      const launchedAt = Date.now();
      const { result, rerender } = renderHook(
        (props: { launchedAt: number | null }) =>
          useSessionLiveness(SID, c, { launchedAt: props.launchedAt }),
        { initialProps: { launchedAt: null as number | null } },
      );
      expect(result.current).toEqual({ kind: "online" });

      runnerMock.mockReturnValue(false);
      rerender({ launchedAt });
      expect(result.current).toEqual({ kind: "starting" });
    });

    it("expires so a launch that never lands still reaches its real state", () => {
      // The marker is a grace, not a latch — a runner that never registers
      // must fall through to the actionable state instead of spinning
      // forever.
      const stale = Date.now() - (STARTING_GRACE_S + 1) * 1000;
      expect(derive(false, true, conv({ host_id: "h2" }), { launchedAt: stale })).toEqual({
        kind: "runner_asleep",
      });
    });
  });

  describe("startup grace (fresh session, runner not yet registered)", () => {
    it("starting for a just-created session whose runner is offline", () => {
      // A brand-new session's runner hasn't registered its tunnel yet, so the
      // poll reads `false` — but within the grace window that's cold-boot, not
      // stranded. Without the grace this would be local_stranded (host null)
      // and flash a reconnect banner over a session that's simply starting.
      expect(derive(false, null, conv({ host_id: null, created_at: freshCreatedAt() }))).toEqual({
        kind: "starting",
      });
    });

    it("imported session skips the grace → local_stranded even when fresh", () => {
      // An import has no runner booting, so the cold-boot grace must not apply:
      // a fresh import reads local_stranded at once (offering the resume
      // picker) instead of "Connecting…" for the whole grace window. Same
      // inputs as the just-created case above, which is `starting`.
      expect(
        derive(false, null, conv({ host_id: null, created_at: freshCreatedAt(), imported: true })),
      ).toEqual({ kind: "local_stranded" });
    });

    it("starting wins over host_offline for a fresh host-bound session", () => {
      // The grace precedes the host_offline check: a new host-bound session
      // whose host hasn't registered yet must not flash "host offline" either.
      // Steady-state (old created_at) with these same inputs is host_offline
      // (asserted above), which proves the grace — not host state — is what
      // flips this to starting.
      expect(derive(false, false, conv({ host_id: "h1", created_at: freshCreatedAt() }))).toEqual({
        kind: "starting",
      });
    });

    it("starting even pre-poll (runner undefined) for a fresh session", () => {
      // Before the first poll resolves, a fresh session is starting, not the
      // generic `unknown` — the grace gives it a concrete "Connecting…" state.
      expect(
        derive(undefined, undefined, conv({ host_id: null, created_at: freshCreatedAt() })),
      ).toEqual({ kind: "starting" });
    });

    it("online still wins over the grace once the runner tunnel is up", () => {
      // The runner short-circuit precedes the grace: a fresh session whose
      // runner registered quickly is online, not stuck in "starting".
      expect(derive(true, null, conv({ host_id: null, created_at: freshCreatedAt() }))).toEqual({
        kind: "online",
      });
    });

    it("a crash after the runner was online defeats the grace (local_stranded, not masked)", () => {
      // Regression (tests/e2e_ui/test_stale_stream.py::
      // test_stale_banner_on_runner_crash): killing the runner of a
      // freshly-created session must surface the reconnect banner, not stay
      // masked as cold-boot "starting" for the whole grace window. The grace
      // only stands in for the INITIAL boot; once the runner has been observed
      // online, a later `false` is a genuine crash. Same-mount rerender so the
      // hook's "ever online" ref carries the earlier `true` observation.
      const c = conv({ host_id: null, created_at: freshCreatedAt() });
      hostMock.mockReturnValue(null);
      runnerMock.mockReturnValue(true);
      const { result, rerender } = renderHook(() => useSessionLiveness(SID, c));
      expect(result.current).toEqual({ kind: "online" });

      // SIGKILL: the poll flips the runner to offline. created_at is still
      // fresh, but the grace must not re-mask a confirmed crash.
      runnerMock.mockReturnValue(false);
      rerender();
      expect(result.current).toEqual({ kind: "local_stranded" });
    });

    it("a crash after the runner was online surfaces host_offline for a host-bound session", () => {
      // Same gate for the host-bound path: once the runner has been online,
      // a fresh-but-crashed host-bound session whose host is also down reads
      // host_offline, not a grace-masked "starting".
      const c = conv({ host_id: "h1", created_at: freshCreatedAt() });
      runnerMock.mockReturnValue(true);
      hostMock.mockReturnValue(true);
      const { result, rerender } = renderHook(() => useSessionLiveness(SID, c));
      expect(result.current).toEqual({ kind: "online" });

      runnerMock.mockReturnValue(false);
      hostMock.mockReturnValue(false);
      rerender();
      expect(result.current).toEqual({ kind: "host_offline", isOwner: true });
    });
  });

  it("unknown when there is no open session", () => {
    runnerMock.mockReturnValue(undefined);
    hostMock.mockReturnValue(undefined);
    const { result } = renderHook(() => useSessionLiveness(undefined, null));
    expect(result.current).toEqual({ kind: "unknown" });
  });

  it("treats a missing conv as not-host-bound (local_stranded) when the runner is down", () => {
    // conv null while loading + runner down + host null → no host to
    // reconnect, so the local-restart affordance is the safe default.
    expect(derive(false, null, null)).toEqual({ kind: "local_stranded" });
  });

  describe("livenessRowFromSession (snapshot fallback for off-sidebar sessions)", () => {
    function session(partial: Partial<Session>): Session {
      // Only the fields the row carries matter; the rest are filler.
      return {
        id: "conv_x",
        agentId: "ag_x",
        agentName: null,
        hostId: null,
        status: "idle",
        createdAt: SOME_CREATED_AT,
        title: null,
        items: [],
        permissionLevel: null,
        parentSessionId: null,
        ...partial,
      } as Session;
    }

    it("maps hostId / permissionLevel / createdAt / hostResumable / kind into the snake_case row", () => {
      expect(
        livenessRowFromSession(session({ hostId: "h1", permissionLevel: 1, createdAt: 123 })),
      ).toEqual({
        host_id: "h1",
        permission_level: 1,
        created_at: 123,
        host_resumable: false,
        kind: undefined,
        imported: false,
      });
      // hostResumable flows through so an off-sidebar resumable host can
      // classify host_asleep rather than dead-ending on host_offline.
      expect(livenessRowFromSession(session({ hostId: "h1", hostResumable: true }))).toMatchObject({
        host_resumable: true,
      });
      // kind flows through so sub-agent sessions are not misclassified.
      expect(livenessRowFromSession(session({ kind: "sub_agent" }))).toMatchObject({
        kind: "sub_agent",
      });
    });

    it("returns null for a null/undefined snapshot", () => {
      expect(livenessRowFromSession(null)).toBeNull();
      expect(livenessRowFromSession(undefined)).toBeNull();
    });

    it("a host-bound snapshot-derived row classifies host_offline (NOT local_stranded)", () => {
      // P1-1 regression: a directly-opened host-bound session absent from the
      // sidebar must keep its host_id via the snapshot fallback, so a dead
      // host shows the owner-gated reconnect path — not the local-restart hint
      // that a lost host_id (→ local_stranded) would give.
      const row = livenessRowFromSession(session({ hostId: "h1", permissionLevel: null }));
      expect(derive(false, false, row)).toEqual({ kind: "host_offline", isOwner: true });
    });
  });
});
