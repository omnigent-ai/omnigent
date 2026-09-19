import { useEffect, useState } from "react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { quoteShellArgument } from "@/lib/shell";
import {
  controlHost,
  getHostIdentity,
  isElectronShell,
  type HostIdentity,
} from "@/lib/nativeBridge";
import { CliCommandBlock } from "./CliCommandBlock";
import { ForkSessionForm } from "./ForkSessionDialog";
import { SwitchHostDialog } from "./SwitchHostDialog";

import { nativeCodingAgentForHarness, nativeCodingAgentForWrapper } from "@/lib/nativeCodingAgents";

const HOST_OWNER_DESCRIPTION =
  "This session's host is offline. Run the command below from the host machine to reconnect.";

const HOST_OWNER_THIS_MACHINE_DESCRIPTION =
  "This session's host is this machine. Reconnect it below, or run the command from a terminal.";

const HOST_VIEWER_DESCRIPTION =
  "This session's host machine is offline and only its owner can reconnect it. " +
  "Clone the session to continue in a copy you own.";

const RUN_DESCRIPTION =
  "Run the command below from the machine where you started this session to reconnect.";

/**
 * The liveness state this dialog reconnects from. Maps to the
 * {@link SessionLiveness} variants that leave the session unreachable:
 *
 * - `host_offline` — the session is host-bound and the host tunnel is
 *   down. The owner reconnects the host (`omnigent host`); a
 *   non-owner can't reach that machine, so cloning is their only path.
 * - `local_stranded` — not host-bound and the runner is down. Whoever
 *   started it relaunches from their machine via the wrapper's resume
 *   form.
 */
export type ReconnectState = "host_offline" | "local_stranded";

/**
 * Build the CLI command the user pastes to bring an unreachable session
 * back. Two forms, picked by `state`:
 *
 * 1. `host_offline` — `omnigent host` re-registers the host
 *    machine; the server relaunches the session's runner on demand once
 *    it's back. No `--resume` and no agent YAML (the host launches
 *    whatever the session was bound to), regardless of wrapper.
 * 2. `local_stranded` — the session isn't host-bound, so the user
 *    relaunches a runner directly. claude-native sessions
 *    (`wrapper === "claude-code-native-ui"`) use `omnigent claude
 *    --resume <id>`; everything else uses the generic `omnigent run
 *    path/to/agent.yaml --resume <id>`.
 *
 * The native wrapper is resolved from the `omnigent.wrapper` label, then
 * the canonical `harness` — a pre-native session (e.g. a legacy `devin-acp`
 * row) can carry no wrapper label yet still be a native harness, and the
 * generic `omnigent run` form cannot resume it.
 *
 * The Databricks profile stays a placeholder in every form — it's
 * per-deployment and not knowable from the browser.
 */
export function buildReconnectCommand({
  conversationId,
  serverUrl,
  wrapper,
  harness,
  state,
}: {
  conversationId: string;
  serverUrl: string;
  wrapper?: string | null;
  harness?: string | null;
  state: ReconnectState;
}): string {
  // Backslash-continued so the command stays readable inside a narrow
  // dialog AND remains valid when pasted into a shell.
  const quotedServerUrl = quoteShellArgument(serverUrl);
  if (state === "host_offline") {
    return ["omnigent host \\", `  --server ${quotedServerUrl}`].join("\n");
  }
  // Every native TUI wrapper resumes through its own verb (`omnigent devin
  // --resume …`), and the verb is the registry key — the generic
  // `omnigent run <agent.yaml>` below cannot resume one at all, so it was wrong
  // for every native harness except claude. Fall back to the canonical harness
  // when there's no wrapper label (a label-less pre-native session).
  const nativeAgent = nativeCodingAgentForWrapper(wrapper) ?? nativeCodingAgentForHarness(harness);
  if (nativeAgent !== undefined) {
    return [
      `omnigent ${nativeAgent.key} \\`,
      `  --resume ${conversationId} \\`,
      `  --server ${quotedServerUrl}`,
    ].join("\n");
  }
  return [
    "omnigent run path/to/agent.yaml \\",
    `  --resume ${conversationId} \\`,
    `  --server ${quotedServerUrl}`,
  ].join("\n");
}

/**
 * Dialog surfaced when the open session is unreachable — the host is
 * offline (`host_offline`) or it isn't host-bound and the runner is down
 * (`local_stranded`). It is NOT shown when the runner is merely asleep
 * but the host is up: there the composer stays open and typing silently
 * relaunches the runner (see `useSessionLiveness`).
 *
 * Two tabs:
 * - **Reconnect** — a one-line instruction plus the CLI command. For a
 *   non-owner of a `host_offline` session — who can't reach the host
 *   machine — the command is dropped and the text explains that only
 *   the owner can reconnect. Under the desktop shell, when the offline
 *   host IS this machine, a one-click button performs the reconnect
 *   in-app via the bridge's `controlHost("start")` — the same call
 *   NewChatDialog's "Run on this machine" drives — instead of sending
 *   the user to a terminal.
 * - **Clone** — the same {@link ForkSessionForm} the header-menu Clone
 *   dialog uses (one fork implementation, two entry points), so the
 *   user can continue in a copy they own without leaving the dialog.
 *
 * The default tab is Reconnect, except for the non-owner `host_offline`
 * case where reconnecting is impossible and Clone is the only action.
 *
 * @param wrapper - The conversation's `omnigent.wrapper` label
 *   (`"claude-code-native-ui"` for `omnigent claude` sessions). Picks
 *   the `local_stranded` command form.
 * @param harness - The conversation's canonical harness, used to pick the
 *   `local_stranded` command form when no wrapper label is present.
 * @param state - Which unreachable state we're reconnecting from.
 * @param isOwner - Whether the viewer owns the session. Gates the
 *   reconnect command for `host_offline`.
 * @param sourceTitle - Source title for the Clone tab's name prefill.
 * @param sourceWorkspace - Source workspace; marks a coding source for
 *   the Clone tab (host/directory pickers).
 * @param sourceHostId - Source host for the Clone tab's host prefill.
 * @param sourceGitBranch - Source git branch for the Clone tab's
 *   worktree base-ref prefill.
 */
export function ReconnectSessionDialog({
  open,
  onOpenChange,
  conversationId,
  serverUrl,
  wrapper,
  harness,
  state,
  isOwner,
  sourceTitle,
  sourceWorkspace,
  sourceHostId,
  sourceGitBranch,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  conversationId: string;
  serverUrl: string;
  wrapper?: string | null;
  harness?: string | null;
  state: ReconnectState;
  isOwner: boolean;
  sourceTitle?: string | null;
  sourceWorkspace?: string | null;
  sourceHostId?: string | null;
  sourceGitBranch?: string | null;
}) {
  const [switchOpen, setSwitchOpen] = useState(false);
  const [desktopHost, setDesktopHost] = useState<HostIdentity | null>(null);
  const [reconnecting, setReconnecting] = useState(false);
  const [reconnectError, setReconnectError] = useState<string | null>(null);
  const isHostReconnect = state === "host_offline";

  // Only the desktop shell can act on this machine; identify it while the
  // dialog is open so the one-click path renders only when the offline host
  // IS this machine.
  useEffect(() => {
    if (!open || !isHostReconnect || !isOwner || !isElectronShell()) return;
    let cancelled = false;
    setReconnectError(null);
    void getHostIdentity().then((identity) => {
      if (!cancelled) setDesktopHost(identity);
    });
    return () => {
      cancelled = true;
    };
  }, [open, isHostReconnect, isOwner]);

  const canReconnectThisMachine =
    isHostReconnect &&
    isOwner &&
    sourceHostId != null &&
    desktopHost != null &&
    desktopHost.cliInstalled &&
    desktopHost.hostId === sourceHostId;

  async function reconnectThisMachine() {
    if (reconnecting) return;
    setReconnecting(true);
    setReconnectError(null);
    try {
      // A single controlHost("start") blocks through enrollment → sign-in →
      // connect, so on success the host is already back; the session's
      // liveness poll picks it up once the dialog is out of the way.
      const res = await controlHost("start");
      if (!res.ok) {
        setReconnectError(
          res.authError
            ? (res.error ??
                "Sign-in didn't complete. A browser should have opened — finish signing in, then try again.")
            : (res.error ?? "Couldn't reconnect this machine."),
        );
        return;
      }
      onOpenChange(false);
    } finally {
      setReconnecting(false);
    }
  }
  // A non-owner can't reach the host machine to reconnect it, so the
  // CLI command is useless to them. Owners of both states, and anyone
  // on a local_stranded session, get a command.
  const showCommand = isOwner || !isHostReconnect;
  const command = buildReconnectCommand({ conversationId, serverUrl, wrapper, harness, state });
  // Titles mirror the unreachable banner's wording ("Host is offline —
  // click to reconnect" / "Agent disconnected — click to reconnect").
  const title = isHostReconnect ? "Host is offline" : "Agent disconnected";
  const description = isHostReconnect
    ? isOwner
      ? canReconnectThisMachine
        ? HOST_OWNER_THIS_MACHINE_DESCRIPTION
        : HOST_OWNER_DESCRIPTION
      : HOST_VIEWER_DESCRIPTION
    : RUN_DESCRIPTION;
  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent
          data-testid="reconnect-session-dialog"
          className="flex max-h-[85vh] flex-col gap-4 sm:max-w-lg"
        >
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            {/* The visible per-tab text lives inside the tab panels; this
              keeps the dialog described for screen readers. */}
            <DialogDescription className="sr-only">{description}</DialogDescription>
          </DialogHeader>
          {/* Uncontrolled tabs: DialogContent unmounts on close, so the
            default re-applies on every open. */}
          <Tabs
            defaultValue={showCommand ? "reconnect" : "clone"}
            className="flex min-h-0 flex-1 flex-col gap-4"
            componentId="reconnect.tabs"
          >
            <TabsList className="w-full">
              <TabsTrigger value="reconnect" data-testid="reconnect-session-tab-reconnect">
                Reconnect
              </TabsTrigger>
              <TabsTrigger value="clone" data-testid="reconnect-session-tab-clone">
                Clone
              </TabsTrigger>
            </TabsList>
            <TabsContent value="reconnect" className="flex flex-col gap-4">
              <p
                className="text-ui text-muted-foreground"
                data-testid="reconnect-session-description"
              >
                {description}
              </p>
              {canReconnectThisMachine && (
                <div className="flex flex-col gap-2">
                  <Button
                    className="self-start"
                    data-testid="reconnect-session-this-machine"
                    disabled={reconnecting}
                    onClick={() => void reconnectThisMachine()}
                  >
                    {reconnecting ? "Reconnecting this machine…" : "Reconnect this machine"}
                  </Button>
                  {reconnectError && (
                    <p
                      className="text-sm text-destructive select-text"
                      data-testid="reconnect-session-reconnect-error"
                    >
                      {reconnectError}
                    </p>
                  )}
                </div>
              )}
              {showCommand && (
                <CliCommandBlock command={command} testIdPrefix="reconnect-session" />
              )}
              {/* Waiting on a machine that may not come back is a dead end, so
                offer the move as the way out. Owners only — binding a runner
                elsewhere is not something a viewer can do. */}
              {isHostReconnect && isOwner && (
                <div className="flex flex-col gap-2 border-t pt-4">
                  <p className="text-ui text-muted-foreground">
                    Can't bring that machine back? Move the session to another one instead.
                  </p>
                  <Button
                    variant="outline"
                    className="self-start"
                    data-testid="reconnect-session-switch-host"
                    onClick={() => {
                      setSwitchOpen(true);
                      onOpenChange(false);
                    }}
                  >
                    Switch host
                  </Button>
                </div>
              )}
            </TabsContent>
            {/* forceMount keeps the fork form's state (notably the
              created-fork ref after a failed launch) across tab switches —
              losing it would re-fork on retry. The explicit hidden class is
              required: `flex` would otherwise override the native [hidden]
              display:none that Radix puts on the inactive panel. */}
            <TabsContent
              value="clone"
              forceMount
              className="flex min-h-0 flex-1 flex-col gap-4 data-[state=inactive]:hidden"
            >
              <ForkSessionForm
                sourceSessionId={conversationId}
                sourceTitle={sourceTitle}
                sourceWorkspace={sourceWorkspace}
                sourceHostId={sourceHostId}
                sourceGitBranch={sourceGitBranch}
                onClose={() => onOpenChange(false)}
              />
            </TabsContent>
          </Tabs>
        </DialogContent>
      </Dialog>
      {/* Sibling of the dialog above, not a child: the switch opens as the
          reconnect dialog closes, and a child would unmount with it. */}
      {switchOpen && (
        <SwitchHostDialog
          open
          onOpenChange={setSwitchOpen}
          sessionId={conversationId}
          currentHostId={sourceHostId ?? null}
        />
      )}
    </>
  );
}
