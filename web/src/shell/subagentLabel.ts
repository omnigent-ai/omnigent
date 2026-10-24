// Display label for a child (sub-agent) session, shared by the Agents rail
// rows and the approval cards a sub-agent's prompts mirror into, so a card
// names the sub-agent exactly as the rail lists it.

import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import { nativeCodingAgentForSubagentWrapper, WRAPPER_LABEL_KEY } from "@/lib/nativeCodingAgents";

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
