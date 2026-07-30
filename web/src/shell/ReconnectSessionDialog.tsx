import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { CliCommandBlock } from "./CliCommandBlock";
import { ForkSessionForm } from "./ForkSessionDialog";

const CLAUDE_NATIVE_WRAPPER = "claude-code-native-ui";

const HOST_OWNER_DESCRIPTION =
  "This session's host is offline. Run the command below from the host machine to reconnect.";

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
 * The Databricks profile stays a placeholder in every form — it's
 * per-deployment and not knowable from the browser.
 */
export function buildReconnectCommand({
  conversationId,
  serverUrl,
  wrapper,
  state,
}: {
  conversationId: string;
  serverUrl: string;
  wrapper?: string | null;
  state: ReconnectState;
}): string {
  // Backslash-continued so the command stays readable inside a narrow
  // dialog AND remains valid when pasted into a shell.
  if (state === "host_offline") {
    return ["omnigent host \\", `  --server ${serverUrl}`].join("\n");
  }
  if (wrapper === CLAUDE_NATIVE_WRAPPER) {
    return [
      "omnigent claude \\",
      `  --resume ${conversationId} \\`,
      `  --server ${serverUrl}`,
    ].join("\n");
  }
  return [
    "omnigent run path/to/agent.yaml \\",
    `  --resume ${conversationId} \\`,
    `  --server ${serverUrl}`,
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
 *   the owner can reconnect.
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
  state: ReconnectState;
  isOwner: boolean;
  sourceTitle?: string | null;
  sourceWorkspace?: string | null;
  sourceHostId?: string | null;
  sourceGitBranch?: string | null;
}) {
  const isHostReconnect = state === "host_offline";
  // A non-owner can't reach the host machine to reconnect it, so the
  // CLI command is useless to them. Owners of both states, and anyone
  // on a local_stranded session, get a command.
  const showCommand = isOwner || !isHostReconnect;
  const command = buildReconnectCommand({ conversationId, serverUrl, wrapper, state });
  // Titles mirror the unreachable banner's wording ("Host is offline —
  // click to reconnect" / "Agent disconnected — click to reconnect").
  const title = isHostReconnect ? "Host is offline" : "Agent disconnected";
  const description = isHostReconnect
    ? isOwner
      ? HOST_OWNER_DESCRIPTION
      : HOST_VIEWER_DESCRIPTION
    : RUN_DESCRIPTION;
  return (
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
              className="text-sm text-muted-foreground"
              data-testid="reconnect-session-description"
            >
              {description}
            </p>
            {showCommand && <CliCommandBlock command={command} testIdPrefix="reconnect-session" />}
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
  );
}
