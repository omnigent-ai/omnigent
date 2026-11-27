// The session row's actions as ⌘K offers them on any session: the same set, labels and gates
// as the sidebar row's menu, decided from plain facts about one session. No React here; the
// icons and the code that runs each action live beside the palette.

import type { Conversation } from "@/hooks/useConversations";
import { SHARING_OFF_REASON } from "@/lib/capabilities";
import { LEVEL_OWNER } from "@/lib/permissionsApi";
import {
  AVAILABLE,
  type CommandAvailability,
  HIDDEN,
  unavailableBecause,
} from "@/store/paletteCommands";

import { SESSION_COMMAND_IDS, type SessionCommandId } from "./commandIds";

export { SESSION_COMMAND_IDS, type SessionCommandId };

/** What the actions need to know about one session and the server it's on. */
export interface SessionCommandFacts {
  /** Still being created, so the server doesn't know it yet; its row offers no actions. */
  provisional: boolean;
  /** The viewer owns it, as far as the app can tell; see `sessionOwnership`. */
  owner: boolean;
  /** The row the app has names no owner, so the owner's actions wait for one that does. */
  ownerUnknown: boolean;
  archived: boolean;
  pinned: boolean;
  /** A family member shown under its leader, which pins and files with the family. */
  indentedSibling: boolean;
  /** No pin slot is left, so pinning another session needs an unpin first. */
  atPinCapacity: boolean;
  /** A pin write is saving; pin writes don't overlap. */
  pinSaving: boolean;
  /** It shows the unread dot. */
  unread: boolean;
  /** The viewer muted the session itself, which silences its unread dot. */
  muted: boolean;
  /** It sits in a project the viewer muted, which silences it too; only the project lifts it. */
  mutedByProject: boolean;
  /** The viewer may start an automation from it; see `canCreateAutomationFromSession`. */
  canCreateAutomation: boolean;
  /** The project it's filed under, if any. */
  currentProject: string | null;
  /** It can move into or out of a nest: a top-level session, on a server that moves nests. */
  nestable: boolean;
  /** Its runner may still be running. */
  stoppable: boolean;
  /** A new window can show it: windows open here, and this one doesn't show it on its own. */
  canOpenWindow: boolean;
  /** Why Fork can't run here, if it can't. */
  forkDisabledReason?: string;
  server: {
    /** One user, so there's nobody to share with or leave a session to. */
    singleUser: boolean;
    /** The admin turned sharing off for this server. */
    sharingOff: boolean;
    /** The server creates nested agents. */
    siblingSessions: boolean;
    /** The server can mute a session for the viewer alone. */
    sessionMute: boolean;
  };
}

export interface SessionCommand {
  id: SessionCommandId;
  label: string;
  /** Extra words ⌘K matches. */
  keywords: readonly string[];
  /** Ends or removes something, styled as the menu's destructive items are. */
  destructive: boolean;
  /** Opens a dialog or picker, which should take focus once the palette has closed. */
  opensDialog: boolean;
  availability: CommandAvailability;
}

/** Actions that ask or show something first; Archive can ask about worktrees. */
const OPENS_DIALOG: ReadonlySet<SessionCommandId> = new Set<SessionCommandId>([
  "share",
  "fork",
  "rename",
  "create-automation",
  "move-to-project",
  "move-to-nest",
  "stop",
  "archive",
  "delete",
  "leave",
]);

const DESTRUCTIVE: ReadonlySet<SessionCommandId> = new Set<SessionCommandId>([
  "stop",
  "delete",
  "leave",
]);

/** Why an action only the owner may take is greyed for everyone else, as the row menu says. */
export const OWNER_ONLY_REASONS = {
  share: "Only the session owner can share this session",
  rename: "Only the session owner can rename this session",
  stop: "Only the session owner can stop this session",
  archive: "Only the session owner can archive this session",
  unarchive: "Only the session owner can unarchive this session",
} as const;

/** Why the owner's actions are greyed while no cached row says whose the session is. */
export const UNKNOWN_OWNER_REASON = "Can't tell who owns this session";

export const PIN_SAVING_REASON = "A pin change is still saving.";
export const PIN_CAPACITY_REASON = "Unpin a session to pin another.";

const KEYWORDS: Record<SessionCommandId, readonly string[]> = {
  "add-sibling": ["nested", "sibling", "new agent", "family"],
  "open-in-new-window": ["window", "pop out", "separate"],
  pin: ["favorite", "keep", "top"],
  unpin: ["favorite", "remove pin"],
  share: ["permissions", "invite", "access"],
  fork: ["branch", "clone", "copy", "duplicate"],
  rename: ["title", "name"],
  mute: ["notifications", "silence", "quiet"],
  unmute: ["notifications", "unsilence"],
  "mark-unread": ["unread", "dot", "revisit", "flag"],
  "mark-read": ["read", "seen", "clear"],
  "create-automation": ["automation", "schedule", "recurring"],
  "move-to-project": ["project", "folder", "file", "move"],
  "move-to-nest": ["nest", "family", "group", "unnest", "remove from nest"],
  stop: ["terminate", "kill", "runner", "halt"],
  archive: ["hide", "done"],
  unarchive: ["restore", "unhide"],
  delete: ["remove", "trash"],
  leave: ["remove", "unshare"],
};

/**
 * Whose session `row` is. A row that names no owner and holds no owner grant counts as
 * unknown, and so as not the viewer's, unless the server is the single-user local runtime.
 */
export function sessionOwnership(
  row: Pick<Conversation, "owner" | "permission_level">,
  viewerId: string | null,
  singleUser: boolean,
): "mine" | "theirs" | "unknown" {
  if (singleUser) return "mine";
  if (row.owner != null) return row.owner === viewerId ? "mine" : "theirs";
  if (row.permission_level != null && row.permission_level >= LEVEL_OWNER) return "mine";
  // No viewer yet, or a header-auth server that names none: nothing shows the row is theirs.
  return "unknown";
}

function ownerOnly(
  { owner, ownerUnknown }: Pick<SessionCommandFacts, "owner" | "ownerUnknown">,
  reason: string,
): CommandAvailability {
  if (owner) return AVAILABLE;
  return unavailableBecause(ownerUnknown ? UNKNOWN_OWNER_REASON : reason);
}

function when(condition: boolean, availability: CommandAvailability): CommandAvailability {
  return condition ? availability : HIDDEN;
}

/** A local server's links open nowhere else, so there's nothing to share. */
export const LOCAL_SHARING_REASON = "Sharing is unavailable from a local server.";

/** What sharing a session, or a file in it, depends on. */
export type SessionShareFacts = Pick<SessionCommandFacts, "owner" | "ownerUnknown"> & {
  /** False for a sub-agent, which shares through its parent; unset when the row doesn't say. */
  topLevel?: boolean;
  server: Pick<SessionCommandFacts["server"], "singleUser" | "sharingOff"> & { local?: boolean };
};

/** Whether the viewer can share the session, or one of its files. */
export function sessionShareAvailability(facts: SessionShareFacts): CommandAvailability {
  const { server } = facts;
  // One user has nobody to share with, and a sub-agent inherits its parent's grants.
  if (server.singleUser || facts.topLevel === false) return HIDDEN;
  // Server-wide reasons outrank the viewer's own.
  if (server.sharingOff) return unavailableBecause(SHARING_OFF_REASON);
  if (server.local) return unavailableBecause(LOCAL_SHARING_REASON);
  return ownerOnly(facts, OWNER_ONLY_REASONS.share);
}

function pinAvailability(facts: SessionCommandFacts, pinning: boolean): CommandAvailability {
  // Archiving outranks pinning, and a nested family member pins with its family.
  if (facts.archived || facts.indentedSibling || facts.pinned === pinning) return HIDDEN;
  if (facts.pinSaving) return unavailableBecause(PIN_SAVING_REASON);
  if (pinning && facts.atPinCapacity) return unavailableBecause(PIN_CAPACITY_REASON);
  return AVAILABLE;
}

const LABELS: Record<SessionCommandId, string> = {
  "add-sibling": "Add nested agent",
  "open-in-new-window": "Open in new window",
  pin: "Pin",
  unpin: "Unpin",
  share: "Share",
  fork: "Fork",
  rename: "Rename",
  mute: "Mute",
  unmute: "Unmute",
  "mark-unread": "Mark as unread",
  "mark-read": "Mark as read",
  "create-automation": "Create automation",
  "move-to-project": "Add to project",
  "move-to-nest": "Move to nest",
  stop: "Stop session",
  archive: "Archive",
  unarchive: "Unarchive",
  delete: "Delete",
  leave: "Leave session",
};

function availabilityOf(id: SessionCommandId, facts: SessionCommandFacts): CommandAvailability {
  const { owner, server } = facts;
  // Each action writes to the server, which can't act on the session before it exists.
  if (facts.provisional) return HIDDEN;
  switch (id) {
    case "add-sibling":
      return when(owner && !facts.archived && server.siblingSessions, AVAILABLE);
    case "open-in-new-window":
      return when(facts.canOpenWindow, AVAILABLE);
    case "pin":
      return pinAvailability(facts, true);
    case "unpin":
      return pinAvailability(facts, false);
    case "share":
      return sessionShareAvailability(facts);
    case "fork":
      return facts.forkDisabledReason ? unavailableBecause(facts.forkDisabledReason) : AVAILABLE;
    case "rename":
      return ownerOnly(facts, OWNER_ONLY_REASONS.rename);
    // As the row menu shows it: while the project's mute applies, the session's own toggle
    // would change nothing visible, so it's greyed.
    case "mute":
      return when(server.sessionMute && !facts.muted && !facts.mutedByProject, AVAILABLE);
    case "unmute":
      if (server.sessionMute && facts.mutedByProject) {
        return unavailableBecause(MUTED_WITH_PROJECT_REASON);
      }
      return when(server.sessionMute && facts.muted, AVAILABLE);
    // A muted session's dot is silenced, so it can be marked read but not flagged unread.
    case "mark-unread":
      return when(!facts.unread && !facts.muted && !facts.mutedByProject, AVAILABLE);
    case "mark-read":
      return when(facts.unread, AVAILABLE);
    case "create-automation":
      return when(facts.canCreateAutomation, AVAILABLE);
    // Filing is the owner's own tool, and a nested family member files with its family.
    case "move-to-project":
      return when(owner && !facts.indentedSibling, AVAILABLE);
    // As the row menu's Move session offers it: the owner's top-level, unarchived sessions.
    case "move-to-nest":
      return when(owner && facts.nestable, AVAILABLE);
    case "stop":
      return when(facts.stoppable, ownerOnly(facts, OWNER_ONLY_REASONS.stop));
    case "archive":
      return when(!facts.archived, ownerOnly(facts, OWNER_ONLY_REASONS.archive));
    case "unarchive":
      return when(facts.archived, ownerOnly(facts, OWNER_ONLY_REASONS.unarchive));
    // The owner deletes; anyone it's shared with leaves. One user only deletes.
    // Neither is offered on a guess at whose session it is.
    case "delete":
      if (facts.ownerUnknown) return unavailableBecause(UNKNOWN_OWNER_REASON);
      return when(owner || server.singleUser, AVAILABLE);
    case "leave":
      if (facts.ownerUnknown) return unavailableBecause(UNKNOWN_OWNER_REASON);
      return when(!owner && !server.singleUser, AVAILABLE);
  }
}

/** Why a session in a muted project can't be unmuted on its own. */
export const MUTED_WITH_PROJECT_REASON = "Unmute its project to hear from this session again.";

function labelOf(id: SessionCommandId, facts: SessionCommandFacts): string {
  if (id === "move-to-project" && facts.currentProject !== null) return "Move session";
  if (id === "unmute" && facts.mutedByProject) return "Muted with project";
  return LABELS[id];
}

/** Every action for `facts`, in the row menu's order; hidden ones have no reason. */
export function sessionCommands(facts: SessionCommandFacts): SessionCommand[] {
  return SESSION_COMMAND_IDS.map((id) => ({
    id,
    label: labelOf(id, facts),
    keywords: KEYWORDS[id],
    destructive: DESTRUCTIVE.has(id),
    opensDialog: OPENS_DIALOG.has(id),
    availability: availabilityOf(id, facts),
  }));
}

/** The action `id` for `facts`. */
export function sessionCommand(facts: SessionCommandFacts, id: SessionCommandId): SessionCommand {
  return sessionCommands(facts).find((command) => command.id === id)!;
}

/** How ⌘K offers an action on a session the user picks, rather than the open one. */
export interface SessionCommandPick {
  /** The Commands row, such as "Archive a Session…". */
  label: string;
  /** The picker's heading while it waits for a session, such as "Archive which session?". */
  question: string;
  /** What the actions page says when the action doesn't apply to `title`. */
  notAvailable: (title: string) => string;
  /** The picker lists archived sessions too, since the action applies to them. */
  includesArchived: boolean;
}

function pick(
  label: string,
  question: string,
  verb: string,
  { includesArchived = false } = {},
): SessionCommandPick {
  return {
    label,
    question,
    notAvailable: (title) => `${verb} isn't available for “${title}”.`,
    includesArchived,
  };
}

/** The sidebar row actions ⌘K can start from a verb, then run on a session the user picks. */
export const SESSION_COMMAND_PICKS: Partial<Record<SessionCommandId, SessionCommandPick>> = {
  "add-sibling": pick(
    "Add a Nested Agent to a Session…",
    "Add a nested agent to which session?",
    "Add nested agent",
  ),
  pin: pick("Pin a Session…", "Pin which session?", "Pin"),
  unpin: pick("Unpin a Session…", "Unpin which session?", "Unpin"),
  share: pick("Share a Session…", "Share which session?", "Share"),
  fork: pick("Fork a Session…", "Fork which session?", "Fork"),
  rename: pick("Rename a Session…", "Rename which session?", "Rename"),
  "mark-unread": pick(
    "Mark a Session as Unread…",
    "Mark which session as unread?",
    "Mark as unread",
  ),
  "mark-read": pick("Mark a Session as Read…", "Mark which session as read?", "Mark as read"),
  "create-automation": pick(
    "Create an Automation from a Session…",
    "Create an automation from which session?",
    "Create automation",
  ),
  "move-to-project": pick(
    "Add a Session to a Project…",
    "Add which session to a project?",
    "Add to project",
  ),
  "move-to-nest": pick(
    "Move a Session to a Nest…",
    "Move which session to a nest?",
    "Move to nest",
  ),
  stop: pick("Stop a Session…", "Stop which session?", "Stop"),
  archive: pick("Archive a Session…", "Archive which session?", "Archive"),
  unarchive: pick("Unarchive a Session…", "Unarchive which session?", "Unarchive", {
    includesArchived: true,
  }),
  delete: pick("Delete a Session…", "Delete which session?", "Delete", {
    includesArchived: true,
  }),
  leave: pick("Leave a Session…", "Leave which session?", "Leave"),
};
