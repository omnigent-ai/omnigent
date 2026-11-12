/**
 * Roles the share dialog offers and how it names them.
 *
 * Permission levels are numeric ids, not ranks: Comment (5) sits between Read
 * and Edit. Compare roles with {@link roleRank}, never with `<`.
 */

import type { SharingMode } from "@/lib/capabilities";
import {
  LEVEL_COMMENT,
  LEVEL_EDIT,
  LEVEL_MANAGE,
  LEVEL_OWNER,
  LEVEL_READ,
} from "@/lib/permissionsApi";

/** Levels from least to most access. */
const ROLE_ORDER = [LEVEL_READ, LEVEL_COMMENT, LEVEL_EDIT, LEVEL_MANAGE, LEVEL_OWNER];

const ROLE_LABELS: Record<number, string> = {
  [LEVEL_READ]: "Read",
  [LEVEL_COMMENT]: "Comment",
  [LEVEL_EDIT]: "Edit",
  [LEVEL_MANAGE]: "Manage",
  [LEVEL_OWNER]: "Owner",
};

const ROLE_VERBS: Record<number, string> = {
  [LEVEL_READ]: "read",
  [LEVEL_COMMENT]: "comment",
  [LEVEL_EDIT]: "edit",
};

const ROLE_HINTS: Record<number, string> = {
  [LEVEL_READ]: "Can read the session",
  [LEVEL_COMMENT]: "Can read and leave comments",
  [LEVEL_EDIT]: "Can send messages and change files",
};

/**
 * Position of a level in the access order; unknown levels rank lowest.
 *
 * @param level Numeric permission level, e.g. ``2`` for Edit.
 * @returns ``0`` for Read up to ``4`` for Owner, or ``-1`` when unknown.
 */
export function roleRank(level: number): number {
  return ROLE_ORDER.indexOf(level);
}

/** Display label for a level, e.g. ``"Edit"``. */
export function roleLabel(level: number): string {
  return ROLE_LABELS[level] ?? `Level ${level}`;
}

/** What a role lets someone do, as a short hint for role menus. */
export function roleHint(level: number): string {
  return ROLE_HINTS[level] ?? "";
}

/** Verb for "Everyone with this link can …", e.g. ``"comment"``. */
export function roleVerb(level: number): string {
  return ROLE_VERBS[level] ?? "open it";
}

/**
 * Lower ``level`` to ``max`` when it ranks above it.
 *
 * @param level Requested level.
 * @param max Highest level allowed.
 * @returns ``level`` or ``max``, whichever gives less access.
 */
export function capRole(level: number, max: number): number {
  return roleRank(level) > roleRank(max) ? max : level;
}

/**
 * Roles the dialog may grant to people, least access first.
 *
 * - ``on``: Read, Comment, Edit.
 * - ``read_only`` / ``restricted_read_only``: Read, Comment.
 * - A session whose files are withheld (home or root cwd on a restricted
 *   server): Read only.
 *
 * Comment appears only when the server enables comment sharing.
 *
 * @param sharingMode The server's sharing mode.
 * @param filesWithheld Whether the session can never share its workspace files.
 * @param commentSharing Whether the server accepts Comment grants.
 * @returns Grantable levels, e.g. ``[1, 2]`` for Read and Edit.
 */
export function grantableRoles(
  sharingMode: SharingMode,
  filesWithheld: boolean,
  commentSharing: boolean,
): number[] {
  const roles = filesWithheld
    ? [LEVEL_READ]
    : sharingMode === "on"
      ? [LEVEL_READ, LEVEL_COMMENT, LEVEL_EDIT]
      : [LEVEL_READ, LEVEL_COMMENT];
  return roles.filter((level) => commentSharing || level !== LEVEL_COMMENT);
}

/**
 * Join role labels for a sentence, e.g. ``"read and comment"``.
 *
 * @param levels Levels to name, least access first.
 * @returns Lowercase labels joined with commas and a final "and".
 */
export function joinRoleNames(levels: readonly number[]): string {
  const names = levels.map((level) => roleLabel(level).toLowerCase());
  return names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names.at(-1)}` : names[0];
}
