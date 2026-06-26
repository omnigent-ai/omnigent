import { useCallback, useEffect, useRef, useState } from "react";
import { PlayIcon, RotateCwIcon, SquareIcon } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  controlHost,
  controlServer,
  getHostStatus,
  getLocalServerStatus,
  isElectronShell,
  onHostStatusChanged,
  type HostControlAction,
  type HostStatus,
  type LocalServerStatus,
} from "@/lib/nativeBridge";
import { cn } from "@/lib/utils";

/** What the row should display, derived from live status + any in-flight action. */
interface Display {
  /** Dot color class. */
  tone: string;
  /** Short word shown on the row next to the dot (e.g. "connecting…"), or null. */
  hint: string | null;
  /** Fuller line shown atop the menu. */
  statusText: string;
  /** Whether the thing is up (drives which actions show). */
  active: boolean;
}

/** Host row display. A pending start/restart — or a live process that isn't yet
 *  tunneled — reads as "connecting…". */
function hostDisplay(status: HostStatus, pending: HostControlAction | null): Display {
  const stopping = pending === "stop";
  const connecting =
    pending === "start" ||
    pending === "restart" ||
    (status.process === "online" && !status.connected);

  let tone = "bg-muted-foreground/40";
  if (status.cliInstalled) {
    if (status.connected && !stopping) tone = "bg-success";
    else if (connecting || stopping) tone = "bg-warning";
  }

  let statusText: string;
  if (!status.cliInstalled) statusText = "Omnigent CLI not found";
  else if (stopping) statusText = "Stopping…";
  else if (connecting) statusText = "Connecting…";
  else if (status.connected) statusText = "Connected";
  else if (status.error) statusText = status.error;
  else statusText = "Not hosting";

  return {
    tone,
    hint: stopping ? "stopping…" : connecting ? "connecting…" : null,
    statusText,
    active: status.connected || status.process === "online",
  };
}

/** Local-server row display. */
function serverDisplay(server: LocalServerStatus, pending: HostControlAction | null): Display {
  const stopping = pending === "stop";
  const starting = pending === "start" || pending === "restart";

  let tone = server.running ? "bg-success" : "bg-muted-foreground/40";
  if (stopping || (starting && !server.running)) tone = "bg-warning";

  let statusText: string;
  if (stopping) statusText = "Stopping…";
  else if (pending === "restart") statusText = "Restarting…";
  else if (pending === "start") statusText = "Starting…";
  else if (server.running) statusText = "Running";
  else statusText = "Stopped";

  let hint: string | null = null;
  if (stopping) hint = "stopping…";
  else if (pending === "restart") hint = "restarting…";
  else if (pending === "start") hint = "starting…";

  return { tone, hint, statusText, active: server.running };
}

/**
 * A sidebar status row that opens a Start / Stop / Restart menu. The trigger
 * shows a title, an optional transient hint (e.g. "connecting…"), and a status
 * dot; the menu shows only the actions relevant to the current state.
 */
function StatusMenu({
  title,
  display,
  canControl,
  busy,
  onAction,
}: {
  title: string;
  display: Display;
  canControl: boolean;
  busy: boolean;
  onAction: (action: HostControlAction) => void;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        className={cn(
          "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm",
          "text-muted-foreground hover:bg-foreground/5 hover:text-foreground",
          "data-[state=open]:bg-foreground/5 data-[state=open]:text-foreground",
        )}
      >
        <span className="truncate">{title}</span>
        <span className="ml-auto flex items-center gap-1.5">
          {display.hint && <span className="text-xs">{display.hint}</span>}
          <span aria-hidden className={cn("size-2 shrink-0 rounded-full", display.tone)} />
        </span>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align="start" className="min-w-48">
        <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">
          {display.statusText}
        </DropdownMenuLabel>
        {/* Actions depend on state: only Start when off, only Stop/Restart when
            running. Nothing actionable when the CLI is missing. */}
        {canControl && (
          <>
            <DropdownMenuSeparator />
            {!display.active && (
              <DropdownMenuItem
                className="gap-2"
                disabled={busy}
                onSelect={() => onAction("start")}
              >
                <PlayIcon className="size-4 shrink-0" />
                Start
              </DropdownMenuItem>
            )}
            {display.active && (
              <>
                <DropdownMenuItem
                  className="gap-2"
                  disabled={busy}
                  onSelect={() => onAction("stop")}
                >
                  <SquareIcon className="size-4 shrink-0" />
                  Stop
                </DropdownMenuItem>
                <DropdownMenuItem
                  className="gap-2"
                  disabled={busy}
                  onSelect={() => onAction("restart")}
                >
                  <RotateCwIcon className="size-4 shrink-0" />
                  Restart
                </DropdownMenuItem>
              </>
            )}
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * Desktop-shell host/server controls in the sidebar footer next to Settings.
 *
 * A "Host Status" row (this machine's host daemon) and — when connected to a
 * local server the CLI manages — a "Local Server Status" row. Each shows a
 * status dot, a transient hint (e.g. "connecting…") while the tunnel comes up
 * or an action runs, and a Start / Stop / Restart menu driving the omnigent CLI
 * through the shell. Status is read live (`getHostStatus` + the shell's pushed
 * updates); hosting can also be opted into at connect time on the setup page.
 *
 * Renders nothing outside the Electron shell or before the shell reports a
 * status. Desktop-only by layout (hidden on the narrow mobile sidebar).
 */
export function HostStatusIndicator() {
  const [host, setHost] = useState<HostStatus | null>(null);
  const [server, setServer] = useState<LocalServerStatus | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [pendingHost, setPendingHost] = useState<HostControlAction | null>(null);
  const [pendingServer, setPendingServer] = useState<HostControlAction | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const refresh = useCallback(() => {
    void getHostStatus().then((s) => {
      if (!mounted.current) return;
      setHost(s);
      setLoaded(true);
    });
    void getLocalServerStatus().then((s) => {
      if (mounted.current) setServer(s);
    });
  }, []);

  useEffect(() => {
    if (!isElectronShell()) return;
    refresh();
    return onHostStatusChanged(() => refresh());
  }, [refresh]);

  // Only ever shown inside the desktop shell — never in a plain browser tab.
  if (!isElectronShell()) return null;
  // Once we've loaded and the page isn't a connected server (no status), hide.
  if (loaded && !host) return null;

  // Render immediately (don't wait for the first status query, which shells out
  // to a Python CLI): a neutral "checking…" row until the status arrives.
  const hostDisp: Display = host
    ? hostDisplay(host, pendingHost)
    : { tone: "bg-muted-foreground/40", hint: "checking…", statusText: "Checking…", active: false };

  const runHost = async (action: HostControlAction) => {
    setPendingHost(action);
    try {
      await controlHost(action);
    } finally {
      refresh();
      if (mounted.current) setPendingHost(null);
    }
  };
  const runServer = async (action: HostControlAction) => {
    setPendingServer(action);
    try {
      await controlServer(action);
    } finally {
      refresh();
      if (mounted.current) setPendingServer(null);
    }
  };

  return (
    <div className="max-md:hidden">
      <StatusMenu
        title="Host Status"
        display={hostDisp}
        canControl={host ? host.cliInstalled : false}
        busy={pendingHost !== null}
        onAction={(action) => void runHost(action)}
      />
      {server && (
        <StatusMenu
          title="Local Server Status"
          display={serverDisplay(server, pendingServer)}
          canControl
          busy={pendingServer !== null}
          onAction={(action) => void runServer(action)}
        />
      )}
    </div>
  );
}
