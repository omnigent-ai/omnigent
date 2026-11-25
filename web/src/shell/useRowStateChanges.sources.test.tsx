// Two lists on one page share a recovery scope: the table, and the source beside it that reads
// Recently reviewed's PRs. A Close's Undo runs through whichever lists its PR when it's clicked,
// with that list's live read of it, however the two mounted, so a PR the table handed over is
// changed back only where the list that has it now still finds it closed.

import type * as SonnerModule from "sonner";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { toast, toastError } = vi.hoisted(() => ({ toast: vi.fn(), toastError: vi.fn() }));
vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast: Object.assign(toast, { error: toastError, success: vi.fn(), dismiss: vi.fn() }),
}));

import { MacReadAsContext } from "@/hooks/macReadAs";
import {
  resetDesktopPullRequestWritesForTests,
  setLifecycleFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import { resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import { resetMacNamedReadsForTests } from "@/lib/macNamedReads";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";
import { resetStateChainsForTests } from "@/pullRequestPage/pullRequestStateChain";
import { installNamedWriteShell, removeNamedWriteShell } from "@/test/macNamedWriteShell";

import type { PullRequestListEntry } from "./PullRequestListRows";
import { useRowStateChanges } from "./useRowStateChanges";

const PR = "https://github.com/acme/app/pull/42";
const AUTOMATIC = { reader: null };
const SCOPE = "page-recovery";
const WRITES = ["pull_request_close", "pull_request_reopen"];
const answer = (request: Record<string, unknown>) => ({
  ok: true,
  data: {
    object: "session.github.pr_state",
    pr_url: PR,
    state: request.operation === "pull_request_close" ? "CLOSED" : "OPEN",
  },
  actor: { login: "alice" },
});

/** What one list shows of the PR now: nothing where it doesn't list it. */
interface Listing {
  state: string | null;
  /** How this list writes the PR's link; the same PR either way. */
  url?: string;
}

/** The same PR as another list may write it. */
const SPELLED = "https://GitHub.com/ACME/App/pull/42/";

function renderList(listing: Listing) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <MacReadAsContext.Provider value={AUTOMATIC}>{children}</MacReadAsContext.Provider>
    </QueryClientProvider>
  );
  return renderHook(
    () => {
      const url = listing.url ?? PR;
      const entry = {
        association: { url, repository: "acme/app", number: 42, provider: "github" },
        row: { url, repository: "acme/app", number: 42, title: "Build" },
        pr: { url, state: listing.state ?? "", is_draft: false },
      } as unknown as PullRequestListEntry;
      const changes = useRowStateChanges({
        conversationId: "pull-requests-page",
        recoveryScope: SCOPE,
        entriesByUrl: listing.state === null ? new Map() : new Map([[url, entry]]),
        onOpen: () => {},
      });
      return changes.actionFor(entry, "#42");
    },
    { wrapper },
  );
}

interface ToastOptions {
  action: { label: string; onClick: () => void };
}

/** Close the PR from the table, and the Undo its toast offers. */
async function closeFromTable(table: ReturnType<typeof renderList>) {
  act(() => table.result.current!.run());
  await waitFor(() =>
    expect(toast).toHaveBeenCalledWith("Closed #42 as @alice", expect.anything()),
  );
  return (toast.mock.calls.at(-1)![1] as ToastOptions).action.onClick;
}

beforeEach(() => {
  toast.mockReset();
  toastError.mockReset();
  resetStateChainsForTests();
  setLifecycleFlagForTests(true);
});

afterEach(() => {
  cleanup();
  setLifecycleFlagForTests(null);
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  removeNamedWriteShell();
});

describe.each([
  ["the table mounted first", "table", PR],
  ["the source beside it mounted first", "beside", PR],
  ["the table mounted first, beside writing the link another way", "table", SPELLED],
  ["the source beside it mounted first, writing the link another way", "beside", SPELLED],
] as const)("with %s", (_label, first, besideUrl) => {
  function mountBoth() {
    const table: Listing = { state: "OPEN" };
    const beside: Listing = { state: null, url: besideUrl };
    const views =
      first === "table"
        ? { table: renderList(table), beside: renderList(beside) }
        : { beside: renderList(beside), table: renderList(table) };
    return { ...views, listings: { table, beside } };
  }

  /** The table takes the closed PR out of its list, and Recently reviewed reads it. */
  function handOver(view: ReturnType<typeof mountBoth>, besideState: string) {
    view.listings.table.state = null;
    view.listings.beside.state = besideState;
    view.table.rerender();
    view.beside.rerender();
  }

  it("changes a closed PR back through the list that has it now", async () => {
    const shell = installNamedWriteShell(answer, { extraWrites: WRITES });
    await shell.ready(PR);
    const view = mountBoth();
    const undo = await closeFromTable(view.table);
    handOver(view, "CLOSED");
    act(() => undo());
    await waitFor(() => expect(shell.codeHostWrite).toHaveBeenCalledTimes(2));
    expect(shell.codeHostWrite.mock.calls[1]![0]).toMatchObject({
      operation: "pull_request_reopen",
    });
  });

  it("sends nothing where the list that has it now reads it merged", async () => {
    const shell = installNamedWriteShell(answer, { extraWrites: WRITES });
    await shell.ready(PR);
    const view = mountBoth();
    const undo = await closeFromTable(view.table);
    handOver(view, "MERGED");
    act(() => undo());
    await act(async () => {});
    expect(shell.codeHostWrite).toHaveBeenCalledTimes(1);
  });

  /** A Close GitHub refused once, its Try Again, and the PR handed over as `besideState`. */
  async function refusedThenHandedOver(besideState: string) {
    let closes = 0;
    const shell = installNamedWriteShell(
      (request) =>
        request.operation === "pull_request_close" && ++closes === 1
          ? { ok: false, error: { code: "rejected", message: "Refused once." } }
          : answer(request),
      { extraWrites: WRITES },
    );
    await shell.ready(PR);
    const view = mountBoth();
    act(() => view.table.result.current!.run());
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    const tryAgain = (toastError.mock.calls.at(-1)![1] as ToastOptions).action.onClick;
    handOver(view, besideState);
    act(() => tryAgain());
    await act(async () => {});
    return shell;
  }

  it("tries a refused Close again through the list that has it now", async () => {
    const shell = await refusedThenHandedOver("OPEN");
    await waitFor(() => expect(shell.codeHostWrite).toHaveBeenCalledTimes(2));
  });

  it("tries nothing again where the list that has it now reads it merged", async () => {
    const shell = await refusedThenHandedOver("MERGED");
    expect(shell.codeHostWrite).toHaveBeenCalledTimes(1);
  });

  it("asks the table, not the newest list, while the table still lists the PR", async () => {
    const shell = installNamedWriteShell(answer, { extraWrites: WRITES });
    await shell.ready(PR);
    const view = mountBoth();
    const undo = await closeFromTable(view.table);
    // The table reads it merged; the source beside it doesn't list it.
    view.listings.table.state = "MERGED";
    view.table.rerender();
    act(() => undo());
    await act(async () => {});
    expect(shell.codeHostWrite).toHaveBeenCalledTimes(1);
  });
});

it("changes it back through the source beside the table once the table has gone", async () => {
  const shell = installNamedWriteShell(answer, { extraWrites: WRITES });
  await shell.ready(PR);
  const table = renderList({ state: "OPEN" });
  const beside: Listing = { state: null };
  const besideView = renderList(beside);
  const undo = await closeFromTable(table);
  // The list is hidden; the sidebar's tab for the PR is read beside it.
  table.unmount();
  beside.state = "CLOSED";
  besideView.rerender();
  act(() => undo());
  await waitFor(() => expect(shell.codeHostWrite).toHaveBeenCalledTimes(2));
});
