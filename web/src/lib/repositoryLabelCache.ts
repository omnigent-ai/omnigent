// A repository's labels for the label picker, cached per repository rather than per pull
// request. Read on github.com as this Mac's own account, or as a login the Pull Requests page
// picked, they're also kept on this device, so the picker lists them at once after a restart
// and reads GitHub again behind them. A picked login's restore once a fresh read this run
// proved that login; another login's stay kept, never shown. GitSpoke's stay in memory only.

import type { QueryKey } from "@tanstack/react-query";
import { z } from "zod";

import { subscribeDesktopReadPath } from "@/lib/desktopReadPath";
import { getCurrentUserId } from "@/lib/identity";
import {
  createMacDurableReadCache,
  durableServerScope,
  type MacDurableKeep,
} from "@/lib/macDurableReadCache";
import { namedReadsAvailable, namedReadsSettled, subscribeNamedReads } from "@/lib/macNamedReads";
import { macNamedReadScopeKey, macReadScopeKey } from "@/lib/macReadScope";

/** One label as the picker lists it. */
export interface RepositoryLabel {
  name: string;
  /** Six hex digits, lowercase, or null when GitHub's isn't one. */
  color: string | null;
  description: string | null;
}

/** The repository's labels as the picker reads them. */
export interface RepositoryLabels {
  object: "list";
  /** Sorted by name. */
  data: RepositoryLabel[];
  /** The repository has more labels than one read lists. */
  has_more: boolean;
}

/** A label read's answer, with the login GitHub read it as, where this Mac read it. */
export interface RepositoryLabelsSnapshot {
  labels: RepositoryLabels;
  viewer: string | null;
  /** Set on an answer restored from this device, until GitHub answers again. */
  restored?: true;
}

export const REPOSITORY_LABELS_OPERATION = "pull_request_label_options";
const GITHUB_HOST = "github.com";
const STORAGE_PREFIX = "omnigent:repository-labels";
/** Moves when the saved shape changes, so older entries are dropped. */
const SCHEMA = "1";
const MAX_AGE_MS = 30 * 24 * 60 * 60_000;
/** One repository's labels, at most; a larger answer is read again instead. */
export const MAX_SAVED_LABEL_CHARS = 512 * 1024;
/** Every repository's labels together, at most; the least recently used go first. */
export const MAX_SAVED_LABELS_TOTAL_CHARS = 1024 * 1024;

const labelSchema = z.object({
  name: z.string().min(1),
  color: z
    .string()
    .regex(/^[0-9a-f]{6}$/)
    .nullable(),
  description: z.string().nullable(),
});
const savedLabelsSchema = z.object({
  viewer: z.string().min(1),
  labels: z.object({
    object: z.literal("list"),
    data: z.array(labelSchema),
    has_more: z.boolean(),
  }),
});
type SavedLabels = z.infer<typeof savedLabelsSchema>;

/** A GitHub login as named scopes key it, lowercased. */
const LOGIN_PATTERN = /^[a-z0-9][a-z0-9_-]{0,99}$/;

/** `owner/name` as a key part: GitHub's names ignore case. */
const repositoryKey = (repository: string) => repository.toLowerCase();

/**
 * The query key for a repository's labels read on this Mac: the server, then the read scope
 * (`scope`, the host's account's or a named login's), then the repository, so every pull
 * request of one repository shares it and nothing else does.
 */
export function repositoryLabelsKey(
  scope: readonly unknown[],
  repository: string,
  server: string | null = durableServerScope(),
) {
  return [
    "code-host-mac",
    REPOSITORY_LABELS_OPERATION,
    "server",
    server,
    ...scope,
    repositoryKey(repository),
  ] as const;
}

export type RepositoryLabelsKey = ReturnType<typeof repositoryLabelsKey>;

/**
 * Where `queryKey`'s labels may be kept: the current server's and user's, read on github.com as
 * this Mac's own account or as a login named by the page, for a repository. Null otherwise,
 * and for named logins once named reads are known to be off here.
 */
function keptScope(queryKey: QueryKey): MacDurableKeep | null {
  const server = durableServerScope();
  const userId = getCurrentUserId();
  if (server === null || !userId) return null;
  const repository = queryKey.at(-1);
  if (typeof repository !== "string" || !repository.includes("/")) return null;
  const matches = (scope: readonly unknown[]) => {
    const expected = repositoryLabelsKey(scope, "", server).slice(0, -1);
    return (
      queryKey.length === expected.length + 1 &&
      expected.every((part, index) => queryKey[index] === part)
    );
  };
  const own = macReadScopeKey("github", GITHUB_HOST, userId);
  if (matches(own)) return { scope: own, restorable: true };
  // ["mac-read-as", user, "github", host, login] after the server.
  const login = queryKey[8];
  if (typeof login !== "string" || !LOGIN_PATTERN.test(login)) return null;
  if (namedReadsSettled() && !namedReadsAvailable()) return null;
  const named = macNamedReadScopeKey(GITHUB_HOST, login, userId);
  if (!matches(named)) return null;
  return { scope: named, restorable: true, reader: { host: GITHUB_HOST, login } };
}

// Typed by the generic key: the picker's query also takes other keys, which this never keeps.
const labelCache = createMacDurableReadCache<RepositoryLabelsSnapshot, SavedLabels, QueryKey>({
  prefix: STORAGE_PREFIX,
  buster: SCHEMA,
  maxAgeMs: MAX_AGE_MS,
  maxEntryChars: MAX_SAVED_LABEL_CHARS,
  totalBudgetChars: MAX_SAVED_LABELS_TOTAL_CHARS,
  dataSchema: savedLabelsSchema,
  keepOf: keptScope,
  viewerOf: (data) => data.viewer,
  saveOf: (snapshot) =>
    snapshot.restored || !snapshot.viewer
      ? null
      : { viewer: snapshot.viewer, labels: snapshot.labels },
  restoreOf: (data) => ({ ...data, restored: true }),
  // A list from this device paints at once; a stale one is read again behind it.
  refetchOnRestore: true,
  // Named logins' lists go once named reads are known to be off here: the flag rolled back, or
  // the shell, once it says what it supports, can't read as a named login.
  prunesOn: (prune) => {
    const stopNamed = subscribeNamedReads(prune);
    const stopSupport = subscribeDesktopReadPath(prune);
    return () => {
      stopNamed();
      stopSupport();
    };
  },
});

/** The persister for a label read under `queryKey`, where this device keeps that one. */
export function repositoryLabelsPersister(queryKey: QueryKey) {
  return keptScope(queryKey) ? labelCache.persister() : undefined;
}

/** Drop every kept label list another server, user or account read. */
export function pruneRepositoryLabelCache(): void {
  labelCache.prune();
}

/** Forget the persister and stop its watchers, so a test starts clean. */
/**
 * Follow account and scope changes from now on, so a picked login's sign-out drops its kept
 * lists though no picker opened this run. Called once Mac reads start; repeat calls do nothing.
 */
export function watchRepositoryLabelCache(): void {
  labelCache.watch();
}

export function resetRepositoryLabelCacheForTests(): void {
  labelCache.resetForTests();
}
