// Display labels for a child (sub-agent) session: the Agents rail rows and the
// approval cards a sub-agent's prompts mirror into share one, so a card names
// the sub-agent exactly as the rail lists it; the composer tray uses the other.

import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import {
  ANTIGRAVITY_NATIVE_SUBAGENT_WRAPPER,
  CLAUDE_NATIVE_SUBAGENT_WRAPPER,
  CODEX_NATIVE_SUBAGENT_WRAPPER,
  claudeNativeSubagentLabel,
  codexNativeSubagentLabel,
  OPENCODE_NATIVE_SUBAGENT_WRAPPER,
  WRAPPER_LABEL_KEY,
} from "@/lib/nativeCodingAgents";
import type { Session } from "@/lib/types";

/**
 * Pick the primary label for a child-session row.
 *
 * @param child - One child-session summary from the poll or stream.
 * @returns The label shown beside the child icon.
 */
export function childPrimaryLabel(child: ChildSessionInfo): string {
  // User-added rows use the reserved "ui:<agent>:<name>" title sentinel;
  // LLM-spawned titles cannot start with "ui:" because the spec validator
  // rejects "ui" as a sub-agent name.
  const isUserAdded = child.title?.startsWith("ui:") ?? false;
  const childWrapper = child.labels?.[WRAPPER_LABEL_KEY];
  // agy child titles are ``"<role>:<cascade id>"``: the generic path below
  // would show the cascade UUID, so native sub-agents use ``tool`` instead.
  const isNativeSubagent =
    childWrapper === CODEX_NATIVE_SUBAGENT_WRAPPER ||
    childWrapper === OPENCODE_NATIVE_SUBAGENT_WRAPPER ||
    childWrapper === ANTIGRAVITY_NATIVE_SUBAGENT_WRAPPER ||
    childWrapper === CLAUDE_NATIVE_SUBAGENT_WRAPPER;
  if (isNativeSubagent && !isUserAdded) {
    return child.tool ?? child.title ?? child.id;
  }
  let titleTask: string | null = null;
  if (child.title?.includes(":")) {
    const titleSuffix = child.title.split(":").slice(1).join(":");
    if (titleSuffix) titleTask = titleSuffix;
  }
  return (
    child.task_summary ?? child.session_name ?? titleTask ?? child.title ?? child.tool ?? child.id
  );
}

/**
 * Resolve the sub-agent instance label for the composer's "Chatting with
 * sub-agent …" tray, mirroring the Agents rail's child-row label
 * (``childPrimaryLabel`` above).
 *
 * The spawn tool seeds a sub-agent's title as ``"{tool}:{name}"`` (e.g.
 * ``"claude_code:check-account-eligibility"``), so the human instance name
 * is the suffix after the first ``":"``. User-added rows carry a reserved
 * ``"ui:<agent>:<name>"`` sentinel; the ``"ui:"`` marker is stripped first
 * so the suffix is still the human name. Falls back to the bare title,
 * then the sub-agent type, then the bound agent name.
 *
 * @param session - The active session snapshot, or ``null`` while it loads
 *   / on the new-chat landing.
 * @returns The tray label, e.g. ``"check-account-eligibility"``; ``null``
 *   for a top-level session (no ``parentSessionId``) or when no snapshot is
 *   loaded — both hide the tray.
 */
export function subAgentComposerLabel(
  session: Pick<
    Session,
    "parentSessionId" | "title" | "subAgentName" | "agentName" | "labels"
  > | null,
): string | null {
  if (!session || session.parentSessionId == null) return null;
  const claudeLabel = claudeNativeSubagentLabel(session.labels, session.subAgentName);
  if (claudeLabel) return claudeLabel;
  // Codex children (incl. /side forks) title as "codex-native-ui-subagent:<uuid>";
  // show the friendly nickname label ("Side chat") instead of that UUID suffix.
  const codexLabel = codexNativeSubagentLabel(session.labels);
  if (codexLabel) return codexLabel;
  // Strip the user-added "ui:" sentinel so its "agent:name" suffix reads
  // like an LLM-spawned title.
  let title = session.title ?? null;
  if (title?.startsWith("ui:")) title = title.slice(3);
  if (title?.includes(":")) {
    const suffix = title.split(":").slice(1).join(":");
    if (suffix) return suffix;
  }
  // Last-resort display string: a sub-agent session always has a seeded
  // title in practice, so the final "sub-agent" only guards a degenerate
  // all-null snapshot (the tray still needs something to render).
  return title ?? session.subAgentName ?? session.agentName ?? "sub-agent";
}
