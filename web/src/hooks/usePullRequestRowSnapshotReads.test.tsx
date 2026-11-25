// A followed row reads its overview again when its batch's snapshot changes, and only while it's
// one of the list's read targets: a row scrolled away keeps what it read.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type * as RowSnapshotsModule from "@/lib/pullRequestRowSnapshots";

vi.mock("@/lib/pullRequestRowSnapshots", async (importOriginal) => ({
  ...(await importOriginal<typeof RowSnapshotsModule>()),
  rowReaderHeld: () => false,
}));

import type { usePullRequestRowReads } from "@/newPullRequestPage/hooks/usePullRequests";

import { useRowSnapshotReads } from "./usePullRequestRowSnapshotReads";

const urlOf = (number: number) => `https://github.com/acme/app/pull/${number}`;
const keyOf = (url: string) => ["github-info", "page", url, "mac-read"];
const ROW_READS = {
  keyOf,
  targetOf: () => ({ provider: "github", host: "github.com" }),
  scopeOf: () => ["mac-read"],
} as unknown as ReturnType<typeof usePullRequestRowReads>;

function setup(numbers: number[]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // Each row read once as mac-octo, following an older snapshot of its PR.
  for (const number of numbers) {
    client.setQueryData(keyOf(urlOf(number)), {
      followed_snapshot: "old",
      dispatched_at: 10,
      read_on_mac: { login: "mac-octo" },
    });
  }
  const refetch = vi.spyOn(client, "refetchQueries").mockResolvedValue();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const live = numbers.map((number) => ({
    association: { url: urlOf(number) },
    data: { read_on_mac: { provider: "github" as const, login: "mac-octo", host: "github.com" } },
    offline: false,
  }));
  // The batch now holds a newer snapshot of every row's PR.
  const snapshots = new Map(
    numbers.map((number) => [
      urlOf(number),
      { fingerprint: "new", reader: "mac-octo", dispatchedAt: 20 },
    ]),
  );
  const refetched = () =>
    refetch.mock.calls.map(([filters]) => (filters as { queryKey: unknown[] }).queryKey[2]);
  return { client, wrapper, live, snapshots, refetched };
}

describe("useRowSnapshotReads", () => {
  it("reads a row again when its batch's snapshot changed", () => {
    const { wrapper, live, snapshots, refetched } = setup([1, 2]);
    renderHook(() => useRowSnapshotReads({ live, snapshots, rowReads: ROW_READS, enabled: true }), {
      wrapper,
    });
    expect(refetched()).toEqual([urlOf(1), urlOf(2)]);
  });

  it("reads only the list's read targets, never a row scrolled away", () => {
    const { wrapper, live, snapshots, refetched } = setup([1, 2, 3]);
    renderHook(
      () =>
        useRowSnapshotReads({
          live,
          snapshots,
          rowReads: ROW_READS,
          enabled: true,
          readUrls: new Set([urlOf(2)]),
        }),
      { wrapper },
    );
    expect(refetched()).toEqual([urlOf(2)]);
  });
});
