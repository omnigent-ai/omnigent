// Labels read as a login the Pull Requests page picked are kept on this device too, but restore
// only once a fresh read this run proved that exact login: main's named identity and GitHub's
// viewer both. Leaving the login keeps them; its sign-out or change drops every one of its
// entries. Another login, workspace user or server never sees them, and a restore makes no writer.

import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";

const state = vi.hoisted(() => ({
  user: "user-1" as string | null,
  server: "https://server-a.example.com" as string | null,
  identityListeners: new Set<() => void>(),
}));

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => state.user,
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
import { fetchDesktopLabelOptions, readOnThisMac } from "@/lib/desktopPullRequests";
import {
  githubMacReadsPreference,
  GITHUB_MAC_READ_OPERATIONS,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import {
  leaveNamedReader,
  namedReadsAvailable,
  PULL_REQUESTS_PAGE_ACCOUNTS_FLAG,
  resetMacNamedReadsForTests,
} from "@/lib/macNamedReads";
import { macNamedReadScopeKey, resetMacReadScopesForTests } from "@/lib/macReadScope";
import { bindMacReadScopes } from "@/lib/macReadScopeQueries";

import {
  repositoryLabelsKey,
  repositoryLabelsPersister,
  resetRepositoryLabelCacheForTests,
  watchRepositoryLabelCache,
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

interface Read {
  operation: string;
  macLogin: string | undefined;
  repository: string | undefined;
  answer: Deferred<unknown>;
}

/** A shell offering named reads and writes; each read waits for the test to answer it. */
const shell = {
  reads: [] as Read[],
  flag: true,
  /** An older shell: no named reads or writes at all. */
  older: false,
};

function labels(...names: string[]): RepositoryLabels {
  return {
    object: "list",
    data: names.map((name) => ({ name, color: "ededed", description: null })),
    has_more: false,
  };
}

/** GitHub's answer read as `login` by name, main and GitHub both saying so. */
function namedAnswer(login: string, data: unknown, viewer = login) {
  return {
    ok: true,
    data,
    identity: { host: "github.com", login, generation: 0, named: true },
    provenance: { version: 1, provider: "github", host: "github.com", viewer },
  };
}

function installShell() {
  const named = !shell.older;
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    getDesktopFeatures: async () => ({
      codeHosts: [
        {
          id: "github",
          operations: [
            ...GITHUB_MAC_READ_OPERATIONS,
            "pull_request_inbox",
            "pull_request_label_options",
          ],
          accounts: true,
          ...(named ? { readAs: 1, writeAs: 1 } : {}),
        },
      ],
      featureFlags: { names: [PULL_REQUESTS_PAGE_ACCOUNTS_FLAG] },
    }),
    featureFlags: {
      subscribe: (name: string, _options: unknown, callback: (value: unknown) => void) => {
        if (name === PULL_REQUESTS_PAGE_ACCOUNTS_FLAG) queueMicrotask(() => callback(shell.flag));
        return () => {};
      },
    },
    codeHostQuery: (request: {
      operation: string;
      macLogin?: string;
      params?: { repository?: string };
    }) => {
      if (request.operation === "account_list") {
        return Promise.resolve({
          ok: true,
          data: [{ login: "alice", active: true, state: "success" }],
        });
      }
      const pending = deferred<unknown>();
      shell.reads.push({
        operation: request.operation,
        macLogin: request.macLogin,
        repository: request.params?.repository,
        answer: pending,
      });
      return pending.promise;
    },
    codeHostWrite: async () => ({ ok: false }),
    codeHostWriteCancel: () => {},
    ...(named ? { codeHostWriteAs: async () => ({ ok: false }) } : {}),
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: {
          intent: { mode: "automatic" },
          effective: { login: "alice", generation: 1 },
          // The inventory still says bob is signed in; that alone never restores anything.
          accounts: [
            { login: "alice", active: true, state: "success" },
            { login: "bob", active: false, state: "success" },
            { login: "carol", active: false, state: "success" },
          ],
        },
      }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
      ...(named ? { onNamedChanged: () => () => {} } : {}),
    },
  };
}

const reader = (login: string) => ({ host: "github.com", login });

function pullRequest(repository: string, readAs: string): CodeHostPullRequest {
  return {
    provider: "github",
    host: "github.com",
    repository,
    number: 1,
    url: `https://github.com/${repository}/pull/1`,
    readAs,
  } as CodeHostPullRequest;
}

const labelsKey = (login: string, repository: string) =>
  repositoryLabelsKey(macNamedReadScopeKey("github.com", login), repository);

/** The picker's label query for `repository` as `login`, as the hook builds it. */
function useLabels(repository: string, login: string) {
  const queryKey = labelsKey(login, repository);
  return useQuery<RepositoryLabelsSnapshot, Error, RepositoryLabels & { restored?: true }>({
    queryKey,
    queryFn: async () => {
      const { data, viewer } = await fetchDesktopLabelOptions<RepositoryLabels>(
        pullRequest(repository, login),
      );
      return { labels: data, viewer };
    },
    persister: repositoryLabelsPersister(queryKey),
    select: (snapshot) =>
      snapshot.restored ? { ...snapshot.labels, restored: true } : snapshot.labels,
    staleTime: 5 * 60_000,
  });
}

let unbindScopes: (() => void) | null = null;

/** One app run: a fresh QueryClient and in-memory stores, as after a restart. */
async function startApp() {
  // The shell is there before the app starts, as Electron's preload puts it.
  installShell();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  resetRepositoryLabelCacheForTests();
  shell.reads = [];
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  unbindScopes?.();
  // As the app does once its Mac reads start.
  unbindScopes = bindMacReadScopes(client);
  watchRepositoryLabelCache();
  if (!shell.older && shell.flag) await waitFor(() => expect(namedReadsAvailable()).toBe(true));
  return client;
}

function mountLabels(client: QueryClient, repository: string, login: string) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(() => useLabels(repository, login), { wrapper });
}

async function answer(index: number, value: unknown) {
  await waitFor(() => expect(shell.reads.length).toBeGreaterThan(index));
  await act(async () => {
    shell.reads[index]!.answer.resolve(value);
  });
}

/** The page's own read as `login` (its inbox), answered as main and GitHub prove it. */
async function prove(login: string, viewer = login) {
  const index = shell.reads.length;
  const read = readOnThisMac(pullRequest("acme/app", login), "pull_request_inbox", {});
  await answer(index, namedAnswer(login, {}, viewer));
  await read.catch(() => undefined);
}

const savedKeys = () =>
  Object.keys(window.localStorage).filter((key) => key.startsWith(STORAGE_PREFIX));

const settle = () =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, 20);
  });

/** The shell's label read for `repository`, once it's sent. */
async function labelReadFor(repository: string): Promise<number> {
  let index = -1;
  await waitFor(() => {
    index = shell.reads.findIndex(
      (read) => read.operation === "pull_request_label_options" && read.repository === repository,
    );
    expect(index).toBeGreaterThanOrEqual(0);
  });
  return index;
}

/** A first run that reads and keeps one repository's labels as `login`, after proving it. */
async function saveLabels(client: QueryClient, login: string, repository: string) {
  const view = mountLabels(client, repository, login);
  await answer(
    await labelReadFor(repository),
    namedAnswer(login, labels(`${login}-${repository}`)),
  );
  await waitFor(() =>
    expect(view.result.current.data?.data[0]?.name).toBe(`${login}-${repository}`),
  );
  view.unmount();
}

/** What a first run read and kept as `login`, one entry per repository. */
async function firstRunSaves(login: string, repositories: string[]) {
  const client = await startApp();
  await prove(login);
  for (const repository of repositories) {
    // oxlint-disable-next-line no-await-in-loop -- each repository's read is answered in turn
    await saveLabels(client, login, repository);
  }
  await settle();
  client.clear();
}

const labelReads = () =>
  shell.reads.filter((read) => read.operation === "pull_request_label_options");

beforeEach(() => {
  window.localStorage.clear();
  githubMacReadsPreference.write("on");
  state.user = "user-1";
  state.server = "https://server-a.example.com";
  shell.reads = [];
  shell.flag = true;
  shell.older = false;
});

afterEach(() => {
  unbindScopes?.();
  unbindScopes = null;
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  resetRepositoryLabelCacheForTests();
  state.identityListeners.clear();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  githubMacReadsPreference.write("automatic");
});

describe("across a restart", () => {
  it("keeps a picked login's labels, and restores them once a fresh read proves the login", async () => {
    await firstRunSaves("bob", ["acme/app"]);
    expect(savedKeys()).toHaveLength(1);

    const client = await startApp();
    const view = mountLabels(client, "acme/app", "bob");
    await settle();
    // No proof yet: nothing paints, and the inventory saying bob is signed in doesn't count.
    expect(view.result.current.data).toBeUndefined();
    expect(labelReads()).toHaveLength(0);
    await prove("bob");
    await waitFor(() => expect(view.result.current.data?.restored).toBe(true));
    expect(view.result.current.data?.data.map((label) => label.name)).toEqual(["bob-acme/app"]);
    // Fresh: GitHub isn't read again behind it.
    expect(labelReads()).toHaveLength(0);
  });
});

describe("switching logins (A→B→A)", () => {
  it("never keeps a label read that lands after the page left the login", async () => {
    const client = await startApp();
    await prove("bob");
    mountLabels(client, "acme/app", "bob");
    await waitFor(() => expect(labelReads()).toHaveLength(1));
    leaveNamedReader(reader("bob"));
    await answer(shell.reads.indexOf(labelReads()[0]!), namedAnswer("bob", labels("late")));
    await settle();
    expect(savedKeys()).toHaveLength(0);
  });
});

describe("dropping a login's entries", () => {
  it("keeps nothing named on an older shell, and never restores there", async () => {
    await firstRunSaves("bob", ["acme/app"]);
    shell.older = true;
    const client = await startApp();
    await settle();
    expect(namedReadsAvailable()).toBe(false);
    mountLabels(client, "acme/app", "bob");
    await settle();
    expect(savedKeys()).toHaveLength(0);
  });
});
