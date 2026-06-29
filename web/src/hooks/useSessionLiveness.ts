// Single derivation of the open session's liveness, folding the two
// split signals (`runner_online` + `host_online`) plus host-binding and
// ownership into one discriminated union the open-session view switches
// on. Centralizing the truth table here keeps ChatPage from re-deriving
// "is the runner asleep vs. is the host down vs. is this not host-bound"
// inline at every render site.

import { useRef } from "react";

import type { Conversation } from "@/hooks/useConversations";
import type { Session } from "@/lib/types";
import { useSessionHostOnline, useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";

/**
 * How long (seconds) after a session is created to treat it as
 * `starting` rather than unreachable while its runner tunnel hasn't
 * registered yet.
 *
 * Runner liveness is poll-driven (the real-time `session.runner_status`
 * push was removed upstream), so for up to one
 * `RunnerHealthProvider` poll interval (~10s) after a brand-new session
 * is created, the poll still reads `runner_online: false`/`undefined`
 * even though the runner is mid-cold-boot. Without a grace window the
 * open view would flash a reconnect/fork banner over a session that is
 * simply starting. Set generously above the poll interval (plus runner
 * cold-start headroom) so the banner only appears once a launch has
 * genuinely had time to register; the moment the poll reports
 * `runner_online: true` the state flips to `online` regardless, so this
 * is only an upper bound on the "Connecting…" window, not a fixed delay.
 */
export const STARTING_GRACE_S = 45;

/** The subset of a conversation row this hook reads. */
export type LivenessRow = Pick<Conversation, "host_id" | "permission_level" | "created_at"> & {
  /**
   * Whether this session's host is a resumable managed host the server wakes
   * on the next message. NOT a `Conversation` field — the sidebar row doesn't
   * carry it; it rides the session snapshot, and the open view splices it in
   * via {@link livenessRowFromSession}. Drives the `host_asleep` vs
   * `host_offline` split (row 3). Absent ⇒ treated `false`.
   */
  host_resumable?: boolean;
};

/**
 * Build a {@link LivenessRow} from the single-session snapshot
 * ({@link Session}) for callers whose sidebar row (`Conversation`) is
 * absent — a directly-opened `/c/:id`, a child/sub-agent session, or any
 * session outside the loaded sidebar page. Without this, those sessions
 * pass `null` to {@link useSessionLiveness}, lose their `host_id`, and a
 * host-bound-but-host-down session misclassifies as `local_stranded`
 * (wrong CLI hint, wrong owner gating) instead of `host_offline`. The
 * snapshot carries the same three fields, so it's an exact stand-in.
 */
export function livenessRowFromSession(
  session:
    | Pick<Session, "hostId" | "permissionLevel" | "createdAt" | "hostResumable">
    | null
    | undefined,
): LivenessRow | null {
  if (!session) return null;
  return {
    host_id: session.hostId,
    permission_level: session.permissionLevel,
    created_at: session.createdAt,
    host_resumable: session.hostResumable ?? false,
  };
}

/**
 * The open session's liveness, derived from the runner tunnel, the host
 * tunnel, host-binding, and the caller's ownership. Exactly one variant
 * is active at a time and each maps to a distinct affordance:
 *
 * - `online` — the runner tunnel is registered; normal chat.
 * - `starting` — a relaunch is in-flight: an asleep session that a
 *   just-sent turn is waking right now (runner down, host up, a turn in
 *   flight). Transient and expected — the open view shows a passive
 *   "Connecting…" indicator (terminal-first sessions show the pill's own
 *   spinner), the composer stays open, and there's nothing to act on.
 *   Distinct from `runner_asleep`, which is an idle session whose runner
 *   is down with no turn yet trying to wake it. (Runner liveness is
 *   poll-driven — the real-time `session.runner_status` push was removed
 *   upstream — so the *initial* cold launch of a brand-new session isn't
 *   specially flagged here; its terminal-pill spinner comes from
 *   `terminalPending` once the runner connects.)
 * - `runner_asleep` — runner down, host up, and no turn in flight. The
 *   host relaunches the runner on the next message, so the composer stays
 *   open. The open view renders no banner for this state — typing
 *   silently relaunches the runner (which then flips it to `starting`).
 * - `host_asleep` — the session is host-bound, the host tunnel is down, but
 *   the host is a resumable managed host: the server wakes the sandbox on the
 *   next message (the send-message relaunch path calls `resume_managed_host`).
 *   Treated like `runner_asleep` — the composer stays open, no reconnect
 *   banner, and typing wakes it. This is what makes the backend resume
 *   reachable from the web; without it a resumable host would dead-end on
 *   `host_offline` below. While a just-sent turn is waking it (`turnActive`),
 *   this upgrades to `starting` so the ~85s cold wake shows a "Connecting…"
 *   intermediate rather than a blank screen.
 * - `host_offline` — the session is host-bound and the host tunnel is down,
 *   and the host is NOT resumable from the web (an external/laptop host, or a
 *   managed provider without a stop/resume lifecycle). The owner must
 *   reconnect the host from that machine (`isOwner` true), and any viewer can
 *   fork to continue independently.
 * - `local_stranded` — not host-bound (no `host_id`) and the runner is
 *   down. There's no host to relaunch it; the user restarts from their
 *   own machine, and forking is the escape hatch.
 * - `unknown` — liveness hasn't been observed yet for this session (not
 *   polled, no stream row). Callers must NOT block on this — treat it as
 *   "assume online until proven otherwise" so a not-yet-resolved poll
 *   doesn't flash a reconnect banner over a live session.
 */
export type SessionLiveness =
  | { kind: "online" }
  | { kind: "starting" }
  | { kind: "runner_asleep" }
  | { kind: "host_asleep" }
  | { kind: "host_offline"; isOwner: boolean }
  | { kind: "local_stranded" }
  | { kind: "unknown" };

/**
 * True when the viewer owns `conv`. Mirrors the convention used by the
 * sidebar and header action gates (`Sidebar`/`AppShell`): a `null`
 * permission level means the session isn't shared (so the viewer is the
 * owner), and level >= 4 is the owner grant.
 */
function isOwner(conv: Pick<Conversation, "permission_level"> | null | undefined): boolean {
  const level = conv?.permission_level;
  return level === null || level === undefined || level >= 4;
}

/**
 * Derive the open session's {@link SessionLiveness} from the split
 * liveness signals.
 *
 * The truth table (runner / host / host_id / turn → state), in
 * precedence order — the first matching row wins:
 *
 * | # | runner_online | host_online | host_id | turnActive | → state              |
 * |---|---------------|-------------|---------|------------|----------------------|
 * | 1 | true          | (any)       | (any)   | (any)      | online               |
 * | 2 | not-true      | (any)       | (any)   | (any)      | starting (fresh*)    |
 * | 3 | not-true      | false       | set+resumable | true  | starting (waking)    |
 * | 3'| not-true      | false       | set+resumable | false | host_asleep          |
 * | 3"| not-true      | false       | set, non-resum| (any) | host_offline {owner} |
 * | 4 | undefined     | (any)       | (any)   | (any)      | unknown (pre-poll)   |
 * | 5 | false         | true        | (any)   | true       | starting (relaunch)  |
 * | 5'| false         | true        | (any)   | false      | runner_asleep        |
 * | 6 | false         | undefined   | set     | (any)      | unknown (host unseen)|
 * | 7 | false         | null/false  | null    | (any)      | local_stranded       |
 *
 * `*fresh` = `created_at` is within {@link STARTING_GRACE_S} of now AND
 * the runner tunnel has never been observed online (initial cold boot).
 * Once a runner has come online, a later `runner_online: false` is a real
 * crash, not cold boot, so the grace no longer applies and rows 3/5–7 win.
 *
 * `runner_online === true` short-circuits to `online` — a registered
 * runner tunnel is the only signal that means "chat now." A just-created
 * session whose runner hasn't registered yet (row 2) is cold-booting, not
 * stranded: surface `starting` rather than a reconnect banner until the
 * grace window lapses. A confirmed host-down splits on resumability: a
 * resumable managed host is `host_asleep` (row 3 — wakeable by sending a
 * message, composer open), a non-resumable one is `host_offline` (row 3' —
 * reconnect / fork).
 * Pre-poll `undefined` (row 4) stays `unknown` so a not-yet-resolved poll
 * doesn't flash a banner over a live session. A known-down runner with a
 * live host then splits on whether a turn is in flight: a just-sent turn
 * means the host is relaunching the runner *now* → `starting` (row 5);
 * otherwise it's idle `runner_asleep` (row 5'). Runner liveness is
 * poll-driven (the real-time `session.runner_status` push was removed
 * upstream).
 *
 * @param sessionId The open conversation's id, or undefined when none is
 *   open. Undefined yields `unknown`.
 * @param conv The open session's liveness row (carries `host_id`,
 *   `permission_level`, and `host_resumable`). Null/undefined while loading;
 *   the host-bound vs. local distinction, ownership, and the
 *   host_asleep-vs-host_offline split read from it.
 * @param opts.turnActive Whether a turn is currently in flight for the
 *   open session (the user just sent, or a cross-client turn is running).
 *   When the runner is down but the host is up, this upgrades the idle
 *   `runner_asleep` state to `starting` — the relaunch is happening now,
 *   so the user sees a "Connecting…" intermediate instead of a silent gap.
 * @returns The single active liveness variant.
 */
export function useSessionLiveness(
  sessionId: string | undefined,
  conv: LivenessRow | null | undefined,
  opts?: { turnActive?: boolean },
): SessionLiveness {
  const runnerOnline = useSessionRunnerOnline(sessionId);
  const hostOnline = useSessionHostOnline(sessionId);

  // Track whether this session's runner tunnel has *ever* been observed
  // registered. The startup grace below only stands in for the INITIAL
  // cold boot ("hasn't registered yet"); once a runner has come online, a
  // later `runner_online: false` is a genuine crash/disconnect, not cold
  // boot, so the grace must NOT re-mask it. Without this, killing the
  // runner of a freshly-created session shows no reconnect banner for the
  // whole grace window (regression caught by
  // tests/e2e_ui/test_stale_stream.py::test_stale_banner_on_runner_crash).
  // Per-mount ref keyed by session id (reset on navigation to another
  // session); converges as soon as the poll first reports `true`.
  const everOnlineRef = useRef<{ id: string | undefined; seen: boolean }>({
    id: sessionId,
    seen: false,
  });
  if (everOnlineRef.current.id !== sessionId) {
    everOnlineRef.current = { id: sessionId, seen: false };
  }
  if (runnerOnline === true) everOnlineRef.current.seen = true;
  const runnerEverOnline = everOnlineRef.current.seen;

  // 1. A live runner tunnel is the only thing that means "chat normally".
  if (runnerOnline === true) return { kind: "online" };

  const hostId = conv?.host_id ?? null;

  // 2. Startup grace: a freshly-created session whose runner tunnel hasn't
  // registered yet is cold-booting, not stranded. The runner-liveness poll
  // reads stale-`false`/`undefined` for up to one interval after creation
  // (see STARTING_GRACE_S), so surface `starting` — a passive "Connecting…"
  // intermediate with the composer open — instead of flashing a reconnect
  // banner. This precedes the host_offline check so a brand-new host-bound
  // session whose host hasn't registered yet doesn't flash "host offline"
  // either. `created_at` is Unix seconds; a missing/zero value (older
  // snapshot) yields a large delta and harmlessly skips the grace.
  const createdAt = conv?.created_at;
  if (
    !runnerEverOnline &&
    typeof createdAt === "number" &&
    createdAt > 0 &&
    Date.now() / 1000 - createdAt < STARTING_GRACE_S
  ) {
    return { kind: "starting" };
  }

  // 3. A host-bound session whose host is confirmed offline. If the host is
  // a resumable managed host, the server wakes the sandbox on the next
  // message (the send-message relaunch path calls resume_managed_host). A
  // just-sent turn (turnActive) is waking it *now* — surface the same
  // `starting` "Connecting…" intermediate as a fresh launch so the ~85s cold
  // wake isn't a blank screen; idle (no turn) stays `host_asleep` (composer
  // open, no banner). Either way it's NOT the host_offline dead-end.
  // Otherwise (non-resumable) it's genuinely stuck: `host_offline`.
  if (hostId && hostOnline === false) {
    if (conv?.host_resumable) {
      return opts?.turnActive ? { kind: "starting" } : { kind: "host_asleep" };
    }
    return { kind: "host_offline", isOwner: isOwner(conv) };
  }

  // 4. The runner has not been observed yet (pre-poll) — don't surface any
  // affordance over what may well be a live session.
  if (runnerOnline === undefined) return { kind: "unknown" };

  // Runner is known-offline from here down.

  // 5. Host is up: the host relaunches the runner on the next message. If
  // a turn is already in flight (the user just sent, or a cross-client
  // turn is running), that relaunch is happening *now* — surface the same
  // `starting` "Connecting…" intermediate as a fresh launch so there's no
  // silent gap. Otherwise the session is idle-asleep: the open view
  // renders nothing and the composer stays open, and the next send flips
  // it to `starting`.
  if (hostOnline === true) {
    return opts?.turnActive ? { kind: "starting" } : { kind: "runner_asleep" };
  }

  // 6. Host-bound but host liveness unseen (`host_id` set,
  // `host_online === undefined`): don't guess host-down — fall through to
  // `unknown` rather than asserting.
  if (hostId) return { kind: "unknown" };

  // 7. Not host-bound. `host_online` is `null` for these (no host to be
  // online); once the runner is known-down there's no host to relaunch
  // it, so the user restarts from their own machine.
  if (hostOnline === null || hostOnline === false) return { kind: "local_stranded" };
  // host_online undefined for a non-host-bound session shouldn't happen
  // (the stream sets it to null), but treat the unobserved case as
  // unknown rather than asserting.
  return { kind: "unknown" };
}
