import AntigravityColor from "@lobehub/icons/es/Antigravity/components/Color";
import DevinColor from "@lobehub/icons/es/Devin/components/Color";
import KiroColor from "@lobehub/icons/es/Kiro/components/Color";
import type { ComponentType } from "react";

import { ComposerAgentIcon } from "@/components/ComposerAgentIcon";
import type { Conversation } from "@/hooks/useConversations";
import type { Host } from "@/hooks/useHosts";
import { matchWorktree } from "@/hooks/useHostWorktrees";
import { useNow } from "@/hooks/useNow";
import { useSessionWorktrees } from "@/hooks/useSessionWorktrees";
import { sandboxOptionLabel } from "@/lib/capabilities";
import {
  nativeCodingAgentForWrapper,
  type NativeCodingAgentIconKind,
  WRAPPER_LABEL_KEY,
} from "@/lib/nativeCodingAgents";
import { relativeTime } from "@/lib/relativeTime";
import { isCurrentServerLocal } from "@/lib/serverOrigin";

import {
  deriveRepoName,
  localMachineLabel,
  resolveThisMachineHostId,
  SANDBOX_REPO_LABEL_KEY,
} from "./NewChatDialog";
import { useDesktopHostId } from "./RevealInFileManager";
import { getConversationAgentType } from "./sidebarNav";

/** Where a session runs: local machine, a sandbox provider, or the host's name (id while unresolved). */
export function sessionLocationLabel(
  conversation: Conversation,
  hostsById: ReadonlyMap<string, Host>,
): string {
  if (!conversation.host_id) return "Local machine";
  const host = hostsById.get(conversation.host_id);
  return host?.sandbox_provider
    ? sandboxOptionLabel(host.sandbox_provider)
    : (host?.name ?? conversation.host_id);
}

/** A row's short environment: the sandbox type, this device, or "Remote"; the tooltip names the host. */
function sessionEnvironmentLabel(
  conversation: Conversation,
  hostsById: ReadonlyMap<string, Host>,
  desktopHostId: string | null,
): string {
  const hostId = conversation.host_id;
  if (!hostId || hostsById.get(hostId)?.sandbox_provider) {
    return sessionLocationLabel(conversation, hostsById);
  }
  const onlineHostIds = [...hostsById.values()]
    .filter((h) => h.status === "online" && !h.sandbox_provider)
    .map((h) => h.host_id);
  return hostId === resolveThisMachineHostId(desktopHostId, isCurrentServerLocal(), onlineHostIds)
    ? localMachineLabel(navigator.userAgent)
    : "Remote";
}

/** A managed-sandbox session's repo name from its first `omnigent.sandbox.repo` label; null otherwise. */
export function sessionRepoName(conversation: Conversation): string | null {
  const labels = conversation.labels ?? {};
  const workspace = labels[`${SANDBOX_REPO_LABEL_KEY}.0`] ?? labels[SANDBOX_REPO_LABEL_KEY];
  // Label value is `<url>[#<branch>]`; the name is derived from the url part.
  return workspace ? deriveRepoName(workspace.split("#")[0]!) : null;
}

/**
 * The session's branch: its recorded `git_branch`, else its workspace worktree's.
 * Looks the worktree up only when `enabled`; null while detached or unknown.
 */
export function useSessionBranch(conversation: Conversation, enabled: boolean): string | null {
  const gitBranch = conversation.git_branch ?? null;
  const loadBranch = enabled && gitBranch === null;
  const { data: worktrees } = useSessionWorktrees(
    loadBranch ? conversation.id : null,
    loadBranch ? conversation.host_id : null,
    loadBranch ? conversation.workspace : null,
  );
  if (!enabled) return null;
  if (gitBranch !== null) return gitBranch;
  const workspaceWorktree =
    worktrees?.status === "ok" && conversation.workspace
      ? matchWorktree(worktrees.worktrees, conversation.workspace)
      : null;
  return workspaceWorktree?.detached ? null : (workspaceWorktree?.branch ?? null);
}

// Its own component so only rows showing Environment subscribe to the desktop host id.
export function SessionEnvironmentLabel({
  conversation,
  hostsById,
}: {
  conversation: Conversation;
  hostsById: ReadonlyMap<string, Host>;
}) {
  return sessionEnvironmentLabel(conversation, hostsById, useDesktopHostId());
}

// Its own component so only rows showing the timestamp subscribe to the shared clock.
export function SessionUpdatedLabel({ updatedAt }: { updatedAt: number }) {
  const now = useNow();
  return relativeTime(updatedAt * 1000, now.getTime());
}

// Brand-color glyphs for harnesses ComposerAgentIcon draws in mono. Claude and Codex
// are already in color there; Kimi's color mark is white-on-dark, and Cursor,
// OpenCode, Pi and Goose have none.
const TOOLTIP_COLOR_ICONS: Partial<
  Record<NativeCodingAgentIconKind, ComponentType<{ size?: number; className?: string }>>
> = { antigravity: AntigravityColor, devin: DevinColor, kiro: KiroColor };

// The session's harness glyph. `withFallback` draws the agent's generic icon for
// sessions without a known harness, the way project rows fall back to a folder.
export function SessionHarnessIcon({
  conversation,
  testId,
  withFallback = false,
}: {
  conversation: Conversation;
  testId: string;
  withFallback?: boolean;
}) {
  const nativeAgent = nativeCodingAgentForWrapper(conversation.labels?.[WRAPPER_LABEL_KEY]);
  const ColorIcon = nativeAgent ? TOOLTIP_COLOR_ICONS[nativeAgent.iconKind] : undefined;
  const harnessAgent = nativeAgent
    ? { name: nativeAgent.agentName, harness: nativeAgent.harness }
    : conversation.agent_name === "nessie"
      ? { name: "nessie", harness: null }
      : withFallback
        ? { name: conversation.agent_name ?? "", harness: conversation.harness ?? null }
        : null;
  if (!harnessAgent) return null;
  return (
    <span
      role="img"
      aria-label={`${getConversationAgentType(conversation)} harness`}
      data-testid={testId}
      className="flex shrink-0"
    >
      {ColorIcon ? (
        <ColorIcon size={16} className="shrink-0" />
      ) : (
        <ComposerAgentIcon agent={harnessAgent} className="size-4" />
      )}
    </span>
  );
}
