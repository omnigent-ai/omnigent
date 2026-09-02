import { AlertTriangleIcon, Loader2Icon, WifiOffIcon } from "lucide-react";
import { ConversationEmptyState } from "@/components/ai-elements/conversation";
import { Message, MessageContent } from "@/components/ai-elements/message";
import { ErrorBanner } from "@/components/blocks/StatusBlocks";
import type { SessionLiveness } from "@/hooks/useSessionLiveness";
import type { SandboxStatus } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useTerminalFirst } from "@/shell/TerminalFirstContext";
import { useChatStore } from "@/store/chatStore";
import { CHAT_COLUMN_WIDTH } from "./chatLayout";

/**
 * Band copy for each in-flight managed-sandbox launch stage, in
 * pipeline order: provisioning → cloning (repo workspaces only) →
 * starting → connecting. `starting` is the in-sandbox host booting
 * and dialing back to the server (so it reads "Connecting host");
 * `connecting` is the agent runner being launched on that host
 * (so it reads "Starting agent"). Terminal stages are absent on
 * purpose — `ready` clears the band and `failed` renders its own
 * error band.
 */
const SANDBOX_STAGE_LABELS: Record<string, string | undefined> = {
  provisioning: "Provisioning sandbox",
  cloning: "Cloning repository",
  starting: "Connecting host",
  connecting: "Starting agent",
};

/**
 * Failure band for a managed-sandbox session whose background launch
 * died. Renders the recorded reason so a dead launch explains itself
 * instead of presenting a silent dead chat. In-flight launch progress
 * does NOT render here — it shares the in-thread
 * :func:`RunnerStartingIndicator` spot so all launch states live on
 * one consistent line.
 */
export function SandboxFailedIndicator({ status }: { status: SandboxStatus }) {
  return (
    <div
      data-testid="sandbox-failed-indicator"
      role="status"
      className={cn("mx-auto w-full", CHAT_COLUMN_WIDTH)}
    >
      <ErrorBanner message={status.error ?? ""} source="" code="" title="Sandbox launch failed" />
    </div>
  );
}

export function ConnectionIndicator({
  liveness,
  onShowReconnectHelp,
}: {
  liveness: SessionLiveness;
  onShowReconnectHelp: () => void;
}) {
  const terminalFirst = useTerminalFirst();
  const sandboxStatus = useChatStore((s) => s.sandboxStatus);
  // Genuinely-unreachable states get the reconnect banner, for
  // both terminal-first and regular sessions. `runner_asleep` (host up,
  // runner relaunches on the next message), `host_asleep` (resumable managed
  // host the server wakes on the next message), and `unknown` (pre-poll) are
  // NOT unreachable — they're handled below.
  const unreachable = liveness.kind === "host_offline" || liveness.kind === "local_stranded";

  if (sandboxStatus !== null) {
    // A failed launch owns this band with its reason. An IN-FLIGHT
    // launch renders in the chat thread (RunnerStartingIndicator)
    // instead — but still suppresses the liveness bands below, which
    // would misread the not-yet-bound session as stranded.
    if (sandboxStatus.stage === "failed") {
      return <SandboxFailedIndicator status={sandboxStatus} />;
    }
    return null;
  }
  if (unreachable) {
    // A host-bound session carries the reconnect affordance in the composer's
    // host badge (ComposerStatusLine), which names the host that dropped — so
    // render nothing here whenever that composer is on screen (sub-agent
    // sessions included; their badge carries it just like a normal session's).
    // The composer is hidden only in the terminal-first *terminal* view (the
    // PTY owns the surface); there the banner still carries the affordance.
    // `local_stranded` keeps the banner everywhere (no host, hence no badge).
    const composerOnScreen = !(terminalFirst?.isTerminalFirst && terminalFirst.view === "terminal");
    if (liveness.kind === "host_offline" && composerOnScreen) {
      return null;
    }
    return (
      <div className={cn("mx-auto mb-4 flex w-full justify-center px-6", CHAT_COLUMN_WIDTH)}>
        {/* Reconnect affordance styled as the destructive error pill (never
              raw red text). Keeps its own click → reconnect dialog rather than
              the ErrorBanner's async Retry, since some states need the picker. */}
        <button
          type="button"
          data-testid="disconnected-indicator"
          onClick={onShowReconnectHelp}
          className="flex items-center gap-2 rounded-[12px] px-4 py-2 text-sm text-destructive transition-[filter] hover:brightness-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
          style={{
            background:
              "color-mix(in srgb, var(--destructive) 4%, var(--app-shell-bg, var(--background)))",
            border: "1px solid color-mix(in srgb, var(--destructive) 32%, transparent)",
          }}
        >
          <WifiOffIcon className="size-3.5 shrink-0" />
          <span>
            {liveness.kind === "host_offline"
              ? "Host is offline — click to reconnect"
              : "Agent disconnected — click to reconnect"}
          </span>
        </button>
      </div>
    );
  }

  // Terminal-first sessions: the Chat/Terminal switcher lives in the header
  // (ViewModeToggle) on every shell, iOS included — this band renders
  // nothing for them outside the unreachable states above.
  if (terminalFirst?.isTerminalFirst) {
    return null;
  }

  // A regular (non-terminal-first) session whose runner is still spinning
  // up shows a passive "Connecting…" row — no action, no banner, just a
  // heartbeat so the empty chat doesn't read as broken.
  if (liveness.kind === "starting") {
    return (
      <div
        data-testid="connecting-indicator"
        className={cn(
          "mx-auto mb-4 flex w-full items-center justify-center gap-2 px-6 py-1.5 text-muted-foreground text-sm",
          CHAT_COLUMN_WIDTH,
        )}
      >
        <Loader2Icon className="size-3.5 shrink-0 animate-spin" aria-hidden />
        <span>Connecting…</span>
      </div>
    );
  }

  // `online`/`unknown` for a non-terminal-first session and
  // `runner_asleep`/`host_asleep` for any session: status lives in the
  // sidebar / the composer stays open, so render nothing here.
  return null;
}

/**
 * Main-pane launch indicator — the single in-thread line for every
 * "session is coming up" state. Two launch shapes feed it, in
 * priority order:
 *
 * 1. A managed-sandbox launch (`sandboxStatus` in flight): shows the
 *    current pipeline stage ("Provisioning sandbox…", "Cloning
 *    repository…", …) for ANY session type.
 * 2. A terminal-first runner spin-up (`terminalStartingUp`): shows the
 *    generic "Starting up…" terminal copy. The sandbox stages win
 *    while both are active — they're strictly more specific.
 *
 * Self-gates to null when neither applies. `hero` is the centered
 * empty-state placeholder (no bubbles yet); `row` is the in-thread
 * spinner beneath the user's first message (the create-then-send path
 * renders that bubble immediately, so the empty state never shows
 * there).
 */
export function RunnerStartingIndicator({ variant }: { variant: "hero" | "row" }) {
  const terminalFirst = useTerminalFirst();
  const sandboxStatus = useChatStore((s) => s.sandboxStatus);
  // `ready` never reaches the store (cleared) and `failed` renders the
  // destructive band in ConnectionIndicator — only in-flight stages
  // with known copy show here.
  const sandboxLabel =
    sandboxStatus !== null && sandboxStatus.stage !== "failed"
      ? SANDBOX_STAGE_LABELS[sandboxStatus.stage]
      : undefined;
  // `terminalStartingUp` is computed for ALL sessions in AppShell (it does not
  // check isTerminalFirst), so gate on isTerminalFirst too: regular agents
  // (e.g. polly) get the generic ConnectionIndicator "Connecting…" band and
  // must not also render this.
  const terminalSpinUp = Boolean(
    terminalFirst?.isTerminalFirst && terminalFirst.terminalStartingUp,
  );
  if (sandboxLabel === undefined && !terminalSpinUp) {
    return null;
  }
  const line = sandboxLabel !== undefined ? `${sandboxLabel}…` : "Starting up…";
  // role=status + aria-live so assistive tech announces the transient wait;
  // the spinner glyph itself is decorative (aria-hidden).
  if (variant === "hero") {
    return (
      <ConversationEmptyState
        data-testid="runner-starting-indicator"
        role="status"
        aria-live="polite"
        icon={<Loader2Icon className="size-7 animate-spin" aria-hidden />}
        title={sandboxLabel !== undefined ? `${sandboxLabel}…` : "Starting up…"}
        description={
          sandboxLabel !== undefined
            ? "Setting up your sandbox — this can take a minute."
            : "This can take a few seconds."
        }
      />
    );
  }
  return (
    <Message
      from="assistant"
      data-testid="runner-starting-indicator"
      role="status"
      aria-live="polite"
    >
      <MessageContent>
        <span className="flex items-center gap-2 text-muted-foreground text-ui">
          <Loader2Icon className="size-4 shrink-0 animate-spin" aria-hidden />
          {line}
        </span>
      </MessageContent>
    </Message>
  );
}

// How many still-starting server names the startup band spells out
// before collapsing the rest into "…" — mirrors the Codex TUI's own
// startup header, and keeps a 20-server config to one line.
const MCP_STARTING_NAMES_SHOWN = 3;
// Cap for the settled warning's failed/cancelled name lists. Longer
// than the starting cap because these name servers the user may need
// to fix; beyond this the count carries the signal.
const MCP_SETTLED_NAMES_SHOWN = 8;

/**
 * The startup band's in-flight line, mirroring the Codex TUI's header.
 *
 * @param starting Still-starting server names, sorted.
 * @param total Total servers in the round.
 * @returns e.g. `"Starting MCP servers (1/20): glean, jira, safe, …"`.
 */
export function mcpStartingLine(starting: string[], total: number): string {
  if (total === 1 && starting.length === 1) {
    return `Starting MCP server: ${starting[0]}…`;
  }
  const shown = starting.slice(0, MCP_STARTING_NAMES_SHOWN);
  if (starting.length > MCP_STARTING_NAMES_SHOWN) shown.push("…");
  return `Starting MCP servers (${total - starting.length}/${total}): ${shown.join(", ")}`;
}

/**
 * A settled warning's name list, capped so the band stays scannable.
 *
 * @param names Failed or cancelled server names, sorted.
 * @returns e.g. `"a, b, c, d, e, f, g, h, +12 more"`.
 */
export function mcpSettledNames(names: string[]): string {
  if (names.length <= MCP_SETTLED_NAMES_SHOWN) return names.join(", ");
  const shown = names.slice(0, MCP_SETTLED_NAMES_SHOWN);
  return `${shown.join(", ")}, +${names.length - MCP_SETTLED_NAMES_SHOWN} more`;
}

/**
 * Per-MCP-server startup band for native harness sessions (codex-native).
 * Codex defers a mid-startup turn's execution until its MCP servers
 * settle, and the session previously showed nothing during that window.
 * Renders a spinner naming the still-starting servers; once startup
 * settles with failures/cancellations, a one-line notice says which
 * servers never came up. Self-gates to null when the store carries no
 * startup state (an all-ready map is cleared by the store handler).
 */
export function McpStartupIndicator() {
  const mcpStartup = useChatStore((s) => s.mcpStartup);
  if (mcpStartup === null) return null;
  const names = Object.keys(mcpStartup).sort();
  const starting = names.filter((name) => mcpStartup[name].status === "starting");
  if (starting.length > 0) {
    return (
      <Message
        from="assistant"
        data-testid="mcp-startup-indicator"
        role="status"
        aria-live="polite"
      >
        <MessageContent>
          <span className="flex items-center gap-2 text-muted-foreground text-ui">
            <Loader2Icon className="size-4 shrink-0 animate-spin" aria-hidden />
            {mcpStartingLine(starting, names.length)}
          </span>
        </MessageContent>
      </Message>
    );
  }
  const failed = names.filter((name) => mcpStartup[name].status === "failed");
  const cancelled = names.filter((name) => mcpStartup[name].status === "cancelled");
  if (failed.length === 0 && cancelled.length === 0) return null;
  const parts: string[] = [];
  if (failed.length > 0) parts.push(`failed: ${mcpSettledNames(failed)}`);
  if (cancelled.length > 0) parts.push(`cancelled: ${mcpSettledNames(cancelled)}`);
  return (
    <Message from="assistant" data-testid="mcp-startup-indicator" role="status">
      <MessageContent>
        <span className="flex items-center gap-2 text-muted-foreground text-ui">
          <AlertTriangleIcon className="size-4 shrink-0" aria-hidden />
          {`MCP startup incomplete (${parts.join("; ")})`}
        </span>
      </MessageContent>
    </Message>
  );
}
