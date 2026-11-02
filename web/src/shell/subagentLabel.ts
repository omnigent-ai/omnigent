// Display labels for a child (sub-agent) session: the Agents rail rows and the
// approval cards a sub-agent's prompts mirror into share one, so a card names
// the sub-agent exactly as the rail lists it; the composer tray uses the other.

import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import {
  claudeNativeSubagentLabel,
  codexNativeSubagentLabel,
  nativeCodingAgentForSubagentWrapper,
  WRAPPER_LABEL_KEY,
} from "@/lib/nativeCodingAgents";
import type { Session } from "@/lib/types";

// User-added rows use this reserved title sentinel (``"ui:<agent>:<name>"``);
// LLM-spawned titles cannot start with it because the spec validator rejects
// "ui" as a sub-agent name.
const USER_ADDED_PREFIX = "ui:";

/** A sub-agent title without the reserved user-added sentinel. */
export function titleWithoutSentinel(title: string | null | undefined): string | null {
  if (title == null) return null;
  return title.startsWith(USER_ADDED_PREFIX) ? title.slice(USER_ADDED_PREFIX.length) : title;
}

/**
 * The handle after the ``"{tool}:"`` prefix of a sub-agent title such as
 * ``"worker:deploy-profile-page"``, or ``null`` when the title has no suffix.
 */
export function titleHandle(title: string | null | undefined): string | null {
  const source = titleWithoutSentinel(title);
  if (!source?.includes(":")) return null;
  return source.split(":").slice(1).join(":") || null;
}

/**
 * Pick the primary label for a child-session row.
 *
 * The generated ``task_summary`` leads so sibling rows read by what they do; the
 * orchestrator-chosen handle (``session_name`` or the ``"{tool}:{name}"`` suffix)
 * follows it, e.g. ``"Deploy PR41 via web ops · deploy-profile-page"``.
 *
 * @param child - One child-session summary from the poll or stream.
 * @param fallback - Label for a row with no name at all; defaults to the id.
 * @returns The label shown beside the child icon.
 */
export function childPrimaryLabel(child: ChildSessionInfo, fallback: string = child.id): string {
  const trimmed = (value: string | null | undefined) => value?.trim() || null;
  const isUserAdded = child.title?.startsWith(USER_ADDED_PREFIX) ?? false;
  // Native sub-agent titles are ``"<role>:<opaque id>"``: the generic path
  // below would show that id, so every registered native wrapper uses ``tool``.
  const isNativeSubagent =
    nativeCodingAgentForSubagentWrapper(child.labels?.[WRAPPER_LABEL_KEY]) !== undefined;
  if (isNativeSubagent && !isUserAdded) {
    return trimmed(child.tool) ?? trimmed(child.title) ?? fallback;
  }
  const handle = trimmed(child.session_name) ?? trimmed(titleHandle(child.title));
  const summary = trimmed(child.task_summary);
  // Only an identical summary suppresses the handle: substring matching would
  // hide valid handles such as "api" inside "Capitalize".
  if (summary && handle && handle !== summary) return `${summary} · ${handle}`;
  return (
    summary ??
    handle ??
    trimmed(titleWithoutSentinel(child.title)) ??
    trimmed(child.tool) ??
    fallback
  );
}

/**
 * Resolve the sub-agent instance label for the composer's "Chatting with
 * sub-agent …" tray: the orchestrator-chosen handle (``titleHandle``), which
 * the Agents rail's child-row label (``childPrimaryLabel`` above) also ends
 * with after any generated task summary.
 *
 * The spawn tool seeds a sub-agent's title as ``"{tool}:{name}"`` (e.g.
 * ``"claude_code:check-account-eligibility"``), so the human instance name
 * is the suffix after the first ``":"``; user-added rows carry a reserved
 * ``"ui:"`` sentinel that is stripped first. Falls back to the bare title,
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
  const handle = titleHandle(session.title);
  if (handle) return handle;
  // Last-resort display string: a sub-agent session always has a seeded
  // title in practice, so the final "sub-agent" only guards a degenerate
  // all-null snapshot (the tray still needs something to render).
  return (
    titleWithoutSentinel(session.title) ?? session.subAgentName ?? session.agentName ?? "sub-agent"
  );
}
