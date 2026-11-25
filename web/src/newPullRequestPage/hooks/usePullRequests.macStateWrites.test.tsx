// Labels and reviewers on the Mac write path, and the picker reads they need, with the
// real read-path and write-path stores over a stand-in shell: which path a change takes,
// what its answer may touch, and what's left for whom after a change of workspace user.

import { invalidatedKey } from "@/test/invalidatedQueries";
import type * as IdentityModule from "@/lib/identity";
import type * as SonnerModule from "sonner";
import { onlineManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { currentUser, hostFetch, toastSuccess, identityListeners } = vi.hoisted(() => ({
  currentUser: { id: "user-1" as string | null },
  hostFetch: vi.fn(),
  toastSuccess: vi.fn(),
  identityListeners: new Set<() => void>(),
}));
vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast: Object.assign(vi.fn(), { success: toastSuccess, error: vi.fn() }),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => currentUser.id,
  subscribeIdentity: (listener: () => void) => {
    identityListeners.add(listener);
    return () => identityListeners.delete(listener);
  },
  authenticatedFetch: hostFetch,
}));

import {
  GITHUB_WRITE_SET_V1,
  resetDesktopPullRequestWritesForTests,
  setPageWriteOperationsForTests,
  writeMacWritesIntent,
} from "@/lib/desktopPullRequestWrites";
import { readScope, resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import { installMacWriteShell, removeMacWriteShell } from "@/test/macWriteShell";
import { macReadScopeKey } from "@/lib/macReadScope";
import {
  usePullRequestRepoLabels,
  useUpdatePullRequestLabels,
  useUpdatePullRequestReviewers,
  type PullRequestInfo,
  type PullRequestLabelChange,
} from "./usePullRequests";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const PR = "https://github.com/acme/app/pull/42";
const SESSION = "session-1";
const BUG = { name: "bug", color: "d73a4a", description: null };
const READY = { name: "ready", color: "0e8a16", description: null };

const labelsChanged = (labels: unknown) => ({
  ok: true,
  data: { object: "session.github.pr_labels", labels },
  actor: { login: "mac-octo" },
});

const installShell = (
  codeHostWrite: (request: unknown) => Promise<unknown>,
  reads: Record<string, unknown> = {},
) => installMacWriteShell(PR, codeHostWrite, reads);

function setup() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const info = (labels: unknown[]) =>
    ({
      available: true,
      selected_pr_url: PR,
      pr: { url: PR, labels, reviewers: [] },
    }) as unknown as PullRequestInfo;
  const macKey = ["github-info", SESSION, PR, ...readScope(PR)];
  queryClient.setQueryData(macKey, info([BUG]));
  const otherKeys = [
    ["github-info", SESSION],
    ["github-info", SESSION, PR, "host-mutation-context"],
    ["github-info", SESSION, PR, ...macReadScopeKey("github", "github.com", "user-2")],
  ];
  for (const key of otherKeys) queryClient.setQueryData(key, info([BUG]));
  const names = (key: unknown[]) =>
    (queryClient.getQueryData<PullRequestInfo>(key)?.pr?.labels ?? []).map((label) => label.name);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  const invalidate = vi.spyOn(queryClient, "invalidateQueries");
  return {
    queryClient,
    wrapper,
    macLabels: () => names(macKey),
    otherLabels: () => otherKeys.map(names),
    invalidate,
  };
}

const addReady: PullRequestLabelChange = { prUrl: PR, add: [READY], remove: [] };

beforeEach(() => {
  currentUser.id = "user-1";
  identityListeners.clear();
  resetDesktopPullRequestWritesForTests();
  hostFetch.mockReset();
  toastSuccess.mockReset();
  hostFetch.mockImplementation(
    async () =>
      new Response(JSON.stringify({ labels: [BUG, READY] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
  );
});

afterEach(() => {
  writeMacWritesIntent("automatic");
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  removeMacWriteShell();
  onlineManager.setOnline(true);
});

describe("labels on this Mac", () => {
  it("stay on the host when the user chose Off", async () => {
    const { write } = await installShell(async () => labelsChanged([BUG, READY]));
    // The user keeps every change on the session's machine.
    writeMacWritesIntent("off");
    const { wrapper } = setup();
    const { result } = renderHook(() => useUpdatePullRequestLabels(SESSION), { wrapper });
    act(() => result.current.mutate(addReady));
    await waitFor(() => expect(hostFetch).toHaveBeenCalledOnce());
    expect(write).not.toHaveBeenCalled();
  });

  it("go to this Mac at the click, and patch only this Mac's cache with the labels now", async () => {
    const { write } = await installShell(async () => labelsChanged([BUG, READY]));
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    const { wrapper, macLabels, otherLabels } = setup();
    const { result } = renderHook(() => useUpdatePullRequestLabels(SESSION), { wrapper });
    let sentDuringClick = false;
    act(() => {
      result.current.mutate({ prUrl: PR, add: [READY], remove: [BUG] });
      sentDuringClick = write.mock.calls.length === 1;
    });
    expect(sentDuringClick).toBe(true);
    expect(write.mock.calls[0][0]).toMatchObject({
      operation: "pull_request_labels_update",
      actor: { login: "mac-octo", generation: 1 },
      params: { repository: "acme/app", number: 42, add: ["ready"], remove: ["bug"] },
    });
    await waitFor(() => expect(macLabels()).toEqual(["bug", "ready"]));
    expect(otherLabels()).toEqual([["bug"], ["bug"], ["bug"]]);
    expect(hostFetch).not.toHaveBeenCalled();
    expect(toastSuccess).not.toHaveBeenCalled();
  });

  it("shows main's copy for a partial change, reads the PR again, and never retries or uses the host", async () => {
    const message =
      "Some changes were made: removed bug. Adding ready failed: GitHub didn't accept this change.";
    const { write } = await installShell(async () => ({
      ok: false,
      error: { code: "partial", cause: "rejected", message },
    }));
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    const { wrapper, invalidate } = setup();
    const { result } = renderHook(() => useUpdatePullRequestLabels(SESSION), { wrapper });
    act(() => result.current.mutate({ prUrl: PR, add: [READY], remove: [BUG] }));
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe(message);
    expect(result.current.variables?.prUrl).toBe(PR);
    expect(invalidatedKey(invalidate, ["github-info", SESSION, PR])).toBe(true);
    expect(write).toHaveBeenCalledOnce();
    expect(hostFetch).not.toHaveBeenCalled();
  });
});

describe("reviewers on this Mac", () => {
  it("send users and teams apart, and patch the reviewers now", async () => {
    const { write } = await installShell(async () => ({
      ok: true,
      data: {
        object: "session.github.pr_reviewers",
        reviewers: [{ login: "rev-octo", kind: "user", state: "PENDING" }],
      },
      actor: { login: "mac-octo" },
    }));
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    const { wrapper, queryClient } = setup();
    const { result } = renderHook(() => useUpdatePullRequestReviewers(SESSION), { wrapper });
    act(() =>
      result.current.mutate({
        prUrl: PR,
        add: [
          { login: "rev-octo", kind: "user" },
          { login: "core", kind: "team" },
        ],
        remove: [{ login: "old-octo", kind: "user" }],
      }),
    );
    expect(write.mock.calls[0][0]).toMatchObject({
      operation: "pull_request_reviewers_update",
      params: {
        add: { users: ["rev-octo"], teams: ["core"] },
        remove: { users: ["old-octo"], teams: [] },
      },
    });
    await waitFor(() =>
      expect(
        queryClient.getQueryData<PullRequestInfo>(["github-info", SESSION, PR, ...readScope(PR)])
          ?.pr?.reviewers,
      ).toEqual([{ login: "rev-octo", kind: "user", state: "PENDING" }]),
    );
  });
});

describe("what the pickers list", () => {
  it("asks the host where changes go there", async () => {
    await installShell(async () => ({}));
    // The user keeps every change on the session's machine.
    writeMacWritesIntent("off");
    const { wrapper } = setup();
    hostFetch.mockImplementation(
      async () =>
        new Response(JSON.stringify({ object: "list", data: [BUG], has_more: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    );
    const { result } = renderHook(() => usePullRequestRepoLabels(SESSION, PR, true), {
      wrapper,
    });
    await waitFor(() => expect(result.current.data?.data).toEqual([BUG]));
    expect(hostFetch).toHaveBeenCalledOnce();
  });
});
