// Whose per-user state the Mac read path keeps: the workspace user on one server and API base
// path. A user id names someone only on its own server, so what was kept for "user-1" on one
// server is not for "user-1" on another, and no event says when the server changes.

import { durableServerScope } from "@/lib/durableServerScope";
import { getCurrentUserId } from "@/lib/identity";

/** The workspace user with the server and base path they're signed in to, as one key. */
export function readPathOwner(): string {
  return JSON.stringify([durableServerScope(), getCurrentUserId()]);
}

/** How a store hears that its owner changed: at once from an identity event, or lazily. */
export type ReadPathOwnerSwitch = "event" | "lazy";

/** A store's watch on its owner; see {@link createReadPathOwnerWatch}. */
export interface ReadPathOwnerWatch {
  /** Run the store's switch once if the owner differs from the last look; says whether it did. */
  (how?: ReadPathOwnerSwitch): boolean;
  /** Start over with no owner seen, for a store's test reset. */
  forget: () => void;
}

/**
 * A store's watch on its owner, which runs `onSwitch` when the owner changes. Stores call it from
 * identity events and as they're read, since a server switch fires nothing; a lazy switch should
 * notify listeners after the read.
 */
export function createReadPathOwnerWatch(
  onSwitch: (how: ReadPathOwnerSwitch) => void,
): ReadPathOwnerWatch {
  let owner: string | null = null;
  const check = (how: ReadPathOwnerSwitch = "lazy") => {
    const now = readPathOwner();
    if (owner === null || owner === now) {
      owner = now;
      return false;
    }
    owner = now;
    onSwitch(how);
    return true;
  };
  return Object.assign(check, {
    forget: () => {
      owner = null;
    },
  });
}
