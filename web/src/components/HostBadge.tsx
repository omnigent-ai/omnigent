import { useRef, useState } from "react";

import { useHosts } from "@/hooks/useHosts";
import type { Host } from "@/hooks/useHosts";
import { useSession } from "@/hooks/useSession";
import { useSessionHostOnline } from "@/hooks/RunnerHealthProvider";
import { sandboxOptionLabel } from "@/lib/capabilities";
import { SwitchHostDialog } from "@/shell/SwitchHostDialog";
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

const RECONNECT_WORD = "offline — click to reconnect";

/**
 * Host indicator for the open conversation, rendered in the composer's
 * status-line tray, immediately left of the worktree branch
 * (ComposerStatusLine). Reads its own data and renders nothing when the
 * session isn't host-bound — same self-contained shape as PresenceAvatars.
 * Shows the friendly host name (or sandbox-provider label) plus a status
 * circle: green online, red offline, neutral while liveness is still unknown.
 *
 * The name + dot is the ONLY shape this badge takes — a disconnected host
 * keeps its name so the user always reads which machine dropped. When the
 * host tunnel is down and reconnecting is possible, the same name + red dot
 * becomes a button that opens the reconnect instructions (`onReconnect`).
 * A dormant resumable managed host is excluded: its "offline" is idle
 * dormancy the next message wakes, not a disconnect to act on.
 *
 * Otherwise the badge is a button that opens `SwitchHostDialog` to move the
 * session to another machine. Reconnect keeps the click when it applies — it
 * has no other entry point — and offers the move inside its own dialog
 * instead. Server-managed sandbox hosts never offer it: the server owns
 * their placement.
 *
 * @param sessionId - The open conversation whose host to show.
 * @param onReconnect - Opens the reconnect help dialog. Wired by the caller
 *   for every host-bound session; the badge itself decides when a host is
 *   actually reconnectable.
 */
export function HostBadge({
  sessionId,
  onReconnect,
}: {
  sessionId: string;
  onReconnect?: () => void;
}) {
  const [switchOpen, setSwitchOpen] = useState(false);
  const { session } = useSession(sessionId);
  const hostId = session?.hostId ?? null;
  // Keep sandbox hosts so managed sessions resolve to a provider label.
  // Skip the fetch (and its 10s refetch loop) when there's no host to
  // resolve — the badge renders nothing in that case anyway.
  const { data: hosts } = useHosts({ includeSandbox: true, enabled: Boolean(hostId) });
  const liveOnline = useSessionHostOnline(sessionId);

  const host = hostId ? hosts?.find((h) => h.host_id === hostId) : undefined;

  // Host liveness is keyed by SESSION, not by host, and comes from a ~10s
  // poll. A host switch repoints `hostId` as soon as the session snapshot
  // refetches, but the map still holds the value polled against the host we
  // just left — so moving off a dead host paints a red dot beside a machine
  // that is demonstrably up until the next tick. Treat the value as stale
  // from the moment the binding changes until the poll reports something
  // different, which can only be an observation of the new host.
  const bindingRef = useRef<{
    hostId: string | null;
    at: boolean | null | undefined;
    stale: boolean;
  }>({ hostId, at: liveOnline, stale: false });
  if (bindingRef.current.hostId !== hostId) {
    bindingRef.current = { hostId, at: liveOnline, stale: true };
  } else if (bindingRef.current.stale && bindingRef.current.at !== liveOnline) {
    bindingRef.current = { hostId, at: liveOnline, stale: false };
  }
  const livenessPredatesBinding = bindingRef.current.stale;

  // Prefer the live health signal. Both `false` (host down) and `null`
  // (the stream's "not host-bound" signal) are meaningful answers, so we
  // only fall back to the host record's stored status when liveness has
  // not been observed yet (`undefined`) or still describes the previous
  // binding. Falling back on `null` would let a stale record's "online"
  // flash a green dot for a session the stream says is unbound — `null`
  // must reach resolveHostBadge as "unknown".
  const online =
    liveOnline === undefined || livenessPredatesBinding
      ? host
        ? host.status === "online"
        : undefined
      : liveOnline;

  const badge = resolveHostBadge({ hostId, host, online });
  if (!badge) return null;

  // A resumable managed host that reports offline is idle-stopped, not
  // disconnected — the next message resumes it, and `omnigent host` (what the
  // reconnect dialog hands out) is the wrong instruction for it.
  const reconnectable = badge.status === "offline" && !session?.hostResumable && !!onReconnect;
  const statusWord = reconnectable ? RECONNECT_WORD : STATUS_WORD[badge.status];
  // The dot is decorative (aria-hidden), so the status would otherwise be
  // conveyed by color alone. Restate it in sr-only text — read together with
  // the visible label, a screen reader announces "<host>, <status>". `title`
  // carries the same text for mouse hover.
  const content = (
    <>
      <span
        aria-hidden
        className={cn("size-2 shrink-0 rounded-full", STATUS_DOT_CLASS[badge.status])}
      />
      <span className="truncate">{badge.label}</span>
      <span className="sr-only">, {statusWord}</span>
    </>
  );
  const title = `Host ${badge.label}, ${statusWord}`;

  if (reconnectable) {
    return (
      <button
        type="button"
        data-testid="host-badge"
        onClick={onReconnect}
        className="flex min-w-0 items-center gap-1.5 text-sm text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
        title={title}
      >
        {content}
      </button>
    );
  }

  // Sandbox-backed hosts are provisioned (and relaunched) by the server, so a
  // manual move isn't meaningful. An unresolved record — a shared session, or
  // the list still loading — can't be confirmed non-sandbox, so it stays
  // passive too rather than offering a switch that may not apply.
  const canSwitch = host !== undefined && !host.sandbox_provider;
  if (!canSwitch) {
    return (
      // No aria-label: on a non-interactive div it's announced unreliably and
      // would only duplicate the sr-only text where it is honored.
      <div
        data-testid="host-badge"
        className="flex min-w-0 items-center gap-1.5 text-sm text-muted-foreground"
        title={title}
      >
        {content}
      </div>
    );
  }

  return (
    <>
      <button
        type="button"
        data-testid="host-badge"
        onClick={() => setSwitchOpen(true)}
        className="flex min-w-0 items-center gap-1.5 text-sm text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
        title={`${title} — click to switch`}
      >
        {content}
      </button>
      {/* Mounted only while open: the badge renders on every session, and a
          closed dialog would still run its host query and workspace hooks. */}
      {switchOpen && (
        <SwitchHostDialog
          open
          onOpenChange={setSwitchOpen}
          sessionId={sessionId}
          currentHostId={hostId}
        />
      )}
    </>
  );
}
