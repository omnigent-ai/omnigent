// A pull request's overview read on this Mac's own github.com account, kept on this device: it
// paints at once after a restart, in any session that opens the PR, and is read again behind it.
// Nothing kept shows for another account, workspace user or server, or once the flag is off.

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
import type { PullRequestInfo } from "@/newPullRequestPage/hooks/usePullRequests";
import type { CodeHostPullRequest } from "@/lib/codeHosts";
import { fetchDesktopPullRequestInfo } from "@/lib/desktopPullRequests";
import {
  githubMacReadsPreference,
  GITHUB_MAC_READ_OPERATIONS,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import { githubCooldowns } from "@/lib/githubCooldown";
import { macReadScopeKey } from "@/lib/macReadScope";
import { bindMacReadScopes } from "@/lib/macReadScopeQueries";

import {
  PULL_REQUEST_SAVED_DETAILS_FLAG,
  pullRequestDetailsPersister,
  resetPullRequestDetailsCacheForTests,
  watchPullRequestDetailsCache,
} from "./pullRequestDetailsCache";
import { EXAMPLE_SPOKE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// GitSpoke stand-in hosts are recognized by name, as a build that registers GitSpoke does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_SPOKE_HOST] });

const STORAGE_PREFIX = "omnigent:pull-request-details-";
const prUrl = (number: number) => `https://github.com/acme/app/pull/${number}`;
const URL_ONE = prUrl(1);

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

type ShellAnswer =
  | { ok: true; data: unknown; identity: { host: string; login: string; generation: number } }
  | { ok: false; error: { code: string; message: string } };

interface ShellRead {
  operation: string;
  params: Record<string, unknown>;
  answer: Deferred<ShellAnswer>;
}

/** A shell reading GitHub as `account`; reads answer at once unless `hold` keeps them waiting. */
const shell = {
  account: { login: "alice", generation: 1 },
  hold: false,
  reads: [] as ShellRead[],
  /** What the next overview read answers, by PR URL. */
  overviews: new Map<string, unknown>(),
  flag: undefined as boolean | undefined,
  flagListeners: new Set<(value: unknown) => void>(),
};

function overviewOf(url: string, title = "Fix the thing", body = "") {
  const number = Number(url.split("/").at(-1));
  return {
    repository: url.split("/").slice(3, 5).join("/"),
    pr: {
      number,
      url,
      title,
      body,
      state: "open",
      is_draft: false,
      head_sha: "b".repeat(40),
      base_sha: "a".repeat(40),
      head_ref: "feature",
      base_ref: "main",
    },
  };
}

/** GitHub's answer to an overview read with `params`, as the shell passes them on. */
function answerFor(params: Record<string, unknown>): ShellAnswer {
  const url = `https://github.com/${params.repository as string}/pull/${params.number as number}`;
  const data = shell.overviews.get(url) ?? overviewOf(url);
  return { ok: true, data, identity: { host: "github.com", ...shell.account } };
}

function installShell() {
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    getDesktopFeatures: async () => ({
      codeHosts: [{ id: "github", operations: [...GITHUB_MAC_READ_OPERATIONS], accounts: true }],
      featureFlags: { names: shell.flag === undefined ? [] : [PULL_REQUEST_SAVED_DETAILS_FLAG] },
    }),
    featureFlags: {
      subscribe: (_name: string, _options: unknown, listener: (value: unknown) => void) => {
        shell.flagListeners.add(listener);
        listener(shell.flag);
        return () => shell.flagListeners.delete(listener);
      },
    },
    codeHostQuery: async (request: { operation: string; params: Record<string, unknown> }) => {
      if (request.operation === "account_list") {
        return { ok: true, data: [{ login: shell.account.login, active: true, state: "success" }] };
      }
      const answer = deferred<ShellAnswer>();
      shell.reads.push({ ...request, answer });
      if (!shell.hold) answer.resolve(answerFor(request.params));
      return answer.promise;
    },
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: { intent: { mode: "automatic" }, effective: { ...shell.account }, accounts: [] },
      }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
    },
  };
}

function pullRequest(url: string): CodeHostPullRequest {
  const [, , , owner, name, , number] = url.split("/");
  return {
    provider: "github",
    host: "github.com",
    repository: `${owner}/${name}`,
    number: Number(number),
    url,
  };
}

const scope = () => macReadScopeKey("github", "github.com");
const overviewKey = (session: string, url: string) => ["github-info", session, url, ...scope()];

/** A view's overview query for `url` in `session`, as the PR page builds it on this Mac. */
function useOverview(session: string, url: string) {
  const queryKey = overviewKey(session, url);
  return useQuery<PullRequestInfo>({
    queryKey,
    queryFn: () => fetchDesktopPullRequestInfo(pullRequest(url), Promise.resolve([])),
    persister: pullRequestDetailsPersister<PullRequestInfo>(queryKey),
    staleTime: 30_000,
  });
}

let unbindScopes: (() => void) | null = null;

/** One app run: a fresh QueryClient and in-memory stores, as after a restart. */
function startApp() {
  resetDesktopReadPathForTests();
  resetPullRequestDetailsCacheForTests();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  unbindScopes?.();
  unbindScopes = bindMacReadScopes(client);
  return client;
}

function mount<T>(client: QueryClient, hook: () => T) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(hook, { wrapper });
}

const savedKeys = () =>
  Object.keys(window.localStorage).filter(
    (key) => key.startsWith(STORAGE_PREFIX) && !key.endsWith(":used"),
  );
const savedEntries = () => savedKeys().map((key) => JSON.parse(window.localStorage.getItem(key)!));
const keyOf = (url: string) =>
  savedKeys().find((key) => JSON.parse(window.localStorage.getItem(key)!).queryKey.includes(url));

const settle = () =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, 20);
  });

/** A first run that reads the overviews of `urls` (PR 1's by default) as alice and keeps them. */
async function firstRunSaves(...urls: string[]) {
  const shown = urls.length > 0 ? urls : [URL_ONE];
  const client = startApp();
  const view = mount(client, () => shown.map((url) => useOverview("session-1", url)));
  await waitFor(() => expect(savedKeys()).toHaveLength(shown.length));
  view.unmount();
  client.clear();
  shell.reads = [];
}

/** Ages every kept entry by `ms`, as a restart that much later. */
function ageEntries(ms: number) {
  for (const key of savedKeys()) {
    const entry = JSON.parse(window.localStorage.getItem(key)!);
    entry.state.dataUpdatedAt -= ms;
    window.localStorage.setItem(key, JSON.stringify(entry));
  }
}

beforeEach(() => {
  window.localStorage.clear();
  githubMacReadsPreference.write("on");
  state.user = "user-1";
  state.server = "https://server-a.example.com";
  shell.account = { login: "alice", generation: 1 };
  shell.hold = false;
  shell.reads = [];
  shell.overviews.clear();
  shell.flag = undefined;
  shell.flagListeners.clear();
  installShell();
});

afterEach(() => {
  githubCooldowns.reset();
  unbindScopes?.();
  unbindScopes = null;
  resetDesktopReadPathForTests();
  resetPullRequestDetailsCacheForTests();
  state.identityListeners.clear();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  githubMacReadsPreference.write("automatic");
  vi.restoreAllMocks();
});

describe("which reads are kept", () => {
  it("keeps no host read, other host or malformed URL, and keeps review conversations", () => {
    // The session's host read has no Mac scope at the end of its key.
    expect(pullRequestDetailsPersister(["github-info", "session-1", URL_ONE])).toBeUndefined();
    expect(
      pullRequestDetailsPersister([
        "github-info",
        "session-1",
        "https://2.spoke.example.com/acme/app/pull/1",
        ...macReadScopeKey("gitspoke", "2.spoke.example.com"),
      ]),
    ).toBeUndefined();
    expect(
      pullRequestDetailsPersister(overviewKey("session-1", "https://github.com/acme/app/issues/1")),
    ).toBeUndefined();
    // Review conversations are kept under the revision they were read at.
    expect(
      pullRequestDetailsPersister([
        "github-review-threads",
        "session-1",
        URL_ONE,
        "",
        `${"a".repeat(40)}:${"b".repeat(40)}`,
        ...scope(),
      ]),
    ).toBeDefined();
    expect(pullRequestDetailsPersister(overviewKey("session-1", URL_ONE))).toBeDefined();
  });
});

describe("across a restart", () => {
  it("paints the kept PR at once in another session, then reads GitHub again behind it", async () => {
    await firstRunSaves();
    ageEntries(10 * 60_000);
    shell.hold = true;
    const view = mount(startApp(), () => useOverview("session-2", URL_ONE));
    await waitFor(() => expect(view.result.current.data?.restored).toBe(true));
    expect(view.result.current.data?.pr?.title).toBe("Fix the thing");
    expect(view.result.current.data?.prs).toEqual([]);
    expect(view.result.current.data?.tracking_available).toBe(false);

    await waitFor(() => expect(shell.reads).toHaveLength(1));
    shell.overviews.set(URL_ONE, overviewOf(URL_ONE, "Fix the thing properly"));
    await act(async () => shell.reads[0].answer.resolve(answerFor(shell.reads[0].params)));
    await waitFor(() => expect(view.result.current.data?.restored).toBeUndefined());
    expect(view.result.current.data?.pr?.title).toBe("Fix the thing properly");
  });
});

describe("accounts, users and the flag", () => {
  it("never shows an overview kept as another account, and forgets it", async () => {
    await firstRunSaves();
    shell.account = { login: "bob", generation: 1 };
    shell.hold = true;
    const view = mount(startApp(), () => useOverview("session-1", URL_ONE));
    await waitFor(() => expect(shell.reads).toHaveLength(1));
    expect(view.result.current.data).toBeUndefined();
    expect(savedEntries().filter((entry) => entry.state.data.viewer === "alice")).toEqual([]);
  });

  it("drops another workspace user's overviews on a switch", async () => {
    await firstRunSaves();
    watchPullRequestDetailsCache();
    state.user = "user-2";
    for (const listener of state.identityListeners) listener();
    expect(savedKeys()).toEqual([]);
  });

  it("keeps nothing while the flag is off, and drops what was kept when it turns off", async () => {
    shell.flag = true;
    installShell();
    await firstRunSaves();
    const client = startApp();
    watchPullRequestDetailsCache();
    await act(settle);
    expect(savedKeys()).toHaveLength(1);

    shell.flag = false;
    act(() => {
      for (const listener of shell.flagListeners) listener(false);
    });
    expect(savedKeys()).toEqual([]);
    mount(client, () => useOverview("session-1", prUrl(2)));
    await waitFor(() => expect(shell.reads).toHaveLength(1));
    await act(settle);
    expect(savedKeys()).toEqual([]);
  });
});

describe("this device's storage", () => {
  async function readOverview(client: QueryClient, number: number, body: string) {
    shell.overviews.set(prUrl(number), overviewOf(prUrl(number), "Fix the thing", body));
    const queryKey = overviewKey("session-1", prUrl(number));
    if (client.getQueryData(queryKey)) {
      // Read again, as a refresh does.
      await act(async () => client.refetchQueries({ queryKey, exact: true, type: "all" }));
    } else {
      const view = mount(client, () => useOverview("session-1", prUrl(number)));
      await waitFor(() => expect(view.result.current.data).toBeDefined());
      view.unmount();
    }
    await act(settle);
  }

  it("makes room when the device is full by dropping its own oldest entry, never other data", async () => {
    window.localStorage.setItem("unrelated", "kept");
    const client = startApp();
    await readOverview(client, 1, "first");
    await readOverview(client, 2, "second");
    const setItem = Storage.prototype.setItem;
    let refused = false;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) {
      if (key.startsWith(STORAGE_PREFIX) && !key.endsWith(":used") && !refused) {
        refused = true;
        throw new DOMException("Full", "QuotaExceededError");
      }
      setItem.call(this, key, value);
    });
    await readOverview(client, 3, "third");
    expect(keyOf(prUrl(1))).toBeUndefined();
    expect(keyOf(prUrl(2))).toBeDefined();
    expect(keyOf(prUrl(3))).toBeDefined();
    expect(window.localStorage.getItem("unrelated")).toBe("kept");
  });
});
