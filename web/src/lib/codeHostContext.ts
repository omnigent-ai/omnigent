// A session's repository context, read on the desktop from the session's own
// machine: the checked-out branch, its commit, where it pushes, and the
// remotes, as host/owner/repo. Main asks the session's workspace helper, so
// the page names only the session and never sees a path, URL or config value.
// Browsers, older shells and Macs without the helper answer `unsupported`, and
// callers keep using the session's host.

import { useQuery } from "@tanstack/react-query";

import { askShell, shellServesRepositoryContext } from "@/lib/workspaceFastTrack";

export interface CodeHostRemote {
  name: string;
  host: string;
  owner: string;
  repo: string;
}

/** Where the branch pushes, and the repository it pushes to. */
export interface CodeHostPushTarget {
  remote_name: string;
  /** The destination branch on the remote. */
  branch: string;
  host: string;
  owner: string;
  repo: string;
  /** The local tracking ref's commit, when it describes the push repository. */
  tracking_sha: string | null;
}

export type CodeHostContext =
  | {
      status: "ok";
      /** Changes whenever any fact below, the route or the helper does. */
      context_key: string;
      /** Null when HEAD is detached. */
      branch: string | null;
      /** Null in a repository without commits. */
      head_sha: string | null;
      /** Take the push repository from here, never from `remotes` by name. */
      push: CodeHostPushTarget | null;
      /** Why there's no push target although the branch may push somewhere. */
      push_unresolved: "refspec" | "push_url" | "push_default" | null;
      remotes: CodeHostRemote[];
      unsupported_remote_count: number;
      truncated_remotes: boolean;
    }
  | {
      /**
       * `pending`: ask again shortly. `unsupported`: use the session's host.
       * The rest describe the repository; the host path may still answer.
       */
      status: "pending" | "unsupported" | "unsupported_config" | "not_a_git_repo" | "failed";
    };

const NOT_OK = new Set(["pending", "unsupported_config", "not_a_git_repo", "failed"]);
const PUSH_UNRESOLVED = new Set(["refspec", "push_url", "push_default"]);
const COMMIT = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/;
const UNSUPPORTED: CodeHostContext = { status: "unsupported" };

const isText = (value: unknown): value is string => typeof value === "string" && value !== "";
const isRepository = (value: Record<string, unknown>) =>
  isText(value.host) && isText(value.owner) && isText(value.repo);

/** The shell's answer as a context, or `unsupported` for anything malformed or unknown. */
export function parseCodeHostContext(body: unknown): CodeHostContext {
  if (!body || typeof body !== "object") return UNSUPPORTED;
  const answer = body as Record<string, unknown>;
  if (answer.status !== "ok") {
    return NOT_OK.has(String(answer.status))
      ? { status: answer.status as Exclude<CodeHostContext["status"], "ok"> }
      : UNSUPPORTED;
  }
  const nullableText = (value: unknown) => (value === null || isText(value) ? value : undefined);
  const branch = nullableText(answer.branch);
  const headSha = nullableText(answer.head_sha);
  if (!isText(answer.context_key) || branch === undefined || headSha === undefined) {
    return UNSUPPORTED;
  }
  if (headSha !== null && !COMMIT.test(headSha)) return UNSUPPORTED;
  let push: CodeHostPushTarget | null = null;
  if (answer.push !== null) {
    const target = answer.push as Record<string, unknown> | undefined;
    if (!target || typeof target !== "object" || !isRepository(target)) return UNSUPPORTED;
    const trackingSha = target.tracking_sha;
    if (!isText(target.remote_name) || !isText(target.branch)) return UNSUPPORTED;
    if (trackingSha !== null && !(isText(trackingSha) && COMMIT.test(trackingSha))) {
      return UNSUPPORTED;
    }
    push = {
      remote_name: target.remote_name,
      branch: target.branch,
      host: target.host as string,
      owner: target.owner as string,
      repo: target.repo as string,
      tracking_sha: trackingSha,
    };
  }
  if (!Array.isArray(answer.remotes)) return UNSUPPORTED;
  const remotes: CodeHostRemote[] = [];
  for (const remote of answer.remotes as Record<string, unknown>[]) {
    if (!remote || typeof remote !== "object" || !isText(remote.name) || !isRepository(remote)) {
      return UNSUPPORTED;
    }
    remotes.push({
      name: remote.name,
      host: remote.host as string,
      owner: remote.owner as string,
      repo: remote.repo as string,
    });
  }
  const pushUnresolved = PUSH_UNRESOLVED.has(String(answer.push_unresolved))
    ? (answer.push_unresolved as "refspec" | "push_url" | "push_default")
    : null;
  return {
    status: "ok",
    context_key: answer.context_key,
    branch,
    head_sha: headSha,
    push,
    push_unresolved: push ? null : pushUnresolved,
    remotes,
    unsupported_remote_count: Number.isInteger(answer.unsupported_remote_count)
      ? (answer.unsupported_remote_count as number)
      : 0,
    truncated_remotes: answer.truncated_remotes === true,
  };
}

/**
 * Asks the desktop shell for the session's repository context. Never falls back
 * to HTTPS: anything the shell can't answer is `unsupported`, and a helper
 * still starting is `pending`. Rejects as soon as `signal` aborts, whatever the
 * shell does, and asks a shell that can to stop the work in main.
 */
export async function fetchCodeHostContext(
  sessionId: string,
  { signal }: { signal?: AbortSignal } = {},
): Promise<CodeHostContext> {
  signal?.throwIfAborted();
  if (!(await shellServesRepositoryContext())) return UNSUPPORTED;
  signal?.throwIfAborted();
  const result = await askShell(
    {
      url: `/v1/sessions/${encodeURIComponent(sessionId)}/resources/environments/default/repository-context`,
      desktopOnly: true,
    },
    signal,
  );
  signal?.throwIfAborted();
  if (!result.ok) return result.reason === "verifying" ? { status: "pending" } : UNSUPPORTED;
  // The bridge serialized the body itself, so it's always valid JSON.
  return parseCodeHostContext(JSON.parse(result.bodyText) as unknown);
}

/** Each session's newest read, and the context it accepted. */
const latestRead = new Map<string, number>();
const accepted = new Map<string, CodeHostContext>();

/**
 * Reads the session's context, accepting only the newest read's answer: a read
 * that a newer one overtook gives back what that newer one accepted, so a late
 * answer can never replace a fresh one. A newer answer always replaces the
 * prior context, whatever its key: a changed key is the normal result of a
 * commit, branch switch or remote change.
 */
export async function readCodeHostContext(
  sessionId: string,
  { signal }: { signal?: AbortSignal } = {},
): Promise<CodeHostContext> {
  const read = (latestRead.get(sessionId) ?? 0) + 1;
  latestRead.set(sessionId, read);
  // An aborted read rejects, and its answer is never accepted.
  const context = await fetchCodeHostContext(sessionId, { signal });
  signal?.throwIfAborted();
  if (latestRead.get(sessionId) !== read) return accepted.get(sessionId) ?? { status: "pending" };
  accepted.set(sessionId, context);
  return context;
}

/**
 * Whether `contextKey` is still the session's accepted context. Work derived from
 * a context, such as finding the branch's pull request, applies its result only
 * while this holds.
 */
export function isCurrentCodeHostContext(sessionId: string, contextKey: string): boolean {
  const current = accepted.get(sessionId);
  return current?.status === "ok" && current.context_key === contextKey;
}

/** For tests: forget every session's reads. */
export function resetCodeHostContextReads(): void {
  latestRead.clear();
  accepted.clear();
}

/** How soon to ask again after a `pending` answer. */
export const PENDING_RETRY_MS = 1_000;

export const codeHostContextQueryKey = (sessionId: string | undefined) =>
  ["code-host-context", sessionId] as const;

/**
 * The session's repository context, re-read on each refetch. A session switch
 * changes the key, which cancels the old session's read in main; it can never
 * answer for the new session.
 */
export function useCodeHostContext(
  sessionId: string | undefined,
  { enabled = true, refetchInterval }: { enabled?: boolean; refetchInterval?: number | false } = {},
) {
  return useQuery({
    queryKey: codeHostContextQueryKey(sessionId),
    queryFn: ({ signal }) => readCodeHostContext(sessionId!, { signal }),
    enabled: enabled && !!sessionId,
    // A helper still starting answers `pending`: ask again until it settles.
    refetchInterval: (query) =>
      query.state.data?.status === "pending" ? PENDING_RETRY_MS : (refetchInterval ?? false),
    staleTime: 0,
    retry: false,
  });
}

/** What the workspace's remotes say about where a repository is hosted. */
export interface RepositoryRemoteHosts {
  hosts: string[];
  /** False when the shell listed only some remotes, so another may name it elsewhere. */
  complete: boolean;
}

/**
 * Hosts of the workspace's remotes for `repository` (`owner/repo`, any case):
 * the push target's alone when it names that repository, else every listed
 * remote's that does. Null when the context isn't read.
 */
export function repositoryRemoteHosts(
  context: CodeHostContext | undefined,
  repository: string | null,
): RepositoryRemoteHosts | null {
  if (context?.status !== "ok" || !repository) return null;
  const wanted = repository.toLowerCase();
  const names = (remote: { owner: string; repo: string }) =>
    `${remote.owner}/${remote.repo}`.toLowerCase() === wanted;
  if (context.push && names(context.push)) {
    return { hosts: [context.push.host.toLowerCase()], complete: true };
  }
  const hosts = new Set(context.remotes.filter(names).map((remote) => remote.host.toLowerCase()));
  return { hosts: [...hosts], complete: !context.truncated_remotes };
}
