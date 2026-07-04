import { useHosts } from "@/hooks/useHosts";
import type { Host } from "@/hooks/useHosts";
import { useSession } from "@/hooks/useSession";
import { useSessionHostOnline } from "@/hooks/RunnerHealthProvider";
import { sandboxOptionLabel } from "@/lib/capabilities";
import { cn } from "@/lib/utils";

export type HostBadgeStatus = "online" | "offline" | "unknown";

export interface HostBadgeInfo {
  label: string;
  status: HostBadgeStatus;
}

/**
 * Compute the host badge's label + status from a session's host binding.
 *
 * - Not host-bound (`hostId` null/absent) → `null` (render nothing).
 * - Sandbox-backed host → the provider label ("Databricks Sandbox").
 * - Connected host → its friendly `name`.
 * - Host-bound but record unresolved (shared session / not yet loaded)
 *   → the raw `hostId`, so the badge always answers "which host".
 *
 * `online` is tri-stated: `true`/`false` map to online/offline; `null`
 * (not-host-bound signal) and `undefined` (not yet observed) both map to
 * "unknown" so the circle never flashes red before liveness settles.
 */
export function resolveHostBadge(args: {
  hostId: string | null | undefined;
  host: Host | undefined;
  online: boolean | null | undefined;
}): HostBadgeInfo | null {
  const { hostId, host, online } = args;
  if (!hostId) return null;
  const label = host
    ? host.sandbox_provider
      ? sandboxOptionLabel(host.sandbox_provider)
      : host.name
    : hostId;
  const status: HostBadgeStatus =
    online === true ? "online" : online === false ? "offline" : "unknown";
  return { label, status };
}

const STATUS_DOT_CLASS: Record<HostBadgeStatus, string> = {
  online: "bg-success",
  offline: "bg-destructive",
  // Neutral while liveness is still settling — avoids a red flash.
  unknown: "bg-muted-foreground/50",
};

const STATUS_WORD: Record<HostBadgeStatus, string> = {
  online: "online",
  offline: "offline",
  unknown: "status unknown",
};

/**
 * Host indicator for the open conversation, rendered in the composer's
 * status-line tray, immediately left of the worktree branch
 * (ComposerStatusLine). Reads its own data and renders nothing when the
 * session isn't host-bound — same self-contained shape as PresenceAvatars.
 * Shows the friendly host name (or sandbox-provider label) plus a status
 * circle: green online, red offline, neutral while liveness is still unknown.
 */
export function HostBadge({ sessionId }: { sessionId: string }) {
  const { session } = useSession(sessionId);
  const hostId = session?.hostId ?? null;
  // Keep sandbox hosts so managed sessions resolve to a provider label.
  // Skip the fetch (and its 10s refetch loop) when there's no host to
  // resolve — the badge renders nothing in that case anyway.
  const { data: hosts } = useHosts({ includeSandbox: true, enabled: Boolean(hostId) });
  const liveOnline = useSessionHostOnline(sessionId);

  const host = hostId ? hosts?.find((h) => h.host_id === hostId) : undefined;
  // Prefer the live health signal. Both `false` (host down) and `null`
  // (the stream's "not host-bound" signal) are meaningful answers, so we
  // only fall back to the host record's stored status when liveness has
  // not been observed yet (`undefined`). Falling back on `null` would let
  // a stale record's "online" flash a green dot for a session the stream
  // says is unbound — `null` must reach resolveHostBadge as "unknown".
  const online =
    liveOnline === undefined ? (host ? host.status === "online" : undefined) : liveOnline;

  const badge = resolveHostBadge({ hostId, host, online });
  if (!badge) return null;

  return (
    <div
      data-testid="host-badge"
      className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground"
      title={`Host ${badge.label}, ${STATUS_WORD[badge.status]}`}
    >
      <span
        aria-hidden
        className={cn("size-2 shrink-0 rounded-full", STATUS_DOT_CLASS[badge.status])}
      />
      <span className="truncate">{badge.label}</span>
      {/* The dot is decorative (aria-hidden), so the status would otherwise be
          conveyed by color alone. Restate it in sr-only text — read together
          with the visible label, a screen reader announces "<host>, <status>".
          `title` carries the same text for mouse hover. No aria-label: on a
          non-interactive div it's announced unreliably and would only
          duplicate this text where it is honored. */}
      <span className="sr-only">, {STATUS_WORD[badge.status]}</span>
    </div>
  );
}
