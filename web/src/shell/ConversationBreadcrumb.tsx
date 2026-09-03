import { BotIcon, ChevronLeftIcon, FolderIcon } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "@/lib/routing";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { isAndroidShell, isIOSShell } from "@/lib/nativeBridge";
import { nativeCodingAgentForSubagentWrapper } from "@/lib/nativeCodingAgents";
import type { Agent } from "@/hooks/useAgents";
import { cn } from "@/lib/utils";

/**
 * `[folder] / <title> [/ <sub-agent>]` breadcrumb for the active conversation.
 *
 * Rendered in the chat header's left slot (ChatHeader). On the macOS shell with
 * the sidebar collapsed the slot is padded clear of the traffic lights and the
 * title-bar cluster (see `.traffic-light-clearance` in index.css), so the
 * breadcrumb stays in the header — truncating within its flex — rather than
 * overlapping the window controls.
 *
 * The caller mounts this when there is a title or a parent route to climb
 * back to. Segments self-gate: the folder only shows when filed, the
 * sub-agent only inside a child. The title links back to the parent when
 * `titleLinkTo` is set (viewing a sub-agent), else it's plain text.
 */
export function ConversationBreadcrumb({
  conversationTitle,
  projectName,
  projectTag,
  titleSlot,
  titleLinkTo,
  isChildSession,
  subAgentName,
  boundAgent,
  wrapperLabel,
  actions,
  className,
}: {
  /** The conversation's display name. */
  conversationTitle: string;
  /** Project the conversation is filed under, or `null` when unfiled. */
  projectName: string | null;
  /**
   * Leading folder segment. When set (the desktop title shortcut), it replaces
   * the static folder icon with an interactive "Move to…" trigger and self-gates
   * its own visibility. When omitted, a static folder renders iff `projectName`
   * is set — the fallback for surfaces without the shortcut (e.g. sub-agents).
   */
  projectTag?: ReactNode;
  /**
   * Interactive title node (the desktop click-to-rename control). Replaces the
   * plain-text title. Ignored when `titleLinkTo` is set — a sub-agent keeps its
   * back-to-parent link rather than becoming editable.
   */
  titleSlot?: ReactNode;
  /** Parent-session route the title links to, or `undefined` for plain text. */
  titleLinkTo?: string;
  /** Whether the active session is a sub-agent (appends its identity). */
  isChildSession: boolean;
  /**
   * The session's own `sub_agent_name` — the dispatched sub-agent's identity
   * (e.g. a `sys_session_send` child of a bundle). Preferred over
   * `boundAgent.name`, which for such children is the *parent* bundle's row.
   */
  subAgentName?: string | null;
  /** The bound agent — names the sub-agent segment. */
  boundAgent: Agent | undefined;
  /** The session's `omnigent.wrapper` label — names a native sub-agent's vendor. */
  wrapperLabel: string | null;
  /** Session-management menu rendered immediately after the title. */
  actions?: ReactNode;
  /** Extra classes for the context (header vs title-bar strip). */
  className?: string;
}) {
  // A native sub-agent (a Claude Code Task, a Codex collab thread) is bound to
  // its parent's `<vendor>-native-ui` row, so its agent name is an internal the
  // server itself hides (`public_agent_name`). Name the product instead,
  // matching the Agents rail and the composer. Otherwise prefer the session's
  // own `sub_agent_name`: a `sys_session_send` child is bound to its parent
  // bundle's agent row, so `boundAgent.name` would misidentify it as the
  // parent. `boundAgent.name` remains the fallback for the Add-Agent flow,
  // where the child is bound to its own agent and `sub_agent_name` is null.
  const subAgentSegment = isChildSession
    ? (nativeCodingAgentForSubagentWrapper(wrapperLabel)?.displayName ??
      (subAgentName?.trim() || null) ??
      boundAgent?.name ??
      null)
    : null;
  // iOS/Android native chrome already identifies the session. Restore the
  // compact "< Back" climb-out there; web / Electron keep the parent name.
  const nativeMobileBack = isIOSShell() || isAndroidShell();
  return (
    <nav
      aria-label="Conversation"
      className={cn("conversation-breadcrumb flex min-w-0 items-center gap-1.5 text-ui", className)}
    >
      {projectTag ??
        (projectName && (
          <div className="hidden md:flex min-w-0 items-center gap-1.5 text-ui shrink-0">
            <Tooltip>
              <TooltipTrigger asChild>
                <span
                  className="breadcrumb-folder flex shrink-0 items-center text-muted-foreground opacity-40 hover:opacity-100"
                  aria-label={`Project: ${projectName}`}
                >
                  <FolderIcon className="size-4" />
                </span>
              </TooltipTrigger>
              <TooltipContent side="bottom" align="start">
                <div>
                  <span className="font-semibold text-ui">{conversationTitle}</span>
                  <div className="flex gap-1 text-muted-foreground">
                    <FolderIcon className="size-4" />
                    {projectName}
                  </div>
                </div>
              </TooltipContent>
            </Tooltip>
            <span aria-hidden className="shrink-0 text-muted-foreground opacity-40">
              /
            </span>
          </div>
        ))}
      {titleLinkTo ? (
        <Link
          to={titleLinkTo}
          aria-label="Back to parent session"
          className={cn(
            "breadcrumb-parent-link min-w-0 text-muted-foreground hover:text-foreground",
            nativeMobileBack
              ? "inline-flex shrink-0 items-center gap-0.5"
              : "truncate hover:underline",
          )}
        >
          {nativeMobileBack ? (
            <>
              <ChevronLeftIcon className="size-4" />
              <span>Back</span>
            </>
          ) : (
            conversationTitle
          )}
        </Link>
      ) : (
        (titleSlot ?? <span className="min-w-0 truncate text-foreground">{conversationTitle}</span>)
      )}
      {actions}
      {isChildSession && (
        <>
          <span aria-hidden className="shrink-0 text-muted-foreground opacity-40">
            /
          </span>
          <span className="flex min-w-0 items-center gap-1.5">
            <BotIcon className="size-4 shrink-0 text-muted-foreground" />
            <span className="truncate font-semibold text-foreground">
              {subAgentSegment ?? "Sub-agent"}
            </span>
          </span>
        </>
      )}
    </nav>
  );
}
