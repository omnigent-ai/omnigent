import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, describe, expect, it } from "vitest";

import type { CodeHostReadPriority } from "@/lib/nativeBridge";

import {
  claimSpeculativeRead,
  infoReadMeta,
  observedReadPriority,
  promoteRead,
  readAtObservedPriority,
  readAhead,
  readPriorityMeta,
  resetReadPrioritiesForTests,
} from "./pullRequestReadPriority";

const KEY = ["pr-page-github-info", "conv_1", "https://github.com/acme/app/pull/7"];

/** A PR query whose reads the test settles, each recording its priority and signal. */
function prQuery(client: QueryClient) {
  const reads: { priority: CodeHostReadPriority; signal: AbortSignal; answer: () => void }[] = [];
  const queryFn = ({ signal }: { signal: AbortSignal }) =>
    readAtObservedPriority(
      client,
      KEY,
      (priority) =>
        new Promise<string>((resolve) => {
          reads.push({ priority, signal, answer: () => resolve(priority) });
        }),
    );
  const options = (priority: CodeHostReadPriority, enabled = true) => ({
    queryKey: KEY,
    queryFn,
    meta: readPriorityMeta(priority),
    retry: false,
    enabled,
  });
  const observe = (priority: CodeHostReadPriority, enabled = true) => {
    const observer = new QueryObserver(client, options(priority, enabled));
    return observer.subscribe(() => {});
  };
  const fetch = () => client.fetchQuery({ queryKey: KEY, queryFn, retry: false });
  return { reads, observe, fetch };
}

const settle = () =>
  new Promise((resolve) => {
    setTimeout(resolve, 0);
  });

describe("a PR read's priority", () => {
  const clients: QueryClient[] = [];
  const newClient = () => {
    const client = new QueryClient();
    clients.push(client);
    return client;
  };
  afterEach(() => {
    for (const client of clients.splice(0)) client.clear();
    resetReadPrioritiesForTests();
  });

  it("is a background read while only list rows want it, and one the user asked for otherwise", async () => {
    const client = newClient();
    const { reads, observe } = prQuery(client);
    const stopRow = observe("background");
    await settle();
    expect(reads.map(({ priority }) => priority)).toEqual(["background"]);
    expect(infoReadMeta(true)).toEqual(readPriorityMeta("background"));
    const stopPanel = observe("interactive");
    expect(observedReadPriority(client, KEY)).toBe("interactive");
    stopRow();
    stopPanel();
    // Nobody watching, e.g. a plain fetch: a read the user asked for.
    expect(observedReadPriority(client, KEY)).toBe("interactive");
  });

  it("takes the highest priority its watchers want: interactive, then prefetch, then background", () => {
    const client = newClient();
    const { observe } = prQuery(client);
    const stopRow = observe("background", false);
    // A disabled observer wants nothing yet.
    expect(observedReadPriority(client, KEY)).toBe("interactive");
    const claim = claimSpeculativeRead(client, KEY);
    expect(observedReadPriority(client, KEY)).toBe("prefetch");
    const stopEnabledRow = observe("background");
    expect(observedReadPriority(client, KEY)).toBe("prefetch");
    claim.release();
    expect(observedReadPriority(client, KEY)).toBe("background");
    const stopAhead = observe("prefetch");
    expect(observedReadPriority(client, KEY)).toBe("prefetch");
    const stopPanel = observe("interactive");
    expect(observedReadPriority(client, KEY)).toBe("interactive");
    for (const stop of [stopRow, stopEnabledRow, stopAhead, stopPanel]) stop();
  });

  it("reads a claimed query as a read ahead, and its claim settles only when the shell answers", async () => {
    const client = newClient();
    const { reads, fetch } = prQuery(client);
    const claim = claimSpeculativeRead(client, KEY);
    const fetching = fetch().catch(() => {});
    await settle();
    expect(reads.map(({ priority }) => priority)).toEqual(["prefetch"]);
    expect(claim.pending()).toBe(true);
    // Its query is cancelled, but the shell's call is still out: the claim still counts it.
    await client.cancelQueries({ queryKey: KEY, exact: true });
    await fetching;
    let settled = false;
    void claim.settled().then(() => (settled = true));
    await settle();
    expect(settled).toBe(false);
    reads[0]!.answer();
    await settle();
    expect(settled).toBe(true);
    expect(claim.pending()).toBe(false);
    claim.release();
  });

  it("reads again as one the user asked for when its PR opens during a lower read, dropping that one", async () => {
    const client = newClient();
    const { reads, observe } = prQuery(client);
    const stopRow = observe("background");
    await settle();
    // Opening the PR promotes the row's read as the panel starts watching it.
    const stopPanel = observe("interactive");
    await settle();
    expect(reads.map(({ priority }) => priority)).toEqual(["background", "interactive"]);
    // The background read's signal aborted, so the shell lets go of it if it hasn't run.
    expect(reads[0]?.signal.aborted).toBe(true);
    reads[1]?.answer();
    await settle();
    expect(client.getQueryData(KEY)).toBe("interactive");
    // Once is enough: the read in flight is already one the user asked for.
    expect(promoteRead(client, KEY)).toBe(false);
    stopRow();
    stopPanel();
  });

  it("promotes a read ahead to one the user asked for, once", async () => {
    const client = newClient();
    const { reads, observe } = prQuery(client);
    const stopAhead = observe("prefetch");
    await settle();
    const stopPanel = observe("interactive");
    const stopSecondPanel = observe("interactive");
    await settle();
    expect(reads.map(({ priority }) => priority)).toEqual(["prefetch", "interactive"]);
    stopAhead();
    stopPanel();
    stopSecondPanel();
  });

  it("leaves a list row's background read alone while only list rows want it", async () => {
    const client = newClient();
    const { reads, observe } = prQuery(client);
    const stopRow = observe("background");
    await settle();
    // Another list consumer, e.g. the list beside an open PR, mounts while it's in flight.
    const stopList = observe("background");
    await settle();
    expect(promoteRead(client, KEY)).toBe(false);
    expect(reads.map(({ priority, signal }) => [priority, signal.aborted])).toEqual([
      ["background", false],
    ]);
    // It's still promoted once a consumer that wants it now opens.
    const stopPanel = observe("interactive");
    await settle();
    expect(reads.map(({ priority }) => priority)).toEqual(["background", "interactive"]);
    stopRow();
    stopList();
    stopPanel();
  });

  it("never promotes a read ahead for a list row, nor a read for a disabled panel", async () => {
    const client = newClient();
    const { reads, observe } = prQuery(client);
    const stopAhead = observe("prefetch");
    await settle();
    const stopRow = observe("background");
    const stopWaitingPanel = observe("interactive", false);
    await settle();
    expect(reads.map(({ priority, signal }) => [priority, signal.aborted])).toEqual([
      ["prefetch", false],
    ]);
    for (const stop of [stopAhead, stopRow, stopWaitingPanel]) stop();
  });

  it("leaves a read alone that already went out as one the user asked for, or that answered", async () => {
    const client = newClient();
    const { reads, observe } = prQuery(client);
    const stopPanel = observe("interactive");
    await settle();
    expect(promoteRead(client, KEY)).toBe(false);
    reads[0]?.answer();
    await settle();
    expect(promoteRead(client, KEY)).toBe(false);
    expect(reads).toHaveLength(1);
    stopPanel();
  });

  it("reads a list row's background read again as a read ahead once a hover claims it", async () => {
    const client = newClient();
    const { reads, observe } = prQuery(client);
    const stopRow = observe("background");
    await settle();
    // The hover joins the row's read under way, as a read ahead does with its fetch.
    const ahead = readAhead(client, KEY, () =>
      client.fetchQuery({ queryKey: KEY, queryFn: () => "unused", retry: false }),
    );
    await settle();
    expect(reads.map(({ priority, signal }) => [priority, signal.aborted])).toEqual([
      ["background", true],
      ["prefetch", false],
    ]);
    reads[1]!.answer();
    // The read ahead answers with the read it was promoted to, not the cancelled one.
    await expect(ahead).resolves.toBe("prefetch");
    stopRow();
  });

  it("forgets a promoted read once it answers, so a later watcher finds the answer fresh", async () => {
    const client = newClient();
    const { reads, observe } = prQuery(client);
    const stopRow = observe("background");
    await settle();
    const stopAhead = observe("prefetch");
    await settle();
    expect(reads).toHaveLength(2);
    for (const read of reads) read.answer();
    await settle();
    stopAhead();
    // Nothing is in flight: a panel that finds the answer fresh reads nothing more.
    const query = client.getQueryCache().find({ queryKey: KEY, exact: true })!;
    const stopPanel = new QueryObserver(client, {
      ...query.options,
      queryKey: KEY,
      staleTime: 60_000,
      meta: readPriorityMeta("interactive"),
    }).subscribe(() => {});
    await settle();
    expect(reads).toHaveLength(2);
    expect(promoteRead(client, KEY)).toBe(false);
    stopRow();
    stopPanel();
  });

  it("reuses a promoted read's answer while the read it replaced still runs", async () => {
    const client = newClient();
    const { reads, observe } = prQuery(client);
    const stopRow = observe("background");
    await settle();
    const ahead = readAhead(client, KEY, () =>
      client.fetchQuery({ queryKey: KEY, queryFn: () => "unused", retry: false }),
    );
    await settle();
    expect(reads.map(({ priority }) => priority)).toEqual(["background", "prefetch"]);
    // The read ahead answers first; the shell can't stop the row's read, already running.
    reads[1]!.answer();
    await expect(ahead).resolves.toBe("prefetch");
    // Opening the PR finds the answer fresh: the replaced read isn't promoted again.
    const query = client.getQueryCache().find({ queryKey: KEY, exact: true })!;
    const stopPanel = new QueryObserver(client, {
      ...query.options,
      queryKey: KEY,
      staleTime: 60_000,
      meta: readPriorityMeta("interactive"),
    }).subscribe(() => {});
    await settle();
    expect(reads.map(({ priority }) => priority)).toEqual(["background", "prefetch"]);
    reads[0]!.answer();
    await settle();
    stopRow();
    stopPanel();
  });
});
