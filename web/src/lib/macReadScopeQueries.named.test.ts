import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";
import { namedPullRequestWritten } from "@/lib/macNamedReads";
import {
  macNamedReadScopeKey,
  macReadScopeKey,
  refuseMacReadScope,
  resetMacReadScopesForTests,
} from "@/lib/macReadScope";
import { bindMacReadScopes } from "./macReadScopeQueries";

const { currentUser } = vi.hoisted(() => ({ currentUser: { id: "user-1" as string | null } }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => currentUser.id,
}));

const PR = "https://github.com/o/r/pull/1";
const named = () => macNamedReadScopeKey("github.com", "bob", "user-1");
const hostWide = () => macReadScopeKey("github", "github.com", "user-1");
const newClient = () => new QueryClient({ defaultOptions: { queries: { retry: false } } });
const unbinds: (() => void)[] = [];

function bound() {
  const client = newClient();
  unbinds.push(bindMacReadScopes(client));
  return client;
}

/** A query an observer keeps shown, until the test ends. */
function observe(
  client: QueryClient,
  queryKey: readonly unknown[],
  queryFn: () => Promise<unknown>,
) {
  const observer = new QueryObserver(client, { queryKey, queryFn, staleTime: Infinity });
  const unsubscribe = observer.subscribe(() => {});
  unbinds.push(unsubscribe);
  return observer;
}

const tick = () =>
  new Promise((resolve) => {
    setTimeout(resolve, 0);
  });

afterEach(() => {
  for (const unbind of unbinds.splice(0)) unbind();
  resetMacReadScopesForTests();
  currentUser.id = "user-1";
});

describe("a named scope main refused", () => {
  it("keeps a failure's error and drops its data, reading nothing again", async () => {
    const client = bound();
    const key = ["github-info", "page", PR, ...named()];
    const queryFn = vi.fn().mockRejectedValue(new Error("@bob isn't signed in"));
    client.setQueryData(key, "as bob");
    const observer = observe(client, key, queryFn);
    await observer.refetch();
    expect(client.getQueryState(key)?.status).toBe("error");
    expect(client.getQueryData(key)).toBe("as bob");

    refuseMacReadScope(named());
    await tick();

    expect(client.getQueryData(key)).toBeUndefined();
    expect(client.getQueryState(key)?.error?.message).toBe("@bob isn't signed in");
    // The refusal itself reads nothing again.
    expect(queryFn).toHaveBeenCalledTimes(1);
  });

  it("reads a shown answer again, removes unshown ones, and settles without a loop", async () => {
    const client = bound();
    const shown = ["github-files", "page", PR, ...named()];
    const unshown = ["github-patch", "page", PR, ...named()];
    const host = ["github-info", "page", PR, ...hostWide()];
    client.setQueryData(shown, "files as bob");
    client.setQueryData(unshown, "patch as bob");
    client.setQueryData(host, "as alice");
    const queryFn = vi.fn().mockRejectedValue(new Error("refused"));
    observe(client, shown, queryFn);

    refuseMacReadScope(named());
    await vi.waitFor(() => expect(client.getQueryState(shown)?.status).toBe("error"));
    // Its own failure refuses again; nothing loops.
    refuseMacReadScope(named());
    await tick();
    await tick();

    expect(client.getQueryData(shown)).toBeUndefined();
    expect(queryFn).toHaveBeenCalledTimes(1);
    expect(client.getQueryCache().find({ queryKey: unshown })).toBeUndefined();
    expect(client.getQueryData(host)).toBe("as alice");
  });

  it("clears a read in flight at once, and again if a cancel brings the old answer back", async () => {
    const client = bound();
    const key = ["github-info", "page", PR, ...named()];
    client.setQueryData(key, "as bob");
    const queryFn = vi.fn(
      ({ signal }: { signal: AbortSignal }) =>
        new Promise<string>((_resolve, reject) => {
          signal.addEventListener("abort", () => reject(new Error("aborted")));
        }),
    );
    const observer = observe(client, key, queryFn as never);
    void observer.refetch();
    await vi.waitFor(() => expect(client.getQueryState(key)?.fetchStatus).toBe("fetching"));

    refuseMacReadScope(named());
    expect(client.getQueryData(key)).toBeUndefined();

    // A cancel rolls the query back to what it showed before the refusal…
    await client.cancelQueries({ queryKey: key });
    await tick();
    // …which is dropped again rather than shown.
    expect(client.getQueryData(key)).toBeUndefined();
  });
});

describe("a change made on this Mac", () => {
  it("cancels a named read begun before it and reads the PR again after", async () => {
    const client = bound();
    const key = ["github-info", "page", PR, ...named()];
    const host = ["github-info", "page", PR, ...hostWide()];
    const reads: string[] = [];
    let release: ((value: string) => void) | null = null;
    const queryFn = vi.fn(({ signal }: { signal: AbortSignal }) => {
      const label = queryFn.mock.calls.length === 1 ? "before the write" : "after the write";
      reads.push(label);
      if (label === "after the write") return Promise.resolve(label);
      return new Promise<string>((resolve, reject) => {
        release = resolve;
        signal.addEventListener("abort", () => reject(new Error("aborted")));
      });
    });
    const hostFn = vi.fn(async () => "as alice");
    client.setQueryData(host, "as alice");
    observe(client, host, hostFn);
    observe(client, key, queryFn as never);
    await vi.waitFor(() => expect(release).not.toBeNull());

    namedPullRequestWritten(PR, "user-1");
    await vi.waitFor(() => expect(client.getQueryData(key)).toBe("after the write"));
    release!("before the write");
    await tick();

    expect(client.getQueryData(key)).toBe("after the write");
    expect(reads).toEqual(["before the write", "after the write"]);
    // The actor's own reads are patched by the write itself, not read again here.
    expect(hostFn).not.toHaveBeenCalled();
  });

  it("ignores a change made for another workspace user, or another PR", async () => {
    const client = bound();
    const key = ["github-info", "page", PR, ...named()];
    const queryFn = vi.fn(async () => "as bob");
    client.setQueryData(key, "as bob");
    observe(client, key, queryFn);

    namedPullRequestWritten(PR, "user-2");
    namedPullRequestWritten("https://github.com/o/r/pull/2", "user-1");
    await tick();

    expect(queryFn).not.toHaveBeenCalled();
    expect(client.getQueryState(key)?.isInvalidated).toBe(false);
  });
});
