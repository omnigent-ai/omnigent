// Ready for review, Convert to draft and Disable auto-merge on a Pull Requests list's rows, through the real write
// route, hooks and store over a fake shell: which rows offer what, what each sends, the list's
// own confirmation, the hold an unknown outcome puts on the row and the PR page alike, and the
// toasts that say what each came to, with Try Again and Check Again.

import type * as IdentityModule from "@/lib/identity";
import type * as SonnerModule from "sonner";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
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

import { MacReadAsContext } from "@/hooks/macReadAs";
import {
  type PullRequestActionOrigin,
  PullRequestActionOriginContext,
} from "@/lib/pullRequestActionOrigin";
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
import type { MacNamedReader } from "@/lib/macNamedReads";
import type { RowAction } from "@/shell/PullRequestListRows";
import { isCommandAvailable } from "@/store/paletteCommands";

import { lifecycleHeld } from "./lifecycleRequests";
import { pullRequestChangeInFlight } from "./pullRequestChangeInFlight";
import { resetLifecycleActionsForTests } from "./pullRequestLifecycleActions";
import { resetStateChainsForTests, stateChainKey } from "./pullRequestStateChain";
import {
  type LifecycleRow,
  rowLifecycleAvailability,
  useRowLifecycleActions,
} from "./useRowLifecycleActions";
import { EXAMPLE_SPOKE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// GitSpoke stand-in hosts are recognized by name, as a build that registers GitSpoke does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_SPOKE_HOST] });

const PR = "https://github.com/acme/app/pull/42";
const HEAD = "a".repeat(40);
const SESSION = "pull-requests-page";

const ok = (data: unknown) => ({ ok: true, data, actor: { login: "mac-octo" } });
const refusal = (code: string, message: string) => ({ ok: false, error: { code, message } });
const draftAnswer = (isDraft: boolean) =>
  ok({ object: "session.github.pr_draft", pr_url: PR, is_draft: isDraft });
const autoMergeOffAnswer = () => ok({ object: "session.github.pr_merge", pr_url: PR });
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

const AUTO_MERGE_ON = {
  merge_method: "SQUASH",
  enabled_by: "amy",
  enabled_at: "2026-11-05T10:00:00Z",
};

/** A shell reading and writing GitHub as `mac-octo`, answering each write from `answers`. */
async function installShell(answers: Record<string, () => unknown>) {
  const sent: string[] = [];
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: vi.fn(async () => ({ ok: true, data: [] })),
    codeHostWrite: vi.fn(async (request: { operation: string }) => {
      sent.push(request.operation);
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
          operations: GITHUB_MAC_READ_OPERATIONS,
          accounts: true,
          writes: {
            version: 1,
            operations: [...GITHUB_WRITE_SET_V1, ...LIFECYCLE_WRITE_OPERATIONS],
          },
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

function row(pr: Partial<NonNullable<LifecycleRow["pr"]>> | null = {}): LifecycleRow {
  return {
    url: PR,
    number: 42,
    paletteName: "#42 Fix the build",
    pr: pr === null ? undefined : { state: "OPEN", is_draft: false, head_sha: HEAD, ...pr },
  };
}

/** The rows the list shows now. */
const listed = new Map<string, LifecycleRow>();
const AUTOMATIC = { reader: null };
function renderRows(
  readAs: { reader: MacNamedReader | null } = AUTOMATIC,
  /** The Pull Requests page the list is on; null for a list elsewhere. */
  origin: PullRequestActionOrigin | null = null,
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <PullRequestActionOriginContext.Provider value={origin}>
        <MacReadAsContext.Provider value={readAs}>{children}</MacReadAsContext.Provider>
      </PullRequestActionOriginContext.Provider>
    </QueryClientProvider>
  );
  return renderHook(
    () => {
      const rows = useRowLifecycleActions(SESSION, true, listed);
      // Each row asked for is what the list shows now.
      return {
        actionsFor: (shown: LifecycleRow) => {
          listed.set(shown.url, shown);
          return rows.actionsFor(shown);
        },
      };
    },
    { wrapper: wrapper },
  );
}

beforeEach(() => {
  listed.clear();
  currentUser.id = "user-1";
  identityListeners.clear();
  resetDesktopPullRequestWritesForTests();
  resetLifecycleActionsForTests();
  resetStateChainsForTests();
  toast.mockReset();
  toastError.mockReset();
});

afterEach(() => {
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetLifecycleActionsForTests();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  githubMacReadsPreference.write("automatic");
});

describe("which rows offer what", () => {
  const facts = { onMac: true, offered: () => true, held: false };

  it("offers Ready for review on an open draft and Convert to draft on an open, ready PR", () => {
    expect(rowLifecycleAvailability("ready_for_review", row({ is_draft: true }), facts)).toEqual({
      kind: "mac",
    });
    expect(rowLifecycleAvailability("convert_to_draft", row({ is_draft: true }), facts)).toEqual({
      kind: "hidden",
    });
    expect(rowLifecycleAvailability("convert_to_draft", row(), facts)).toEqual({ kind: "mac" });
    expect(rowLifecycleAvailability("ready_for_review", row(), facts)).toEqual({ kind: "hidden" });
  });

  it.each([
    ["closed", { state: "CLOSED" }],
    ["merged", { state: "MERGED" }],
    ["a closed draft", { state: "CLOSED", is_draft: true }],
  ])("offers neither on a %s PR", (_name, pr) => {
    for (const id of ["ready_for_review", "convert_to_draft"] as const) {
      expect(rowLifecycleAvailability(id, row(pr), facts)).toEqual({ kind: "hidden" });
    }
  });

  it("offers nothing while the row's read hasn't loaded", () => {
    expect(rowLifecycleAvailability("convert_to_draft", row(null), facts)).toEqual({
      kind: "hidden",
    });
  });

  it("offers GitHub where this Mac can't make it, and says why a held one waits", () => {
    expect(
      rowLifecycleAvailability("convert_to_draft", row(), { ...facts, offered: () => false }),
    ).toEqual({ kind: "github", url: PR });
    expect(rowLifecycleAvailability("convert_to_draft", row(), { ...facts, held: true }).kind).toBe(
      "held",
    );
  });

  it("offers nothing this Mac can't make on another provider's PR, auto-merge included", () => {
    const elsewhere = { onMac: false, offered: () => false, held: false, onGitHub: false };
    expect(rowLifecycleAvailability("convert_to_draft", row(), elsewhere)).toEqual({
      kind: "hidden",
    });
    expect(rowLifecycleAvailability("enable_auto_merge", row(), elsewhere)).toEqual({
      kind: "hidden",
    });
    expect(
      rowLifecycleAvailability("enable_auto_merge", row(), { ...elsewhere, onGitHub: undefined }),
    ).toEqual({ kind: "github", url: PR });
  });
});

describe("a GitSpoke row's changes", () => {
  it("shows each change unavailable, saying where to make it, and links nowhere", async () => {
    const sent = await installShell({ pull_request_ready_for_review: () => draftAnswer(false) });
    const { result } = renderRows();
    const spokeUrl = "https://2.spoke.example.com/example-org/universe/pull/20002346";
    const actions = result.current.actionsFor({ ...row({ is_draft: true }), url: spokeUrl });
    const ready = actions.find((action) => action.id === "lifecycle:ready_for_review");
    expect(ready?.availability).toEqual(
      expect.objectContaining({ reason: "Change this pull request on GitSpoke." }),
    );
    // The row's own Open on GitSpoke takes it there; these add no link of their own.
    expect(actions.every((action) => action.href === undefined)).toBe(true);
    expect(sent).toEqual([]);
  });
});

describe("a row's Ready for review and Convert to draft", () => {
  it("marks a draft ready at once, named for its PR in ⌘K", async () => {
    const sent = await installShell({ pull_request_ready_for_review: () => draftAnswer(false) });
    const { result } = renderRows();
    const [ready] = result.current.actionsFor(row({ is_draft: true }));
    expect(ready).toMatchObject({
      id: "lifecycle:ready_for_review",
      section: "lifecycle",
      label: "Ready for review",
      paletteLabel: "Mark #42 Fix the build Ready for Review",
    });
    act(() => ready.run());
    await waitFor(() => expect(sent).toEqual(["pull_request_ready_for_review"]));
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(
        "Marked #42 ready for review as @mac-octo",
        expect.anything(),
      ),
    );
  });

  it("converts at once with no dialog, and Undo marks it ready again as the same account", async () => {
    const sent = await installShell({
      pull_request_convert_to_draft: () => draftAnswer(true),
      pull_request_ready_for_review: () => draftAnswer(false),
    });
    const { result } = renderRows();
    const [convert] = result.current.actionsFor(row());
    expect(convert).toMatchObject({
      id: "lifecycle:convert_to_draft",
      label: "Convert to draft",
      paletteLabel: "Convert #42 Fix the build to Draft",
    });
    expect(convert.movesFocus).toBeUndefined();
    act(() => convert.run());
    await waitFor(() => expect(sent).toEqual(["pull_request_convert_to_draft"]));
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(
        "Converted #42 to a draft as @mac-octo",
        expect.anything(),
      ),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    // The row reads as a draft now.
    result.current.actionsFor(row({ is_draft: true }));
    act(() => toastAction(toast, "Undo")());
    await waitFor(() =>
      expect(sent).toEqual(["pull_request_convert_to_draft", "pull_request_ready_for_review"]),
    );
    await waitFor(() =>
      expect(toast).toHaveBeenLastCalledWith(
        "Marked #42 ready for review as @mac-octo",
        expect.anything(),
      ),
    );
  });

  it("keeps the row converting through a second click, which sends nothing", async () => {
    let answer!: (value: unknown) => void;
    const sent = await installShell({
      pull_request_convert_to_draft: () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    });
    const { result } = renderRows();
    const inFlight = () => pullRequestChangeInFlight(stateChainKey("user-1", PR))?.label;
    act(() => result.current.actionsFor(row())[0].run());
    await waitFor(() => expect(sent).toEqual(["pull_request_convert_to_draft"]));
    expect(inFlight()).toBe("Converting…");
    act(() => result.current.actionsFor(row())[0].run());
    expect(inFlight()).toBe("Converting…");
    await act(async () => answer(draftAnswer(true)));
    await waitFor(() => expect(inFlight()).toBeUndefined());
    expect(sent).toEqual(["pull_request_convert_to_draft"]);
  });

  it("sends no Undo once another workspace user signed in", async () => {
    const sent = await installShell({
      pull_request_convert_to_draft: () => draftAnswer(true),
      pull_request_ready_for_review: () => draftAnswer(false),
    });
    const { result } = renderRows();
    act(() => result.current.actionsFor(row())[0].run());
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(expect.stringMatching(/^Converted/), expect.anything()),
    );
    const undo = toastAction(toast, "Undo");
    act(() => {
      currentUser.id = "user-2";
      for (const listener of [...identityListeners]) listener();
    });
    act(() => undo());
    expect(toast).toHaveBeenLastCalledWith(
      "#42 wasn’t changed back: the account changed since.",
      expect.anything(),
    );
    expect(sent).toEqual(["pull_request_convert_to_draft"]);
  });

  it("holds the row, and the PR page, after an unknown outcome", async () => {
    const sent = await installShell({
      pull_request_ready_for_review: () =>
        refusal("outcome_unknown", "GitHub may have made this change."),
    });
    const { result } = renderRows();
    act(() => result.current.actionsFor(row({ is_draft: true }))[0].run());
    await waitFor(() => expect(lifecycleHeld(stateChainKey("user-1", PR))).toBe(true));
    await waitFor(() =>
      expect(
        isCommandAvailable(result.current.actionsFor(row({ is_draft: true }))[0].availability),
      ).toBe(false),
    );
    // Asking again sends nothing more until a fresh read.
    act(() => result.current.actionsFor(row({ is_draft: true }))[0].run());
    expect(sent).toEqual(["pull_request_ready_for_review"]);
  });
});

/** The action a toast offered, by its label, from the last toast that offered one. */
function toastAction(mock: ReturnType<typeof vi.fn>, label: string): () => void {
  const options = mock.mock.calls
    .map((call) => call[1] as { action?: { label: string; onClick: () => void } } | undefined)
    .filter((option) => option?.action?.label === label)
    .at(-1);
  expect(options).toBeDefined();
  return options!.action!.onClick;
}

describe("what the Pull Requests page records", () => {
  /** The page's origin, recording what each change began as and came to. */
  function pageOrigin() {
    const recorded: string[] = [];
    const origin: PullRequestActionOrigin = {
      begin: vi.fn((prUrl: string, actor: { login: string }) => {
        recorded.push(`began ${prUrl} as @${actor.login}`);
        return { succeeded: (action: string) => recorded.push(action) };
      }),
    };
    return { origin, recorded };
  }

  it("records a conversion once GitHub made it, and its Undo as marked ready", async () => {
    await installShell({
      pull_request_convert_to_draft: () => draftAnswer(true),
      pull_request_ready_for_review: () => draftAnswer(false),
    });
    const { origin, recorded } = pageOrigin();
    const { result } = renderRows(AUTOMATIC, origin);
    act(() => result.current.actionsFor(row())[0].run());
    await waitFor(() =>
      expect(recorded).toEqual([`began ${PR} as @mac-octo`, "converted_to_draft"]),
    );
    result.current.actionsFor(row({ is_draft: true }));
    act(() => toastAction(toast, "Undo")());
    await waitFor(() =>
      expect(recorded).toEqual([
        `began ${PR} as @mac-octo`,
        "converted_to_draft",
        `began ${PR} as @mac-octo`,
        "marked_ready",
      ]),
    );
  });

  it("records nothing GitHub refused, and the Try Again that made it once", async () => {
    let answer: unknown = refusal("rejected", "GitHub is busy.");
    await installShell({ pull_request_ready_for_review: () => answer });
    const { origin, recorded } = pageOrigin();
    const { result } = renderRows(AUTOMATIC, origin);
    act(() => result.current.actionsFor(row({ is_draft: true }))[0].run());
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(recorded).toEqual([`began ${PR} as @mac-octo`]);
    answer = draftAnswer(false);
    act(() => toastAction(toastError, "Try Again")());
    await waitFor(() =>
      expect(recorded).toEqual([
        `began ${PR} as @mac-octo`,
        `began ${PR} as @mac-octo`,
        "marked_ready",
      ]),
    );
  });

  it("records nothing that was skipped before it was sent", async () => {
    await installShell({ pull_request_ready_for_review: () => draftAnswer(false) });
    const { origin, recorded } = pageOrigin();
    const { result } = renderRows(AUTOMATIC, origin);
    const [ready] = result.current.actionsFor(row({ is_draft: true }));
    result.current.actionsFor(row({ is_draft: false }));
    act(() => ready.run());
    await waitFor(() => expect(toast).toHaveBeenCalled());
    expect(recorded).toEqual([]);
  });
});

describe("a row's Disable auto-merge", () => {
  const disableOf = (actions: RowAction[]) =>
    actions.find((action) => action.id === "lifecycle:disable_auto_merge");

  it("is offered only where the row's read says auto-merge is on", async () => {
    await installShell({});
    const { result } = renderRows();
    expect(disableOf(result.current.actionsFor(row({ auto_merge: AUTO_MERGE_ON })))).toMatchObject({
      label: "Disable auto-merge",
      paletteLabel: "Disable Auto-merge for #42 Fix the build",
      section: "lifecycle",
    });
    // Off, or a read that can't say (an older shell's), offers none.
    expect(disableOf(result.current.actionsFor(row({ auto_merge: null })))).toBeUndefined();
    expect(disableOf(result.current.actionsFor(row()))).toBeUndefined();
    expect(
      disableOf(result.current.actionsFor(row({ state: "CLOSED", auto_merge: AUTO_MERGE_ON }))),
    ).toBeUndefined();
  });

  it("is offered without the lifecycle flag, as the merge bar's has always been", async () => {
    await installShell({});
    setLifecycleFlagForTests(false);
    const { result } = renderRows();
    expect(disableOf(result.current.actionsFor(row({ auto_merge: AUTO_MERGE_ON })))).toBeDefined();
  });

  it("turns it off at once as the reader, with no Undo, and records nothing as a draft change", async () => {
    const sent = await installShell({ pull_request_auto_merge_disable: autoMergeOffAnswer });
    const succeeded = vi.fn();
    const origin = { begin: vi.fn(() => ({ succeeded })) } as unknown as PullRequestActionOrigin;
    const { result } = renderRows(AUTOMATIC, origin);
    act(() => disableOf(result.current.actionsFor(row({ auto_merge: AUTO_MERGE_ON })))!.run());
    await waitFor(() => expect(sent).toEqual(["pull_request_auto_merge_disable"]));
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(
        "Turned off auto-merge for #42 as @mac-octo",
        expect.objectContaining({ action: undefined }),
      ),
    );
    expect(origin.begin).not.toHaveBeenCalled();
  });

  it("holds Convert to draft while it runs, and is held while Convert runs", async () => {
    const disabling = deferred<unknown>();
    const converting = deferred<unknown>();
    await installShell({
      pull_request_auto_merge_disable: () => disabling.promise,
      pull_request_convert_to_draft: () => converting.promise,
    });
    const { result } = renderRows();
    const on = row({ auto_merge: AUTO_MERGE_ON });
    act(() => disableOf(result.current.actionsFor(on))!.run());
    await waitFor(() => expect(lifecycleHeld(stateChainKey("user-1", PR))).toBe(true));
    const convert = result.current
      .actionsFor(on)
      .find((action) => action.id === "lifecycle:convert_to_draft")!;
    expect(isCommandAvailable(convert.availability)).toBe(false);
    expect(pullRequestChangeInFlight(stateChainKey("user-1", PR))?.label).toBe(
      "Turning off auto-merge…",
    );
    await act(async () => disabling.resolve(autoMergeOffAnswer()));
    await waitFor(() => expect(lifecycleHeld(stateChainKey("user-1", PR))).toBe(false));
    act(() =>
      result.current
        .actionsFor(on)
        .find((action) => action.id === "lifecycle:convert_to_draft")!
        .run(),
    );
    await waitFor(() => expect(lifecycleHeld(stateChainKey("user-1", PR))).toBe(true));
    expect(isCommandAvailable(disableOf(result.current.actionsFor(on))!.availability)).toBe(false);
    await act(async () => converting.resolve(draftAnswer(true)));
  });

  it("offers Try Again after a refusal, sent again as the account it was refused for", async () => {
    let answer: unknown = refusal("rejected", "GitHub is busy.");
    const sent = await installShell({ pull_request_auto_merge_disable: () => answer });
    const { result } = renderRows();
    act(() => disableOf(result.current.actionsFor(row({ auto_merge: AUTO_MERGE_ON })))!.run());
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "#42: Couldn’t turn off auto-merge: GitHub is busy.",
        expect.anything(),
      ),
    );
    answer = autoMergeOffAnswer();
    act(() => toastAction(toastError, "Try Again")());
    await waitFor(() =>
      expect(sent).toEqual(["pull_request_auto_merge_disable", "pull_request_auto_merge_disable"]),
    );
  });

  it("sends nothing on Try Again once the row no longer reads auto-merge as on", async () => {
    const sent = await installShell({
      pull_request_auto_merge_disable: () => refusal("rejected", "GitHub is busy."),
    });
    const { result } = renderRows();
    act(() => disableOf(result.current.actionsFor(row({ auto_merge: AUTO_MERGE_ON })))!.run());
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    result.current.actionsFor(row({ auto_merge: null }));
    act(() => toastAction(toastError, "Try Again")());
    expect(toast).toHaveBeenLastCalledWith(
      "#42 wasn’t changed: its auto-merge isn’t on any more.",
      expect.anything(),
    );
    expect(sent).toEqual(["pull_request_auto_merge_disable"]);
  });
});

describe("what a row's change came to", () => {
  it("offers Try Again after a refusal, which sends again while the PR is still a draft", async () => {
    let answer: unknown = refusal("rejected", "GitHub is busy.");
    const sent = await installShell({ pull_request_ready_for_review: () => answer });
    const { result } = renderRows();
    act(() => result.current.actionsFor(row({ is_draft: true }))[0].run());
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "#42: Couldn’t mark ready for review: GitHub is busy.",
        expect.anything(),
      ),
    );
    answer = draftAnswer(false);
    act(() => toastAction(toastError, "Try Again")());
    await waitFor(() =>
      expect(sent).toEqual(["pull_request_ready_for_review", "pull_request_ready_for_review"]),
    );
  });

  it("sends nothing on Try Again once the row no longer reads as a draft, and says why", async () => {
    const sent = await installShell({
      pull_request_ready_for_review: () => refusal("rejected", "GitHub is busy."),
    });
    const { result } = renderRows();
    act(() => result.current.actionsFor(row({ is_draft: true }))[0].run());
    await waitFor(() => expect(toastError).toHaveBeenCalled());
    // A fresh read lists it as ready for review now.
    expect(result.current.actionsFor(row()).map((action) => action.id)).not.toContain(
      "lifecycle:ready_for_review",
    );
    act(() => toastAction(toastError, "Try Again")());
    expect(toast).toHaveBeenLastCalledWith(
      "#42 wasn’t changed: it isn’t an open draft any more.",
      expect.anything(),
    );
    expect(sent).toEqual(["pull_request_ready_for_review"]);
  });

  it("offers Check Again after an unknown outcome, on the toast and the row, and never sends again", async () => {
    const sent = await installShell({
      pull_request_ready_for_review: () =>
        refusal("outcome_unknown", "GitHub may have made this change."),
    });
    const { result } = renderRows();
    act(() => result.current.actionsFor(row({ is_draft: true }))[0].run());
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(
        "#42: GitHub may have marked this pull request ready for review. Check it before trying again.",
        expect.objectContaining({ duration: Infinity }),
      ),
    );
    const check = result.current
      .actionsFor(row({ is_draft: true }))
      .find((action) => action.id === "lifecycle:check-again");
    expect(check?.paletteLabel).toBe("Check #42 Fix the build Again");
    await act(async () => toastAction(toast, "Check Again")());
    // This harness can't read the PR afresh, so the toast offers to open it instead.
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "#42: Couldn’t read it from GitHub. Open it to check.",
        expect.objectContaining({ action: expect.objectContaining({ label: "Open" }) }),
      ),
    );
    expect(sent).toEqual(["pull_request_ready_for_review"]);
  });
});
