// A Pull Requests list row's Enable Auto-merge…, through the real write route, hooks, store and
// dialog over a fake shell: which rows offer it, the dialog that reads this Mac's merge options
// before it asks, what it sends and to which head, and what it sends nothing for.

import type * as IdentityModule from "@/lib/identity";
import type * as SonnerModule from "sonner";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useCallback } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { currentUser, identityListeners, toast, toastError } = vi.hoisted(() => ({
  currentUser: { id: "user-1" as string | null },
  identityListeners: new Set<() => void>(),
  toast: vi.fn(),
  toastError: vi.fn(),
}));
vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast: Object.assign(toast, { success: vi.fn(), error: toastError, dismiss: vi.fn() }),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => currentUser.id,
  subscribeIdentity: (listener: () => void) => {
    identityListeners.add(listener);
    return () => identityListeners.delete(listener);
  },
}));

import { TooltipProvider } from "@/components/ui/tooltip";
import { MacReadAsContext } from "@/hooks/macReadAs";
import type { PullRequestMergeOptions } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  GITHUB_WRITE_SET_V1,
  LIFECYCLE_WRITE_OPERATIONS,
  resetDesktopPullRequestWritesForTests,
  setLifecycleFlagForTests,
  setMacWritesReleaseFlagForTests,
  setPageWriteOperationsForTests,
} from "@/lib/desktopPullRequestWrites";
import {
  acceptMacAnswer,
  desktopReadTarget,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  macReadTicket,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import {
  leaveNamedReader,
  type MacNamedReader,
  resetMacNamedReadsForTests,
} from "@/lib/macNamedReads";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";
import { BOB, installNamedWriteShell } from "@/test/macNamedWriteShell";
import type { RowAction } from "@/shell/PullRequestListRows";

import { resetChangeAttemptsForTests } from "./changeAttempts";
import { autoMergeRefusedBy } from "./EnableAutoMergeDialog";
import { resetLifecycleActionsForTests } from "./pullRequestLifecycleActions";
import { resetStateChainsForTests } from "./pullRequestStateChain";
import { type LifecycleRow, useRowLifecycleActions } from "./useRowLifecycleActions";

const PR = "https://github.com/acme/app/pull/42";
const HEAD = "a".repeat(40);
const MOVED = "c".repeat(40);
const SESSION = "pull-requests-page";
const ON = { merge_method: "SQUASH", enabled_by: "amy", enabled_at: "2026-11-05T10:00:00Z" };

const ok = (data: unknown) => ({ ok: true, data, actor: { login: "mac-octo" } });
const refusal = (code: string, message: string) => ({ ok: false, error: { code, message } });
const enabled = () =>
  ok({ object: "session.github.pr_auto_merge", pr_url: PR, enabled_at: "2026-11-08T12:00:00Z" });
const OPTIONS: PullRequestMergeOptions = {
  object: "session.github.merge_options",
  methods: ["squash", "merge"],
  default: "squash",
  merge_queue: false,
  merge_state_status: "BLOCKED",
  auto_merge: null,
};

interface Sent {
  operation: string;
  params: Record<string, unknown>;
}

/** What the shell answers this Mac's merge options read with, in turn. */
let optionsAnswers: unknown[] = [];

/** A shell reading and writing GitHub as `mac-octo`, answering each write from `answers`. */
async function installShell(
  answers: Record<string, () => unknown>,
  writes: readonly string[] = LIFECYCLE_WRITE_OPERATIONS,
) {
  const sent: Sent[] = [];
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: vi.fn(async (request: { operation: string }) => {
      if (request.operation !== "pull_request_merge_options") return { ok: true, data: [] };
      const answer = optionsAnswers.length > 1 ? optionsAnswers.shift() : optionsAnswers[0];
      return answer instanceof Error
        ? { ok: false, error: { code: "query_failed", message: answer.message } }
        : { ok: true, data: { ...(answer as object) } };
    }),
    codeHostWrite: vi.fn(async (request: Sent) => {
      sent.push({ operation: request.operation, params: request.params });
      return answers[request.operation]?.();
    }),
    codeHostWriteCancel: vi.fn(),
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
      }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
    },
    getDesktopFeatures: async () => ({
      codeHosts: [
        {
          id: "github",
          operations: [...GITHUB_MAC_READ_OPERATIONS, "pull_request_merge_options"],
          accounts: true,
          writes: { version: 1, operations: [...GITHUB_WRITE_SET_V1, ...writes] },
        },
      ],
    }),
  };
  githubMacReadsPreference.write("on");
  await desktopReadTarget(PR);
  acceptMacAnswer(macReadTicket("github", "github.com"), {
    identity: { host: "github.com", login: "mac-octo", generation: 1 },
  });
  setMacWritesReleaseFlagForTests(true);
  setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
  setLifecycleFlagForTests(true);
  return sent;
}

function row(pr: Partial<NonNullable<LifecycleRow["pr"]>> = {}): LifecycleRow {
  return {
    url: PR,
    number: 42,
    paletteName: "#42 Fix the build",
    pr: { state: "OPEN", is_draft: false, head_sha: HEAD, auto_merge: null, ...pr },
  };
}

/** The rows the list shows now. */
const listed = new Map<string, LifecycleRow>();

/**
 * The list's row hook, rendered with its dialog; `actionsFor` lists `shown` as it is now.
 * `refresh` renders it again with `page` as it is then.
 */
function renderList(recoveryScope?: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const page = {
    scope: recoveryScope,
    enabled: true,
    reader: null as MacNamedReader | null,
    /** The PRs GitHub's read says it merges with their stacks. */
    native: new Set<string>(),
  };
  const latest: { actionsFor: (shown: LifecycleRow) => RowAction[] } = { actionsFor: () => [] };
  function List() {
    const stacked = [...page.native].sort().join("\n");
    const mergesOnGitHub = useCallback(
      (url: string) => stacked.split("\n").includes(url),
      [stacked],
    );
    const rows = useRowLifecycleActions(SESSION, page.enabled, listed, page.scope, mergesOnGitHub);
    latest.actionsFor = (shown) => {
      listed.set(shown.url, shown);
      return rows.actionsFor(shown);
    };
    return rows.dialog;
  }
  // The reader the list reads as, a new value only when `page.reader` changed.
  let readAs = { reader: page.reader };
  const ui = (value: typeof readAs) => (
    <QueryClientProvider client={queryClient}>
      <MacReadAsContext.Provider value={value}>
        <TooltipProvider>
          <List />
        </TooltipProvider>
      </MacReadAsContext.Provider>
    </QueryClientProvider>
  );
  const view = render(ui(readAs));
  const enableOf = (shown: LifecycleRow) =>
    latest.actionsFor(shown).find((action) => action.id === "lifecycle:enable_auto_merge");
  const choose = (shown: LifecycleRow = row()) => act(() => enableOf(shown)!.run());
  const refresh = () => {
    if (readAs.reader !== page.reader) readAs = { reader: page.reader };
    view.rerender(ui(readAs));
  };
  return { view, enableOf, choose, latest, page, queryClient, refresh };
}

beforeEach(() => {
  listed.clear();
  optionsAnswers = [OPTIONS];
  currentUser.id = "user-1";
  identityListeners.clear();
  resetDesktopPullRequestWritesForTests();
  resetLifecycleActionsForTests();
  resetStateChainsForTests();
  resetChangeAttemptsForTests();
  toast.mockReset();
  toastError.mockReset();
});

afterEach(() => {
  cleanup();
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  resetLifecycleActionsForTests();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  githubMacReadsPreference.write("automatic");
});

describe("which rows offer Enable Auto-merge…", () => {
  it("offers it on an open, ready PR whose auto-merge is off or can't be told", async () => {
    await installShell({});
    const { enableOf } = renderList();
    expect(enableOf(row())).toMatchObject({
      label: "Enable auto-merge…",
      paletteLabel: "Enable Auto-merge for #42 Fix the build…",
      section: "lifecycle",
    });
    // An older shell's read says nothing of auto-merge: the dialog reads it.
    expect(
      enableOf({ ...row(), pr: { state: "OPEN", is_draft: false, head_sha: HEAD } }),
    ).toBeDefined();
    expect(enableOf(row({ auto_merge: ON }))).toBeUndefined();
    expect(enableOf(row({ is_draft: true }))).toBeUndefined();
    expect(enableOf(row({ state: "CLOSED" }))).toBeUndefined();
    expect(enableOf(row({ head_sha: undefined }))).toBeUndefined();
  });

  it("offers it on GitHub where the shell can't turn it on", async () => {
    await installShell({}, []);
    const { enableOf } = renderList();
    expect(enableOf(row())).toMatchObject({ label: "Enable auto-merge on GitHub", href: PR });
  });
});

describe("a row's Enable Auto-merge… dialog", () => {
  it("reads how the PR merges, then turns it on with the method chosen, pinned to its head", async () => {
    const sent = await installShell({ pull_request_auto_merge_enable: enabled });
    const { choose } = renderList();
    choose();
    // Until this Mac's read answers, it can't ask.
    expect(screen.getByRole("dialog")).toHaveTextContent("Reading how #42 merges…");
    fireEvent.click(await screen.findByRole("button", { name: "Continue…" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toMatchObject({
      operation: "pull_request_auto_merge_enable",
      params: { repository: "acme/app", number: 42, merge_method: "squash", expected_head: HEAD },
    });
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(
        "Turned on auto-merge for #42 as @mac-octo",
        expect.objectContaining({ action: undefined }),
      ),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("asks nothing itself where the repository allows one method: main's sheet is the only question", async () => {
    optionsAnswers = [{ ...OPTIONS, methods: ["squash"], default: "squash" }];
    const sent = await installShell({ pull_request_auto_merge_enable: enabled });
    const { choose } = renderList();
    choose();
    // The read first; then it goes straight to the shell, which shows main's own sheet.
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toMatchObject({
      operation: "pull_request_auto_merge_enable",
      params: { merge_method: "squash", expected_head: HEAD },
    });
    expect(screen.queryByRole("button", { name: "Continue…" })).toBeNull();
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("asks only how it merges where the repository allows several, never whether", async () => {
    const sent = await installShell({ pull_request_auto_merge_enable: enabled });
    const { choose } = renderList();
    choose();
    const dialog = await screen.findByRole("dialog", { name: "Choose how #42 merges" });
    expect(dialog).toHaveTextContent("You confirm it next.");
    expect(sent).toEqual([]);
  });

  it("sends nothing on Cancel", async () => {
    const sent = await installShell({ pull_request_auto_merge_enable: enabled });
    const { choose } = renderList();
    choose();
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(sent).toEqual([]);
  });

  it.each([
    ["already on", { auto_merge: ON }, "Auto-merge is already on."],
    ["through a merge queue", { merge_queue: true }, "merges through a merge queue"],
    ["a queue GitHub can't tell", { merge_queue: null }, "couldn’t say whether its base branch"],
    ["able to merge now", { merge_state_status: "CLEAN" }, "can merge now. Use Merge instead."],
  ])("says why it can't, %s, with OK, and sends nothing", async (_label, over, text) => {
    optionsAnswers = [{ ...OPTIONS, ...over }];
    const sent = await installShell({ pull_request_auto_merge_enable: enabled });
    const { choose } = renderList();
    choose();
    expect(await screen.findByText(new RegExp(text.replace(/[.?]/g, "\\$&")))).toBeVisible();
    expect(screen.queryByRole("button", { name: "Continue…" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "OK" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(sent).toEqual([]);
  });

  it("says a failed read, and asks once Try Again reads it", async () => {
    optionsAnswers = [new Error("GitHub is busy."), OPTIONS];
    await installShell({ pull_request_auto_merge_enable: enabled });
    const { choose } = renderList();
    choose();
    expect(await screen.findByText(/Couldn’t read how #42 merges/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Try Again" }));
    expect(await screen.findByRole("button", { name: "Continue…" })).toBeVisible();
  });

  it("sends nothing once the branch moved under the open dialog, and says so", async () => {
    const sent = await installShell({ pull_request_auto_merge_enable: enabled });
    const { choose, latest } = renderList();
    choose();
    await screen.findByRole("button", { name: "Continue…" });
    latest.actionsFor(row({ head_sha: MOVED }));
    fireEvent.click(screen.getByRole("button", { name: "Continue…" }));
    expect(toast).toHaveBeenLastCalledWith(
      expect.stringContaining("#42 wasn’t changed: its branch moved since."),
      expect.anything(),
    );
    expect(sent).toEqual([]);
  });

  it("sends nothing once another workspace user signed in under the open dialog", async () => {
    const sent = await installShell({ pull_request_auto_merge_enable: enabled });
    const { choose } = renderList();
    choose();
    await screen.findByRole("button", { name: "Continue…" });
    act(() => {
      currentUser.id = "user-2";
      for (const listener of identityListeners) listener();
    });
    const turnOn = screen.queryByRole("button", { name: "Continue…" });
    if (turnOn) fireEvent.click(turnOn);
    await act(async () => {});
    expect(sent).toEqual([]);
  });

  it("sends nothing from cached options once a fresh read of them failed", async () => {
    const sent = await installShell({ pull_request_auto_merge_enable: enabled });
    const { choose, queryClient } = renderList();
    choose();
    const turnOn = await screen.findByRole("button", { name: "Continue…" });
    optionsAnswers = [new Error("GitHub is busy.")];
    await act(() => queryClient.refetchQueries({ queryKey: ["github-merge-options"] }));
    // Clicked before the dialog shows the failure, it checks the read as it stands.
    fireEvent.click(turnOn);
    await act(async () => {});
    expect(sent).toEqual([]);
    expect(await screen.findByText(/Couldn’t read how #42 merges: GitHub is busy./)).toBeVisible();
    expect(screen.getByRole("button", { name: "Try Again" })).toBeVisible();
  });

  it.each([
    ["the list's scope changed", (page: { scope?: string }) => (page.scope = "page-two")],
    ["row changes were turned off", (page: { enabled: boolean }) => (page.enabled = false)],
  ])("retires for good once %s, even changed back", async (_label, change) => {
    const sent = await installShell({ pull_request_auto_merge_enable: enabled });
    const list = renderList("page-one");
    list.choose();
    await screen.findByRole("button", { name: "Continue…" });
    const before = { ...list.page };
    change(list.page);
    list.refresh();
    expect(screen.queryByRole("dialog")).toBeNull();
    Object.assign(list.page, before);
    list.refresh();
    await act(async () => {});
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(sent).toEqual([]);
  });

  it("retires for good once the list reads as another account, even back on Automatic", async () => {
    const shell = installNamedWriteShell(() => ({ ...enabled(), actor: { login: "alice" } }), {
      extraWrites: LIFECYCLE_WRITE_OPERATIONS,
      reads: { pull_request_merge_options: () => OPTIONS },
    });
    await shell.ready(PR);
    setLifecycleFlagForTests(true);
    const list = renderList();
    list.choose();
    await screen.findByRole("button", { name: "Continue…" });
    list.page.reader = BOB;
    list.refresh();
    // Leaving Bob as the account store does: Automatic's own ticket stays valid throughout.
    leaveNamedReader(BOB);
    list.page.reader = null;
    list.refresh();
    await act(async () => {});
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(shell.codeHostWrite).not.toHaveBeenCalled();
    expect(shell.codeHostWriteAs).not.toHaveBeenCalled();
  });

  it("goes with its list, sending nothing", async () => {
    const sent = await installShell({ pull_request_auto_merge_enable: enabled });
    const { choose, view } = renderList();
    choose();
    await screen.findByRole("dialog");
    view.unmount();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(sent).toEqual([]);
  });

  it("asks again on Try Again after a refusal, rather than sending again", async () => {
    let answer: unknown = refusal("rejected", "GitHub is busy.");
    const sent = await installShell({ pull_request_auto_merge_enable: () => answer });
    const { choose } = renderList();
    choose();
    fireEvent.click(await screen.findByRole("button", { name: "Continue…" }));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "#42: Couldn’t turn on auto-merge: GitHub is busy.",
        expect.anything(),
      ),
    );
    const options = toastError.mock.calls.at(-1)![1] as { action: { onClick: () => void } };
    answer = enabled();
    act(() => (options.action.onClick as (event: unknown) => void)({ preventDefault: () => {} }));
    expect(await screen.findByRole("button", { name: "Continue…" })).toBeVisible();
    expect(sent).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Continue…" }));
    await waitFor(() => expect(sent).toHaveLength(2));
  });
});

describe("a row's Enable Auto-merge… for a PR in GitHub's own stack", () => {
  it("isn't offered where GitHub merges the PR with its stack", async () => {
    await installShell({ pull_request_auto_merge_enable: enabled });
    const { enableOf, page, refresh } = renderList();
    page.native.add(PR);
    refresh();
    expect(enableOf(row())).toBeUndefined();
  });

  it("retires an open dialog once GitHub says it stacks the PR, sending nothing, for good", async () => {
    const sent = await installShell({ pull_request_auto_merge_enable: enabled });
    const { choose, page, refresh } = renderList();
    choose();
    await screen.findByRole("dialog", { name: "Choose how #42 merges" });
    page.native.add(PR);
    refresh();
    expect(screen.queryByRole("dialog")).toBeNull();
    // Merging here again never brings the retired question back.
    page.native.delete(PR);
    refresh();
    await act(async () => {});
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(sent).toEqual([]);
  });

  it("asks nothing from an action kept from before GitHub said it stacks the PR", async () => {
    const sent = await installShell({ pull_request_auto_merge_enable: enabled });
    const { enableOf, page, refresh } = renderList();
    const retained = enableOf(row())!;
    page.native.add(PR);
    refresh();
    act(() => retained.run());
    await act(async () => {});
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(sent).toEqual([]);
  });
});

describe("autoMergeRefusedBy", () => {
  const pr = { url: PR, headSha: HEAD };
  it("lets it be turned on for a blocked or behind PR on a base without a queue", () => {
    expect(autoMergeRefusedBy(OPTIONS, pr)).toBeNull();
    expect(autoMergeRefusedBy({ ...OPTIONS, merge_state_status: "BEHIND" }, pr)).toBeNull();
  });

  it("says why for each state it can't be turned on in", () => {
    expect(autoMergeRefusedBy({ ...OPTIONS, methods: [], default: null }, pr)).toBe(
      "The repository allows no merge method to turn auto-merge on with.",
    );
    expect(autoMergeRefusedBy({ ...OPTIONS, merge_state_status: "UNSTABLE" }, pr)).toMatch(
      /can merge now/,
    );
    expect(autoMergeRefusedBy({ ...OPTIONS, merge_state_status: "DIRTY" }, pr)).toBe(
      "GitHub can’t turn on auto-merge for it now. Check it on GitHub.",
    );
  });
});
