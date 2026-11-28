// Convert to draft, Ready for review and the auto-merge changes on a Pull Requests list row:
// where the session's host serves the change, the row offers it, sends it through the host and
// toasts the outcome with an Undo that sends the inverse; elsewhere the row hands off to GitHub.

import type * as SonnerModule from "sonner";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, renderHook, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const host = vi.hoisted(() => ({
  served: new Set<string>(),
  write: vi.fn(),
  read: vi.fn(),
  toast: vi.fn(),
}));
vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast: Object.assign(host.toast, { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() }),
}));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => ({ features: {} }) }));
vi.mock("@/lib/workspaceApi", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  enabledWorkspaceRoutes: () => ["server"],
  servingRoutes: async () => ({
    serving: [{ route: "server", key: "server:session-1", served: host.served }],
    failures: [],
  }),
}));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  writePullRequestRpc: host.write,
  readPullRequestRpc: host.read,
}));

import { pullRequestRecordKey, sessionRecordOwner } from "@/lib/pullRequestRecord";
import { usePullRequestMergeOptions } from "@/newPullRequestPage/hooks/usePullRequests";
import type { WorkspaceClient } from "@/lib/workspaceApi";

import { resetLifecycleActionsForTests } from "./pullRequestLifecycleActions";
import {
  resetPullRequestPageRoutesForTests,
  usePullRequestPageRoute,
} from "./pullRequestPageRoute";
import { resetStateChainsForTests } from "./pullRequestStateChain";
import {
  type LifecycleRow,
  rowLifecycleAvailability,
  useRowLifecycleActions,
} from "./useRowLifecycleActions";

const PR = "https://github.com/acme/app/pull/42";
const SESSION = "session-1";
const HEAD = "a".repeat(40);
const READS = [
  "GetPullRequest",
  "ListPullRequestFiles",
  "GetPullRequestDiff",
  "ListPullRequestCommits",
  "ListReviewThreads",
  "ListReviewThreadComments",
];

function row(pr: Partial<NonNullable<LifecycleRow["pr"]>> = {}): LifecycleRow {
  return {
    url: PR,
    number: 42,
    paletteName: "#42 Fix the build",
    pr: { state: "OPEN", is_draft: false, head_sha: HEAD, ...pr },
  };
}

const listed = new Map<string, LifecycleRow>();

function newQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

async function renderRows(served: readonly string[], queryClient = newQueryClient()) {
  host.served = new Set(served);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  const view = renderHook(
    () => ({
      route: usePullRequestPageRoute(SESSION),
      rows: useRowLifecycleActions(SESSION, true, listed),
    }),
    { wrapper },
  );
  await waitFor(() => expect(view.result.current.route).not.toBe("loading"));
  view.rerender();
  return view;
}

function actionsOf(view: Awaited<ReturnType<typeof renderRows>>, shown: LifecycleRow) {
  listed.set(shown.url, shown);
  return view.result.current.rows.actionsFor(shown);
}

/** The method and request of each host write sent so far. */
async function sent(): Promise<[string, unknown][]> {
  const requests: [string, unknown][] = [];
  const workspace = new Proxy(
    {},
    {
      get: (_target, method: string) => async (request: unknown) => {
        requests.push([method, request]);
        return { payload: {} };
      },
    },
  ) as unknown as WorkspaceClient;
  await Promise.all(host.write.mock.calls.map(([, rpc]) => rpc.call(workspace)));
  return requests;
}

beforeEach(() => {
  listed.clear();
  host.write.mockReset();
  host.read.mockReset();
  host.toast.mockReset();
});
afterEach(() => {
  cleanup();
  resetPullRequestPageRoutesForTests();
  resetLifecycleActionsForTests();
  resetStateChainsForTests();
});

describe("which row changes the host makes", () => {
  const facts = { held: false, offered: () => true };

  it("offers each served change here, auto-merge on included", () => {
    expect(rowLifecycleAvailability("convert_to_draft", row(), facts)).toEqual({ kind: "mac" });
    expect(rowLifecycleAvailability("ready_for_review", row({ is_draft: true }), facts)).toEqual({
      kind: "mac",
    });
    expect(rowLifecycleAvailability("enable_auto_merge", row(), facts)).toEqual({ kind: "mac" });
  });

  it("holds a served change while another runs for the PR", () => {
    expect(
      rowLifecycleAvailability("enable_auto_merge", row(), { ...facts, held: true }),
    ).toMatchObject({ kind: "held" });
  });
});

describe("a row's changes where the host serves them", () => {
  it("converts to draft through the host once, and Undo marks it ready through the host", async () => {
    host.write.mockResolvedValue({ object: "session.github.pr_draft", pr_url: PR, is_draft: true });
    const view = await renderRows([...READS, "SetPullRequestDraft"]);
    const convert = actionsOf(view, row()).find(
      (action) => action.id === "lifecycle:convert_to_draft",
    );
    expect(convert?.href).toBeUndefined();

    act(() => convert!.run());
    await waitFor(() => expect(host.toast).toHaveBeenCalled());
    await expect(sent()).resolves.toEqual([["setPullRequestDraft", { prUrl: PR, draft: true }]]);
    const [, options] = host.toast.mock.calls.at(-1)!;
    expect(options.action.label).toBe("Undo");

    // The list reads it as a draft by the time Undo is clicked.
    listed.set(PR, row({ is_draft: true }));
    host.write.mockResolvedValue({
      object: "session.github.pr_draft",
      pr_url: PR,
      is_draft: false,
    });
    act(() => options.action.onClick(undefined));
    await waitFor(() => expect(host.write).toHaveBeenCalledTimes(2));
    await expect(sent()).resolves.toEqual([
      ["setPullRequestDraft", { prUrl: PR, draft: true }],
      ["setPullRequestDraft", { prUrl: PR, draft: false }],
    ]);
  });

  it("turns auto-merge off through the host", async () => {
    host.write.mockResolvedValue({ object: "session.github.pr_merge", pr_url: PR });
    const view = await renderRows([...READS, "DisableAutoMerge"]);
    const disable = actionsOf(view, row({ auto_merge: { enabled: true } } as never)).find(
      (action) => action.id === "lifecycle:disable_auto_merge",
    );
    expect(disable?.href).toBeUndefined();
    act(() => disable!.run());
    await waitFor(() => expect(host.write).toHaveBeenCalledTimes(1));
    await expect(sent()).resolves.toEqual([["disableAutoMerge", { prUrl: PR }]]);
  });
});

describe("turning auto-merge on from a row", () => {
  it("asks even where the repository allows one method, and Cancel sends nothing", async () => {
    const queryClient = newQueryClient();
    queryClient.setQueryData(
      pullRequestRecordKey("pr-page-github-merge-options", PR, sessionRecordOwner(SESSION)),
      {
        object: "session.github.merge_options",
        methods: ["squash"],
        default: "squash",
        merge_queue: false,
        merge_state_status: "BLOCKED",
        auto_merge: null,
      },
    );
    const view = await renderRows([...READS, "EnableAutoMerge", "GetMergeOptions"], queryClient);
    const enable = actionsOf(view, row()).find(
      (action) => action.id === "lifecycle:enable_auto_merge",
    );
    expect(enable?.href).toBeUndefined();
    act(() => enable!.run());
    render(
      <QueryClientProvider client={queryClient}>
        {view.result.current.rows.dialog}
      </QueryClientProvider>,
    );

    expect(await screen.findByText("Turn on auto-merge for #42?")).toBeTruthy();
    expect(screen.getByText(/using “Squash and merge”/)).toBeTruthy();
    expect(host.write).not.toHaveBeenCalled();
    act(() => screen.getByRole("button", { name: "Cancel" }).click());
    await waitFor(() => expect(view.result.current.rows.dialog).toBeNull());
    expect(host.write).not.toHaveBeenCalled();
  });
});

describe("turning auto-merge on where the host can't read the merge options", () => {
  it("hands the row's Enable auto-merge off to GitHub", async () => {
    const view = await renderRows([...READS, "EnableAutoMerge"]);
    const enable = actionsOf(view, row()).find(
      (action) => action.id === "lifecycle:enable_auto_merge",
    );
    expect(enable?.href).toBe(PR);
    expect(host.write).not.toHaveBeenCalled();
  });

  it("never asks that host for the merge options, and asks one that serves them", async () => {
    host.read.mockResolvedValue({ methods: ["squash"], default: "squash" });
    async function readOptions(served: readonly string[]) {
      host.served = new Set(served);
      resetPullRequestPageRoutesForTests();
      const queryClient = newQueryClient();
      const wrapper = ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      );
      const view = renderHook(
        () => ({
          route: usePullRequestPageRoute(SESSION),
          options: usePullRequestMergeOptions(SESSION, PR, true),
        }),
        { wrapper },
      );
      await waitFor(() => expect(view.result.current.route).not.toBe("loading"));
      view.rerender();
      return view;
    }

    const unserved = await readOptions([...READS, "EnableAutoMerge"]);
    await act(async () => {
      await Promise.resolve();
    });
    expect(unserved.result.current.options.fetchStatus).toBe("idle");
    expect(host.read).not.toHaveBeenCalled();
    unserved.unmount();

    const served = await readOptions([...READS, "EnableAutoMerge", "GetMergeOptions"]);
    await waitFor(() => expect(served.result.current.options.data?.methods).toEqual(["squash"]));
    expect(host.read).toHaveBeenCalledWith(SESSION, "getMergeOptions", expect.any(Function));
  });
});

describe("a row's changes where the host doesn't serve them", () => {
  it("hands each one off to GitHub and sends nothing", async () => {
    const view = await renderRows(READS);
    const actions = actionsOf(view, row());
    const convert = actions.find((action) => action.id === "lifecycle:convert_to_draft");
    expect(convert?.href).toBe(PR);
    const enable = actions.find((action) => action.id === "lifecycle:enable_auto_merge");
    expect(enable?.href).toBe(PR);
    expect(host.write).not.toHaveBeenCalled();
  });
});
