import { useEffect, useRef, useState } from "react";
import { CircleAlertIcon, Loader2Icon, MonitorCloudIcon } from "lucide-react";

import { useHosts } from "@/hooks/useHosts";
import type { Host } from "@/hooks/useHosts";
import { useSession } from "@/hooks/useSession";
import { useSessionActionRestrictions } from "@/hooks/useSessionActionRestrictions";
import { useSessionHostOnline } from "@/hooks/RunnerHealthProvider";
import { sandboxOptionLabel } from "@/lib/capabilities";
import { sandboxStageLabel } from "@/lib/sandboxStatus";
import type { SandboxStatus } from "@/lib/types";
import { DisabledActionTooltip } from "./DisabledActionTooltip";
import { SessionActionMenuItem } from "./SessionActionMenuItem";
import { SwitchHostDialog } from "@/shell/SwitchHostDialog";
import { cn } from "@/lib/utils";
import {
  ComposerConfigTooltipRows,
  ComposerHostTrigger,
  ComposerMenuTooltip,
} from "@/components/composer/ComposerControls";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

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

const CONNECTION_WORD: Record<HostBadgeStatus, string> = {
  online: "Connected",
  offline: "Disconnected",
  unknown: "Unknown",
};

/**
 * Session-bound host status with shared liveness, reconnect and switch behavior.
 * The composer variant uses the compact host menu; the default retains the
 * name-and-status badge. Managed sandbox placement remains server-owned.
 */
export function HostBadge({
  sessionId,
  onReconnect,
  appearance = "status",
  readOnly = false,
}: {
  sessionId: string;
  onReconnect?: () => void;
  appearance?: "status" | "composer";
  readOnly?: boolean;
}) {
  const [switchOpen, setSwitchOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const { session } = useSession(sessionId);
  const hostId = session?.hostId ?? null;
  // Keep sandbox hosts so managed sessions resolve to a provider label.
  // Launch progress supplies identity until there is a host record to resolve.
  const { data: hosts } = useHosts({ includeSandbox: true, enabled: Boolean(hostId) });
  const liveOnline = useSessionHostOnline(sessionId);

  const host = hostId ? hosts?.find((h) => h.host_id === hostId) : undefined;
  const activeLaunch = session?.sandboxStatus?.stage !== "ready" ? session?.sandboxStatus : null;
  // Ready precedes the binding and host-list refreshes. Keep launch identity
  // through that handoff, scoped to this session and its first bound host.
  // State keeps a discarded concurrent render from changing the visible session.
  const [launchIdentity, setLaunchIdentity] = useState<{
    sessionId: string;
    hostId: string | null;
    provider: string | null | undefined;
  } | null>(null);
  if (activeLaunch && activeLaunch.stage !== "failed") {
    const launchProvider = activeLaunch.provider ?? host?.sandbox_provider;
    if (
      !launchIdentity ||
      launchIdentity.sessionId !== sessionId ||
      launchIdentity.hostId !== hostId ||
      launchIdentity.provider !== launchProvider
    ) {
      setLaunchIdentity({ sessionId, hostId, provider: launchProvider });
    }
  } else if (launchIdentity) {
    if (
      activeLaunch?.stage === "failed" ||
      launchIdentity.sessionId !== sessionId ||
      host ||
      (launchIdentity.hostId !== null && launchIdentity.hostId !== hostId)
    ) {
      setLaunchIdentity(null);
    } else if (launchIdentity.hostId !== hostId) {
      setLaunchIdentity({ ...launchIdentity, hostId });
    }
  }

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
  const { switchHostDisabledReason } = useSessionActionRestrictions(sessionId);
  const canSwitch = host !== undefined && !switchHostDisabledReason && !readOnly;
  useEffect(() => {
    if (!canSwitch) setSwitchOpen(false);
  }, [canSwitch]);
  if (appearance === "composer") {
    const launch: SandboxStatus | null =
      activeLaunch ?? (!hostId && launchIdentity ? { stage: "connecting" } : null);
    const provider = launch?.provider ?? host?.sandbox_provider ?? launchIdentity?.provider;
    const sandboxLabel = provider ? sandboxOptionLabel(provider) : "Sandbox";
    const isSandbox = !!launch || !!host?.sandbox_provider || !!launchIdentity;
    const launchStatus = launch ? sandboxStageLabel(launch.stage) : "";
    const reconnectable =
      !launch && badge?.status === "offline" && !session?.hostResumable && !!onReconnect;
    // A dormant resumable host wakes on the next message; it isn't disconnected.
    const asleep = badge?.status === "offline" && !!session?.hostResumable;
    let hostLabel = "No host bound";
    let label = hostLabel;
    let tooltipRows = [{ label: "Host", value: "None" }];
    if (launch) {
      hostLabel = sandboxLabel;
      label = `Host ${hostLabel}, ${launchStatus}`;
      tooltipRows = [
        { label: "Host", value: hostLabel },
        { label: "Status", value: launchStatus },
        ...(launch.error ? [{ label: "Error", value: launch.error }] : []),
      ];
    } else if (badge) {
      hostLabel = !host && launchIdentity ? sandboxLabel : badge.label;
      label = `Host ${hostLabel}, ${STATUS_WORD[badge.status]}`;
      tooltipRows = [
        { label: "Host", value: hostLabel },
        {
          label: "Status",
          value: asleep ? "Asleep — wakes on next message" : CONNECTION_WORD[badge.status],
        },
      ];
    }
    return (
      <>
        <DropdownMenu onOpenChange={setMenuOpen}>
          <ComposerMenuTooltip
            menuOpen={menuOpen}
            content={<ComposerConfigTooltipRows rows={tooltipRows} />}
            testId="composer-host-tooltip"
            className="shrink-0"
          >
            <DropdownMenuTrigger asChild>
              <ComposerHostTrigger
                label={label}
                status={launch ? undefined : (badge?.status ?? "unknown")}
                cloud={isSandbox}
                data-testid="composer-host-select"
              />
            </DropdownMenuTrigger>
          </ComposerMenuTooltip>
          <DropdownMenuContent
            align="start"
            side="top"
            sideOffset={8}
            className="composer-host-menu w-max min-w-[220px]"
            data-testid="composer-host-menu"
          >
            <div className="px-2 py-1 text-xs text-muted-foreground">
              {isSandbox ? "Cloud" : "My machines"}
            </div>
            <DropdownMenuItem disabled className="gap-2" data-selected="true">
              {launch ? (
                <MonitorCloudIcon className="size-4 shrink-0" />
              ) : (
                <span
                  className={cn(
                    "size-2 shrink-0 rounded-full",
                    STATUS_DOT_CLASS[badge?.status ?? "unknown"],
                  )}
                />
              )}
              <span>{hostLabel}</span>
            </DropdownMenuItem>
            {launch && (
              <div className="max-w-72 whitespace-normal px-2 py-1 text-xs text-muted-foreground">
                <p
                  className={cn(
                    "flex items-center gap-2",
                    launch.stage === "failed" && "text-destructive",
                  )}
                >
                  {launch.stage === "failed" ? (
                    <CircleAlertIcon className="size-3 shrink-0" />
                  ) : (
                    <Loader2Icon className="size-3 shrink-0 animate-spin" />
                  )}
                  {launchStatus}
                </p>
                {launch.error && <p className="mt-1 break-words">{launch.error}</p>}
              </div>
            )}
            {reconnectable && (
              <DropdownMenuItem disabled={readOnly} onSelect={onReconnect}>
                Reconnect host
              </DropdownMenuItem>
            )}
            {!readOnly && (canSwitch || switchHostDisabledReason) && (
              <SessionActionMenuItem
                disabledReason={switchHostDisabledReason}
                onSelect={() => setSwitchOpen(true)}
              >
                Switch host…
              </SessionActionMenuItem>
            )}
            {!badge && !launch && (
              <p className="max-w-60 px-2 py-1 text-xs text-muted-foreground">
                This session has no host binding.
              </p>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
        {switchOpen && canSwitch && (
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
  if (!badge) return null;

  // A resumable managed host that reports offline is idle-stopped, not
  // disconnected — the next message resumes it, and `omnigent host` (what the
  // reconnect dialog hands out) is the wrong instruction for it.
  const reconnectable = badge.status === "offline" && !session?.hostResumable && !!onReconnect;
  const statusWord = reconnectable ? RECONNECT_WORD : STATUS_WORD[badge.status];
  const title = `Host ${badge.label}, ${statusWord}`;
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

  return (
    <>
      <DisabledActionTooltip
        reason={switchHostDisabledReason}
        label="Switch host"
        className="min-w-0"
      >
        {canSwitch ? (
          <button
            type="button"
            data-testid="host-badge"
            onClick={() => setSwitchOpen(true)}
            className="flex min-w-0 items-center gap-1.5 text-sm text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
            title={`${title} — click to switch`}
          >
            {content}
          </button>
        ) : (
          // Avoid a native title competing with the disabled-action tooltip.
          <div
            data-testid="host-badge"
            className="flex min-w-0 items-center gap-1.5 text-sm text-muted-foreground"
            title={switchHostDisabledReason ? undefined : title}
          >
            {content}
            {switchHostDisabledReason && (
              <button type="button" disabled className="pointer-events-none opacity-50">
                Switch host…
              </button>
            )}
          </div>
        )}
      </DisabledActionTooltip>
      {/* Mounted only while open: the badge renders on every session, and a
          closed dialog would still run its host query and workspace hooks. */}
      {switchOpen && canSwitch && (
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
