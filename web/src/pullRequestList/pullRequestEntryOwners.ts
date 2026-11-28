// Which mounted list a page's PR menus come from. The table owns the PRs it lists while it's
// mounted; a source beside it owns the page's other PRs (Recently reviewed, sidebar tabs), so
// each PR has one owner, and a hidden or replaced table hands its PRs over and takes them back.

import { useEffect, useRef, useSyncExternalStore } from "react";

import { pullRequestKey } from "@/lib/pullRequestReference";
import type { RowAction } from "@/shell/PullRequestListRows";

/** What a mounted list offers for the PRs it shows now. */
export interface ListedEntries {
  /** Keys of the PRs it shows, by {@link pullRequestKey}. */
  keys: ReadonlySet<string>;
  /** The PR's menu as the list offers it now; null where it doesn't show the PR. */
  actionsFor: (url: string) => RowAction[] | null;
}

type Role = "table" | "beside";

export interface PullRequestEntryOwners {
  /** Register a mounted list as `role` until the returned function is called. */
  register: (role: Role, listed: { current: ListedEntries }) => () => void;
  /** Say a registered list's PRs or menus changed. */
  changed: () => void;
  /** The mounted table's PR keys, joined; "" while no table is mounted. */
  tableKeys: () => string;
  /** The PR's menu from the list that owns it now: the table where it lists it, else beside. */
  menuFor: (url: string) => RowAction[] | null;
  subscribe: (listener: () => void) => () => void;
  version: () => number;
}

export function createPullRequestEntryOwners(): PullRequestEntryOwners {
  const lists: { role: Role; listed: { current: ListedEntries } }[] = [];
  const listeners = new Set<() => void>();
  let version = 0;
  let tableKeys = "";
  const changed = () => {
    const table = lists.findLast((list) => list.role === "table");
    tableKeys = table ? [...table.listed.current.keys].sort().join("\n") : "";
    version += 1;
    for (const listener of listeners) listener();
  };
  const owner = (role: Role, key: string) =>
    lists.findLast((list) => list.role === role && list.listed.current.keys.has(key));
  return {
    register(role, listed) {
      const list = { role, listed };
      lists.push(list);
      changed();
      return () => {
        const at = lists.indexOf(list);
        if (at >= 0) lists.splice(at, 1);
        changed();
      };
    },
    changed,
    tableKeys: () => tableKeys,
    menuFor(url) {
      const key = pullRequestKey(url);
      const list = owner("table", key) ?? owner("beside", key);
      return list ? list.listed.current.actionsFor(url) : null;
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    version: () => version,
  };
}

const NO_OWNERS_KEYS = () => "";
const NO_VERSION = () => 0;
const NO_SUBSCRIPTION = () => () => {};

/**
 * Register this mounted list's PRs and menus with the page's owners while it's mounted, so
 * menus elsewhere on the page take a PR it shows from it, and from nothing once it's gone.
 */
export function useRegisterListedEntries(
  owners: PullRequestEntryOwners | undefined,
  role: Role,
  current: ListedEntries,
): void {
  const listed = useRef(current);
  listed.current = current;
  useEffect(() => owners?.register(role, listed), [owners, role]);
  const keys = [...current.keys].sort().join("\n");
  const { actionsFor } = current;
  // Menus open now read the new actions, and the source beside the table its new keys.
  useEffect(() => owners?.changed(), [owners, keys, actionsFor]);
}

/** The table's PR keys while one is mounted, redrawn as it mounts, unmounts or changes. */
export function useTableEntryKeys(owners: PullRequestEntryOwners | undefined): ReadonlySet<string> {
  const joined = useSyncExternalStore(
    owners?.subscribe ?? NO_SUBSCRIPTION,
    owners?.tableKeys ?? NO_OWNERS_KEYS,
  );
  return useStableKeys(joined);
}

function useStableKeys(joined: string): ReadonlySet<string> {
  const cached = useRef<{ joined: string; keys: ReadonlySet<string> }>({
    joined: "",
    keys: new Set(),
  });
  if (cached.current.joined !== joined) {
    cached.current = { joined, keys: new Set(joined ? joined.split("\n") : []) };
  }
  return cached.current.keys;
}

/** The PR's menu from the list that owns it now, redrawn as either list changes. */
export function usePullRequestEntryMenu(
  owners: PullRequestEntryOwners | undefined,
  url: string,
): RowAction[] | null {
  useSyncExternalStore(owners?.subscribe ?? NO_SUBSCRIPTION, owners?.version ?? NO_VERSION);
  return owners?.menuFor(url) ?? null;
}
