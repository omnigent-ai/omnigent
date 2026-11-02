// @mentions in the message box: who can be addressed, and who a message reaches.

import { type BuiltInAgentRoleId, ROLES } from "../../shared/sdlcTheme.ts";
import { type TeamDefinition, shortRoleTitle } from "../../shared/team.ts";

export interface MentionTarget {
  /** What follows "@", e.g. "PM" or "QA". */
  handle: string;
  label: string;
  /** The session the mention delivers to; null when the role has not started. */
  sessionId: string | null;
}

const MENTION = /(^|[\s(])@([A-Za-z][\w-]*)/g;

/** Sessions an @mention in the text names, in order, without repeats; unknown handles are ignored. */
export function mentionedTargets(text: string, targets: MentionTarget[]): MentionTarget[] {
  const byHandle = new Map(targets.map((target) => [target.handle.toLowerCase(), target]));
  const found: MentionTarget[] = [];
  for (const match of text.matchAll(MENTION)) {
    const target = byHandle.get(match[2].toLowerCase());
    if (target && !found.includes(target)) found.push(target);
  }
  return found;
}

/** The "@word" being typed at the caret, if any, so suggestions can complete it. */
export function mentionAtCaret(text: string, caret: number): { start: number; query: string } | null {
  const before = text.slice(0, caret);
  const match = /(^|[\s(])@([\w-]*)$/.exec(before);
  if (!match) return null;
  return { start: caret - match[2].length - 1, query: match[2] };
}

/** Replace the "@word" being typed with a full mention, returning the new text and caret. */
export function insertMention(text: string, start: number, caret: number, handle: string): { text: string; caret: number } {
  const inserted = `@${handle} `;
  const rest = text.slice(caret).replace(/^[\w-]*/, "").replace(/^ /, "");
  return { text: `${text.slice(0, start)}${inserted}${rest}`, caret: start + inserted.length };
}

/** Targets whose handle or label starts with the typed query, best matches first. */
export function suggestMentions(query: string, targets: MentionTarget[]): MentionTarget[] {
  const lower = query.toLowerCase();
  return targets
    .filter((target) => target.handle.toLowerCase().startsWith(lower) || target.label.toLowerCase().includes(lower))
    .sort((a, b) => Number(!a.handle.toLowerCase().startsWith(lower)) - Number(!b.handle.toLowerCase().startsWith(lower)));
}

/** A mention handle for a free-form session name: "Fix the build" -> "fix-the-build". */
export function handleFor(name: string): string {
  const slug = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 24);
  return /^[a-z]/.test(slug) ? slug : `s-${slug || "session"}`;
}

// Short handles for SDLC roles; "Reviewer" is easier to type than "Reviewing Developer".
const ROLE_HANDLES: Record<BuiltInAgentRoleId, string> = {
  pm: "PM",
  architect: "Architect",
  developer: "Developer",
  reviewer: "Reviewer",
  qa: "QA",
  devops: "DevOps",
};

const themeTitle = (id: string) => ROLES.find((role) => role.id === id)?.title;

/** Every role of the room's team, addressed through its current session. */
export function roleTargets(team: TeamDefinition, sessionOf: (role: string) => string | null): MentionTarget[] {
  return team.roles.map((role) => ({
    // Theme roles keep their short handle unless the team renamed them; others use their
    // abbreviation or title without spaces, e.g. @FSD or @UXDesigner.
    handle:
      (role.title === themeTitle(role.id) ? ROLE_HANDLES[role.id as BuiltInAgentRoleId] : undefined) ??
      (shortRoleTitle(role.title).replace(/[^A-Za-z0-9]+/g, "") || role.id),
    label: role.title,
    sessionId: sessionOf(role.id),
  }));
}

/** Each session of an unthemed room, under a handle made from its name. */
export function sessionTargets(sessions: { id: string; name: string }[]): MentionTarget[] {
  const used = new Set<string>();
  return sessions.map((session) => {
    const base = handleFor(session.name);
    let handle = base;
    for (let suffix = 2; used.has(handle.toLowerCase()); suffix++) handle = `${base}-${suffix}`;
    used.add(handle.toLowerCase());
    return { handle, label: session.name, sessionId: session.id };
  });
}
