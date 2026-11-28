// The Pull Requests list's stacks: GitHub's own where the list's read says a PR has one, else
// from the descriptions its rows' own reads already bring.
// A group nests once its rows' reads have settled, so it lays itself out once while loading;
// rows GitHub stacks nest from GitHub's read without waiting on descriptions.

import { useEffect, useMemo, useRef, useState } from "react";

import { stackFormats } from "@/lib/pullRequestStacks/formats";
import {
  type ListStackPlacement,
  type ListStackRead,
  type NativeListStack,
  nestStackGroups,
} from "@/lib/pullRequestStacks/listNesting";
import { type NativeStackAnswer, readNativeStack } from "@/lib/pullRequestStacks/nativeStack";
import { parseStack } from "@/lib/pullRequestStacks/stackFormats";
import { type StackParse, stackPullRequestRef } from "@/lib/pullRequestStacks/stackModel";
import type { PullRequestListEntry } from "@/shell/PullRequestListRows";

/** How long a group waits for its rows' reads before nesting without the slow ones. */
export const STACK_SETTLE_LIMIT_MS = 2000;

/** What the list knows of a row's description now. */
export type RowStackBody =
  | { kind: "read"; body: string }
  /** Nothing is coming, or nothing usable came: not read here, failed, offline, or no body. */
  | { kind: "unread" }
  | { kind: "waiting" };

export function rowStackBody(entry: PullRequestListEntry | undefined): RowStackBody {
  const body = entry?.pr?.body;
  if (typeof body === "string") return { kind: "read", body };
  return entry?.status === "loading" ? { kind: "waiting" } : { kind: "unread" };
}

export interface StackNestingGroup {
  id: string;
  title: string;
  rows: readonly { url: string }[];
}

interface Barrier {
  scope: string;
  /**
   * Groups whose settle limit passed, counted from when each first showed: rows still waiting
   * count as unread until they answer.
   */
  expired: ReadonlySet<string>;
  /** Groups that nested once; a row that starts waiting later doesn't flatten them again. */
  settled: ReadonlySet<string>;
}

const startBarrier = (scope: string): Barrier => ({
  scope,
  expired: new Set(),
  settled: new Set(),
});

const withIds = (set: ReadonlySet<string>, ids: readonly string[]) => new Set([...set, ...ids]);

/**
 * Each group's rows in tree order once it settled, or undefined while it shows flat. Until
 * then, a group with rows GitHub stacks has them nested, the others flat. `scope` names the
 * reader: a change of account starts over, with nothing kept from the last one.
 */
export function useListStackNesting({
  enabled,
  scope,
  groups,
  entriesByUrl,
  nativeStackOf,
}: {
  enabled: boolean;
  scope: string;
  groups: readonly StackNestingGroup[];
  entriesByUrl: ReadonlyMap<string, PullRequestListEntry>;
  /** GitHub's own stack for a row, as the list's read gave it; undefined where it can't say. */
  nativeStackOf?: (url: string) => NativeStackAnswer | undefined;
}): ReadonlyMap<string, ListStackPlacement[]> | null {
  const [stored, setBarrier] = useState(() => startBarrier(scope));
  const barrier = stored.scope === scope ? stored : startBarrier(scope);
  if (stored !== barrier) setBarrier(barrier);
  // Each group's limit runs from when it first shows rows, not from when the list mounted.
  const deadlines = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  useEffect(() => {
    const timers = deadlines.current;
    return () => {
      for (const timer of timers.values()) clearTimeout(timer);
      timers.clear();
    };
  }, [enabled, scope]);
  const unsettledKey = JSON.stringify(
    groups
      .filter((group) => group.rows.length > 0 && !barrier.settled.has(group.id))
      .map((group) => group.id),
  );
  useEffect(() => {
    if (!enabled) return;
    for (const id of JSON.parse(unsettledKey) as string[]) {
      if (deadlines.current.has(id)) continue;
      const timer = setTimeout(
        () =>
          setBarrier((current) =>
            current.scope === scope
              ? { ...current, expired: withIds(current.expired, [id]) }
              : current,
          ),
        STACK_SETTLE_LIMIT_MS,
      );
      deadlines.current.set(id, timer);
    }
  }, [enabled, scope, unsettledKey]);

  // Parsed once per description, and only ever for the current reader.
  const parsed = useRef<{
    scope: string;
    byUrl: Map<string, { body: string; parse: StackParse | null }>;
  }>({
    scope,
    byUrl: new Map(),
  });
  if (parsed.current.scope !== scope) parsed.current = { scope, byUrl: new Map() };

  const bodies = useMemo(() => {
    const byUrl = new Map<string, RowStackBody>();
    if (!enabled) return byUrl;
    for (const group of groups) {
      for (const row of group.rows) byUrl.set(row.url, rowStackBody(entriesByUrl.get(row.url)));
    }
    return byUrl;
  }, [enabled, entriesByUrl, groups]);

  // GitHub's read of each row, once per answer: a row it stacks waits on no description.
  const natives = useMemo(() => {
    const byUrl = new Map<string, ReturnType<typeof nativeListStack>>();
    if (!enabled) return byUrl;
    for (const group of groups) {
      for (const row of group.rows)
        byUrl.set(row.url, nativeListStack(row.url, nativeStackOf?.(row.url)));
    }
    return byUrl;
  }, [enabled, groups, nativeStackOf]);

  // Off, the list reads no descriptions yet: nothing settles until it's on and they're known.
  const settledNow = useMemo(
    () =>
      groups
        .filter(
          (group) =>
            enabled &&
            (barrier.expired.has(group.id) ||
              barrier.settled.has(group.id) ||
              group.rows.every(
                (row) => bodies.get(row.url)?.kind !== "waiting" || natives.get(row.url)?.stack,
              )),
        )
        .map((group) => group.id),
    [barrier.expired, barrier.settled, bodies, enabled, groups, natives],
  );
  const newlySettled = JSON.stringify(settledNow.filter((id) => !barrier.settled.has(id)));
  useEffect(() => {
    const ids = JSON.parse(newlySettled) as string[];
    if (ids.length === 0) return;
    setBarrier((current) =>
      current.scope === scope ? { ...current, settled: withIds(current.settled, ids) } : current,
    );
  }, [newlySettled, scope]);

  return useMemo(() => {
    if (!enabled) return null;
    const cache = parsed.current.byUrl;
    const nativeOf = (url: string) =>
      natives.get(url) ?? nativeListStack(url, nativeStackOf?.(url));
    /** What GitHub's read alone says of a row: enough to place a row GitHub stacks. */
    const nativeReadOf = (url: string): ListStackRead => {
      const native = nativeOf(url);
      if (native.stack) return { kind: "native", stack: native.stack };
      return native.unstacked ? { kind: "unread", githubUnstacked: true } : { kind: "unread" };
    };
    const readOf = (url: string): ListStackRead => {
      // GitHub saying the PR is in a stack decides; its description is set aside then.
      const native = nativeReadOf(url);
      if (native.kind === "native") return native;
      const githubSays = native.githubUnstacked ? { githubUnstacked: true } : {};
      const state = bodies.get(url);
      if (state?.kind !== "read") return { kind: "unread", ...githubSays };
      const cached = cache.get(url);
      if (cached?.body === state.body) return { kind: "read", parse: cached.parse, ...githubSays };
      const self = stackPullRequestRef(url);
      const parse = self ? parseStack(stackFormats(), state.body, self) : null;
      cache.set(url, { body: state.body, parse });
      return { kind: "read", parse, ...githubSays };
    };
    const stackGroups = groups.map((group) => ({
      id: group.id,
      title: group.title,
      urls: group.rows.map((row) => row.url),
    }));
    const nested = nestStackGroups(stackGroups, readOf);
    const settled = new Set(settledNow);
    // A group still waiting on descriptions nests its GitHub-stacked rows already: their
    // places come from GitHub alone. The rows descriptions place wait, flat, as before.
    const waitingNative = groups.filter(
      (group) => !settled.has(group.id) && group.rows.some((row) => nativeOf(row.url).stack),
    );
    const nativeOnly = waitingNative.length > 0 ? nestStackGroups(stackGroups, nativeReadOf) : [];
    const waiting = new Set(waitingNative.map((group) => group.id));
    return new Map(
      groups.flatMap((group, index) =>
        settled.has(group.id)
          ? [[group.id, nested[index]!]]
          : waiting.has(group.id)
            ? [[group.id, nativeOnly[index]!]]
            : [],
      ),
    );
  }, [bodies, enabled, groups, nativeStackOf, natives, settledNow]);
}

/** What GitHub's read says of the row at `url`: its stack, or that it has none. */
function nativeListStack(
  url: string,
  answer: NativeStackAnswer | undefined,
): { stack: NativeListStack | null; unstacked: boolean } {
  const self = answer ? stackPullRequestRef(url) : null;
  if (!answer || !self) return { stack: null, unstacked: false };
  const read = readNativeStack(answer, self);
  if (read.kind === "stacked" || read.kind === "stacked-partial") {
    return { stack: read, unstacked: false };
  }
  return { stack: null, unstacked: read.kind === "not-stacked" };
}
