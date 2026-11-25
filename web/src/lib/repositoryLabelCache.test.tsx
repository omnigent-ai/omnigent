// The label picker's list, kept per repository: every PR of one repository shares one read, and
// on this Mac's own github.com account it's kept on this device too (a picked login's: see
// repositoryLabelCache.named.test.tsx), so it paints at once after a
// restart and is read again behind it. A kept list never shows for another account, workspace
// user or server, and a read that lands after the account changed is never kept.

import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";

const state = vi.hoisted(() => ({
  user: "user-1" as string | null,
  signedOut: false,
  server: "https://server-a.example.com" as string | null,
  identityListeners: new Set<() => void>(),
}));

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => state.user,
  getIdentityStatus: (): IdentityModule.IdentityStatus =>
    state.signedOut
      ? { kind: "signedOut" }
      : state.user
        ? { kind: "signedIn", userId: state.user }
        : { kind: "unknown" },
  subscribeIdentity: (listener: () => void) => {
    state.identityListeners.add(listener);
    return () => state.identityListeners.delete(listener);
  },
}));
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof HostModule>()),
  getOmnigentServerIdentity: () => state.server,
}));

import type { CodeHostPullRequest } from "@/lib/codeHosts";
import { fetchDesktopLabelOptions } from "@/lib/desktopPullRequests";
import {
  githubMacReadsPreference,
  GITHUB_MAC_READ_OPERATIONS,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import { macReadScopeKey } from "@/lib/macReadScope";
import { githubCooldowns } from "@/lib/githubCooldown";
import { bindMacReadScopes } from "@/lib/macReadScopeQueries";

import {
  MAX_SAVED_LABELS_TOTAL_CHARS,
  repositoryLabelsKey,
  repositoryLabelsPersister,
  resetRepositoryLabelCacheForTests,
  type RepositoryLabels,
  type RepositoryLabelsSnapshot,
} from "./repositoryLabelCache";

const STORAGE_PREFIX = "omnigent:repository-labels-";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

type LabelAnswer =
  | {
      ok: true;
      data: RepositoryLabels;
      identity: { host: string; login: string; generation: number };
    }
  | { ok: false; error: { code: string; message: string } };

/** A shell reading GitHub as `account`; each label read waits for the test to answer it. */
const shell = {
  account: { login: "alice", generation: 1 },
  labelReads: [] as { params: { repository: string }; answer: Deferred<LabelAnswer> }[],
  accountAnswer: null as (() => Promise<unknown>) | null,
};

function labels(...names: string[]): RepositoryLabels {
  return {
    object: "list",
    data: names.map((name) => ({ name, color: "ededed", description: null })),
    has_more: false,
  };
}

function answerAs(login: string, list: RepositoryLabels, generation = shell.account.generation) {
  return { ok: true, data: list, identity: { host: "github.com", login, generation } } as const;
}

function installShell() {
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    getDesktopFeatures: async () => ({
      codeHosts: [
        {
          id: "github",
          operations: [...GITHUB_MAC_READ_OPERATIONS, "pull_request_label_options"],
          accounts: true,
        },
      ],
    }),
    codeHostQuery: async (request: { operation: string; params: { repository: string } }) => {
      if (request.operation === "account_list") {
        return { ok: true, data: [{ login: shell.account.login, active: true, state: "success" }] };
      }
      const answer = deferred<LabelAnswer>();
      shell.labelReads.push({ params: request.params, answer });
      return answer.promise;
    },
    codeHostAccounts: {
      get: async () =>
        shell.accountAnswer
          ? shell.accountAnswer()
          : {
              ok: true,
              data: {
                intent: { mode: "automatic" },
                effective: { ...shell.account },
                accounts: [],
              },
            },
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
    },
  };
}

function pullRequest(repository: string, number: number): CodeHostPullRequest {
  return {
    provider: "github",
    host: "github.com",
    repository,
    number,
    url: `https://github.com/${repository}/pull/${number}`,
  };
}

/** The picker's label query for `target`, as the hook builds it for this Mac's own account. */
function useLabels(
  target: CodeHostPullRequest,
  scope: readonly unknown[] = macReadScopeKey("github", "github.com"),
) {
  const queryKey = repositoryLabelsKey(scope, target.repository);
  return useQuery<RepositoryLabelsSnapshot, Error, RepositoryLabels & { restored?: true }>({
    queryKey,
    queryFn: async () => {
      const { data, viewer } = await fetchDesktopLabelOptions<RepositoryLabels>(target);
      return { labels: data, viewer };
    },
    persister: repositoryLabelsPersister(queryKey),
    select: (snapshot) =>
      snapshot.restored ? { ...snapshot.labels, restored: true } : snapshot.labels,
    staleTime: 5 * 60_000,
  });
}

/** One app run: a fresh QueryClient and in-memory stores, as after a restart. */
function startApp() {
  resetDesktopReadPathForTests();
  resetRepositoryLabelCacheForTests();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // As the app does: an account change resets what was read under the old one.
  unbindScopes?.();
  unbindScopes = bindMacReadScopes(client);
  return client;
}

let unbindScopes: (() => void) | null = null;

function mountLabels(client: QueryClient, target: CodeHostPullRequest, scope?: readonly unknown[]) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const pending: boolean[] = [];
  const view = renderHook(
    () => {
      const query = useLabels(target, scope);
      pending.push(query.isPending);
      return query;
    },
    { wrapper },
  );
  return { ...view, pending };
}

const savedKeys = () =>
  Object.keys(window.localStorage).filter((key) => key.startsWith(STORAGE_PREFIX));

async function answerRead(index: number, answer: LabelAnswer) {
  await waitFor(() => expect(shell.labelReads.length).toBeGreaterThan(index));
  await act(async () => {
    shell.labelReads[index].answer.resolve(answer);
  });
}

const settle = () =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, 20);
  });

/** A first run that reads acme/app's labels as alice and keeps them. */
async function firstRunSaves(list = labels("bug", "automerge")) {
  const client = startApp();
  const view = mountLabels(client, pullRequest("acme/app", 1));
  await answerRead(0, answerAs("alice", list));
  await waitFor(() => expect(savedKeys()).toHaveLength(1));
  view.unmount();
  client.clear();
  shell.labelReads = [];
}

beforeEach(() => {
  window.localStorage.clear();
  githubMacReadsPreference.write("on");
  state.user = "user-1";
  state.signedOut = false;
  state.server = "https://server-a.example.com";
  shell.account = { login: "alice", generation: 1 };
  shell.labelReads = [];
  shell.accountAnswer = null;
  installShell();
});

afterEach(() => {
  // A rate limit a test answered keeps the host waiting, which the next test mustn't inherit.
  githubCooldowns.reset();
  unbindScopes?.();
  unbindScopes = null;
  resetDesktopReadPathForTests();
  resetRepositoryLabelCacheForTests();
  state.identityListeners.clear();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  githubMacReadsPreference.write("automatic");
  vi.restoreAllMocks();
});

describe("per repository", () => {
  it("shares one read across the pull requests of a repository, and not across repositories", async () => {
    const client = startApp();
    const first = mountLabels(client, pullRequest("acme/app", 1));
    await answerRead(0, answerAs("alice", labels("bug")));
    await waitFor(() => expect(first.result.current.data?.data).toHaveLength(1));

    const second = mountLabels(client, pullRequest("Acme/App", 2));
    expect(second.pending[0]).toBe(false);
    expect(second.result.current.data?.data.map((label) => label.name)).toEqual(["bug"]);
    await settle();
    expect(shell.labelReads).toHaveLength(1);

    mountLabels(client, pullRequest("acme/other", 3));
    await waitFor(() => expect(shell.labelReads).toHaveLength(2));
    expect(shell.labelReads[1].params.repository).toBe("acme/other");
  });
});

describe("across a restart", () => {
  it("paints a fresh kept list without reading GitHub", async () => {
    await firstRunSaves();
    const view = mountLabels(startApp(), pullRequest("acme/app", 7));
    await waitFor(() => expect(view.result.current.data?.restored).toBe(true));
    await settle();
    expect(shell.labelReads).toHaveLength(0);
  });

  it("never shows a list kept as another account, and forgets it", async () => {
    await firstRunSaves();
    shell.account = { login: "bob", generation: 1 };
    const view = mountLabels(startApp(), pullRequest("acme/app", 1));
    await waitFor(() => expect(shell.labelReads).toHaveLength(1));
    expect(view.result.current.data).toBeUndefined();
    expect(savedKeys()).toEqual([]);
  });
});

describe("this device's storage", () => {
  /** A list of about `chars` characters once saved. */
  function largeList(chars: number, tag: string): RepositoryLabels {
    const description = "d".repeat(200);
    const count = Math.ceil(chars / 240);
    return {
      object: "list",
      data: Array.from({ length: count }, (_, index) => ({
        name: `${tag}-${index}`,
        color: "ededed",
        description,
      })),
      has_more: false,
    };
  }

  async function readRepository(client: QueryClient, repository: string, list: RepositoryLabels) {
    const index = shell.labelReads.length;
    mountLabels(client, pullRequest(repository, 1));
    await answerRead(index, answerAs("alice", list));
    await settle();
  }

  const savedRepositories = () =>
    savedKeys().map((key) => JSON.parse(window.localStorage.getItem(key)!).queryKey.at(-1));

  it("keeps every repository's labels within one budget, dropping the least recently used", async () => {
    const client = startApp();
    const each = Math.floor(MAX_SAVED_LABELS_TOTAL_CHARS / 3);
    await readRepository(client, "acme/one", largeList(each, "one"));
    await readRepository(client, "acme/two", largeList(each, "two"));
    await readRepository(client, "acme/three", largeList(each, "three"));
    expect(savedRepositories().sort()).toEqual(["acme/three", "acme/two"]);
    const total = savedKeys().reduce(
      (sum, key) => sum + window.localStorage.getItem(key)!.length,
      0,
    );
    expect(total).toBeLessThanOrEqual(MAX_SAVED_LABELS_TOTAL_CHARS);
  });
});
