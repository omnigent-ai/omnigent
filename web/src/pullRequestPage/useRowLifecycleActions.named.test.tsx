// A row's Convert to draft and Ready for review on the Pull Requests page while it reads as one
// of this Mac's logins: each is made at once, only as the account its click captured, and its
// Undo only as that same account. Switching the page's account before Undo, in either direction,
// a login that signed in again, or a shell that can't write as the login, sends nothing.
// A refusal's Try Again is made only as the refused change was: same account, sign-in and user.

import type * as IdentityModule from "@/lib/identity";
import type * as SonnerModule from "sonner";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { type ReactNode, useMemo } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { hostFetch, workspaceUser, toast, toastError } = vi.hoisted(() => ({
  hostFetch: vi.fn(),
  workspaceUser: { id: "user-1" },
  toast: vi.fn(),
  toastError: vi.fn(),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => workspaceUser.id,
  subscribeIdentity: () => () => {},
  authenticatedFetch: hostFetch,
}));
vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast: Object.assign(toast, { success: vi.fn(), error: toastError, dismiss: vi.fn() }),
}));

import { MacReadAsContext } from "@/hooks/macReadAs";
import {
  useEnablePullRequestAutoMerge,
  usePullRequestAutoMergeDisableChange,
  usePullRequestDraftChange,
} from "@/newPullRequestPage/hooks/usePullRequests";
import {
  LIFECYCLE_WRITE_OPERATIONS,
  resetDesktopPullRequestWritesForTests,
  setLifecycleFlagForTests,
  setMacWritesReleaseFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import { resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import { type MacNamedReader, resetMacNamedReadsForTests } from "@/lib/macNamedReads";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";
import { BOB, installNamedWriteShell, removeNamedWriteShell } from "@/test/macNamedWriteShell";

import { sendLifecycleChange } from "./lifecycleRequests";
import { lifecycleActionState, resetLifecycleActionsForTests } from "./pullRequestLifecycleActions";
import { resetStateChainsForTests, stateChainKey } from "./pullRequestStateChain";
import { captureMacChange } from "./usePullRequestStateAction";
import { type LifecycleRow, useRowLifecycleActions } from "./useRowLifecycleActions";

const PR = "https://github.com/acme/app/pull/42";
const SESSION = "pull-requests-page";
const KEY = stateChainKey("user-1", PR);
const convertedAs = (login: string) => ({
  ok: true,
  data: { object: "session.github.pr_draft", pr_url: PR, is_draft: true },
  actor: { login },
});
const READY_PR: LifecycleRow = {
  url: PR,
  number: 42,
  paletteName: "#42",
  pr: { state: "OPEN", is_draft: false, head_sha: "a".repeat(40) },
};

/** The page's account, as its read-as menu sets it; a rerender shows the change. */
const page: { reader: MacNamedReader | null } = { reader: null };

/** The rows the list shows now. */
const listed = new Map<string, LifecycleRow>();
function renderRow() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const Wrapper = ({ children }: { children: ReactNode }) => {
    const reader = page.reader;
    const readAsValue = useMemo(() => ({ reader }), [reader]);
    return (
      <QueryClientProvider client={client}>
        <MacReadAsContext.Provider value={readAsValue}>{children}</MacReadAsContext.Provider>
      </QueryClientProvider>
    );
  };
  const rows = renderHook(
    () => {
      const actions = useRowLifecycleActions(SESSION, true, listed);
      // Each row asked for is what the list shows now.
      return {
        actionsFor: (row: LifecycleRow) => {
          listed.set(row.url, row);
          return actions.actionsFor(row);
        },
      };
    },
    { wrapper: Wrapper },
  );
  /** The page now reads as `reader`; the list renders again. */
  const readAs = (reader: MacNamedReader | null) => {
    page.reader = reader;
    rows.rerender();
  };
  const convertNow = () => {
    const convert = rows.result.current
      .actionsFor(READY_PR)
      .find((action) => action.id === "lifecycle:convert_to_draft");
    expect(convert).toBeDefined();
    act(() => convert!.run());
  };
  /** The Undo the last toast offers, once the conversion was made. */
  const undo = async () => {
    await waitFor(() => expect(String(toast.mock.calls.at(-1)?.[0])).toMatch(/^Converted/));
    const options = toast.mock.calls.at(-1)![1] as { action: { onClick: () => void } };
    return () => act(() => options.action.onClick());
  };
  return { rows, readAs, convertNow, undo, wrapper: Wrapper };
}

function expectNothingSent(shell: ReturnType<typeof installNamedWriteShell>) {
  expect(shell.codeHostWrite).not.toHaveBeenCalled();
  expect(shell.codeHostWriteAs).not.toHaveBeenCalled();
  expect(hostFetch).not.toHaveBeenCalled();
}

beforeEach(() => {
  listed.clear();
  page.reader = null;
  workspaceUser.id = "user-1";
  toast.mockReset();
  toastError.mockReset();
  resetStateChainsForTests();
  resetLifecycleActionsForTests();
  setLifecycleFlagForTests(true);
});

afterEach(() => {
  hostFetch.mockReset();
  setLifecycleFlagForTests(null);
  setMacWritesReleaseFlagForTests(null);
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  resetLifecycleActionsForTests();
  removeNamedWriteShell();
});

describe("a row's Convert to draft as the page's account", () => {
  it("converts at once as the login the page reads as, and says so", async () => {
    const shell = installNamedWriteShell(() => convertedAs("bob"), {
      extraWrites: LIFECYCLE_WRITE_OPERATIONS,
    });
    await shell.ready(PR);
    page.reader = BOB;
    const { convertNow, undo } = renderRow();
    convertNow();
    await waitFor(() => expect(shell.codeHostWriteAs).toHaveBeenCalledTimes(1));
    expect(shell.codeHostWriteAs.mock.calls[0]![0]).toMatchObject({
      operation: "pull_request_convert_to_draft",
      macLogin: "bob",
    });
    expect(shell.codeHostWrite).not.toHaveBeenCalled();
    await undo();
    expect(toast.mock.calls.at(-1)![0]).toBe("Converted #42 to a draft as @bob");
  });

  it.each([
    ["Automatic to a login by name", null, BOB],
    ["a login by name to Automatic", BOB, null],
  ] as const)(
    "sends no Undo once the page switched from %s, even once switched back",
    async (_label, from, to) => {
      const login = from ? "bob" : "alice";
      const shell = installNamedWriteShell(() => convertedAs(login), {
        extraWrites: LIFECYCLE_WRITE_OPERATIONS,
      });
      await shell.ready(PR);
      page.reader = from;
      const { convertNow, undo, readAs } = renderRow();
      convertNow();
      const runUndo = await undo();
      const writes = () =>
        shell.codeHostWrite.mock.calls.length + shell.codeHostWriteAs.mock.calls.length;
      expect(writes()).toBe(1);
      readAs(to);
      runUndo();
      await act(async () => {});
      expect(toast.mock.calls.at(-1)![0]).toBe(
        "#42 wasn’t changed back: the account changed since.",
      );
      expect(writes()).toBe(1);
    },
  );

  it("sends no Undo once the login signed in again since the conversion", async () => {
    const shell = installNamedWriteShell(() => convertedAs("bob"), {
      extraWrites: LIFECYCLE_WRITE_OPERATIONS,
    });
    await shell.ready(PR);
    page.reader = BOB;
    const { convertNow, undo } = renderRow();
    convertNow();
    const runUndo = await undo();
    shell.bobSignsInAgain(7);
    await shell.readAsBob(PR);
    runUndo();
    await act(async () => {});
    expect(shell.codeHostWriteAs).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["an older shell without writes by name", { writeAs: false }],
    ["a partial bridge without the method", { writeAsMethod: false }],
  ] as const)("offers nothing as a login by name on %s", async (_label, options) => {
    const shell = installNamedWriteShell(() => convertedAs("alice"), {
      extraWrites: LIFECYCLE_WRITE_OPERATIONS,
      ...options,
    });
    await shell.ready(PR);
    page.reader = BOB;
    const { rows } = renderRow();
    expect(rows.result.current.actionsFor(READY_PR)).toEqual([]);
    expect(
      rows.result.current.actionsFor({ ...READY_PR, pr: { ...READY_PR.pr!, is_draft: true } }),
    ).toEqual([]);
    expectNothingSent(shell);
  });
});

describe("a row's Disable auto-merge as the page's account", () => {
  const ON_PR: LifecycleRow = {
    ...READY_PR,
    pr: {
      ...READY_PR.pr!,
      auto_merge: { merge_method: "SQUASH", enabled_by: null, enabled_at: null },
    },
  };
  const disableOf = (actions: { id: string; run: () => void }[]) =>
    actions.find((action) => action.id === "lifecycle:disable_auto_merge");
  const offAs = (login: string) => ({
    ok: true,
    data: { object: "session.github.pr_merge", pr_url: PR },
    actor: { login },
  });

  it("turns it off as the login the page reads as, without the lifecycle writes", async () => {
    // Turning auto-merge off is in the v1 set, so a shell without the lifecycle writes has it.
    const shell = installNamedWriteShell(() => offAs("bob"));
    await shell.ready(PR);
    page.reader = BOB;
    const { rows } = renderRow();
    act(() => disableOf(rows.result.current.actionsFor(ON_PR))!.run());
    await waitFor(() => expect(shell.codeHostWriteAs).toHaveBeenCalledTimes(1));
    expect(shell.codeHostWriteAs.mock.calls[0]![0]).toMatchObject({
      operation: "pull_request_auto_merge_disable",
      macLogin: "bob",
    });
    await waitFor(() =>
      expect(toast.mock.calls.at(-1)![0]).toBe("Turned off auto-merge for #42 as @bob"),
    );
    expect(shell.codeHostWrite).not.toHaveBeenCalled();
    expect(hostFetch).not.toHaveBeenCalled();
  });

  it.each([
    ["an older shell without writes by name", { writeAs: false }],
    ["a partial bridge without the method", { writeAsMethod: false }],
  ] as const)("offers nothing as a login by name on %s", async (_label, options) => {
    const shell = installNamedWriteShell(() => offAs("alice"), options);
    await shell.ready(PR);
    page.reader = BOB;
    const { rows } = renderRow();
    expect(disableOf(rows.result.current.actionsFor(ON_PR))).toBeUndefined();
    expectNothingSent(shell);
  });
});

describe("a draft change once the page reads as another account than its click", () => {
  it.each([
    ["Automatic to a login by name", null, BOB],
    ["a login by name to Automatic", BOB, null],
  ] as const)("is skipped from %s, sending nothing", async (_label, from, to) => {
    const shell = installNamedWriteShell(() => convertedAs("alice"), {
      extraWrites: LIFECYCLE_WRITE_OPERATIONS,
    });
    await shell.ready(PR);
    page.reader = from;
    const { wrapper } = renderRow();
    // The mutations rendered at the click still route as before; the page moved on since.
    const writes = renderHook(
      () => ({
        convert: usePullRequestDraftChange(SESSION, "pull_request_convert_to_draft"),
        ready: usePullRequestDraftChange(SESSION, "pull_request_ready_for_review"),
        enable: useEnablePullRequestAutoMerge(SESSION),
        disable: usePullRequestAutoMergeDisableChange(SESSION),
      }),
      { wrapper },
    ).result.current;
    const capture = captureMacChange(PR, "user-1", "pull_request_convert_to_draft", from)!;
    expect(capture).not.toBeNull();
    const outcome = await sendLifecycleChange(
      { key: KEY, owner: "user-1", prUrl: PR, number: 42 },
      "convert_to_draft",
      { capture },
      { ...writes, currentReader: () => to, toasts: false, origin: null },
    );
    expect(outcome).toBe("skipped");
    expect(lifecycleActionState(KEY).notice?.outcome.kind).toBe("skipped");
    expectNothingSent(shell);
  });
});

const DRAFT_PR: LifecycleRow = { ...READY_PR, pr: { ...READY_PR.pr!, is_draft: true } };
const refused = { ok: false, error: { code: "rejected", message: "GitHub is busy." } };
const readyAs = (login: string) => ({
  ok: true,
  data: { object: "session.github.pr_draft", pr_url: PR, is_draft: false },
  actor: { login },
});

/** Ready for review on the draft row, refused once, then answered by `then`. */
async function refusedReady(then: unknown) {
  const answers = [refused, then];
  const shell = installNamedWriteShell(() => answers.shift() ?? then, {
    extraWrites: LIFECYCLE_WRITE_OPERATIONS,
  });
  await shell.ready(PR);
  const view = renderRow();
  const ready = view.rows.result.current
    .actionsFor(DRAFT_PR)
    .find((action) => action.id === "lifecycle:ready_for_review");
  act(() => ready!.run());
  await waitFor(() => expect(toastError).toHaveBeenCalled());
  const options = toastError.mock.calls.at(-1)![1] as { action: { onClick: () => void } };
  /** The refusal toast's Try Again, once the list has rendered as it is now. */
  const tryAgain = async () => {
    view.rows.rerender();
    view.rows.result.current.actionsFor(DRAFT_PR);
    act(() => options.action.onClick());
    await act(async () => {
      await new Promise<void>((resolve) => {
        setTimeout(resolve, 50);
      });
    });
  };
  return { shell, view, tryAgain };
}

const SKIPPED =
  "#42: Couldn’t mark ready for review, because the account or the pull request changed. Nothing was sent.";

describe("a row's Try Again after a refusal", () => {
  it("sends once more as the same account while nothing changed", async () => {
    const { shell, tryAgain } = await refusedReady(readyAs("alice"));
    expect(shell.codeHostWrite).toHaveBeenCalledTimes(1);
    await tryAgain();
    expect(shell.codeHostWrite).toHaveBeenCalledTimes(2);
    expect(shell.codeHostWrite.mock.calls[1]![0]).toMatchObject({
      operation: "pull_request_ready_for_review",
    });
    expect(shell.codeHostWriteAs).not.toHaveBeenCalled();
  });

  it("sends nothing once the page reads as a login by name, and says why", async () => {
    const { shell, view, tryAgain } = await refusedReady(readyAs("bob"));
    view.readAs(BOB);
    await tryAgain();
    expect(shell.codeHostWrite).toHaveBeenCalledTimes(1);
    expect(shell.codeHostWriteAs).not.toHaveBeenCalled();
    expect(toast).toHaveBeenLastCalledWith(SKIPPED, expect.anything());
  });

  it("sends nothing once the page reads as gh's account again, after a refusal by name", async () => {
    page.reader = BOB;
    const { shell, view, tryAgain } = await refusedReady(readyAs("alice"));
    expect(shell.codeHostWriteAs).toHaveBeenCalledTimes(1);
    view.readAs(null);
    await tryAgain();
    expect(shell.codeHostWriteAs).toHaveBeenCalledTimes(1);
    expect(shell.codeHostWrite).not.toHaveBeenCalled();
    expect(toast).toHaveBeenLastCalledWith(SKIPPED, expect.anything());
  });

  it("sends nothing once the login signed in again since the refusal", async () => {
    page.reader = BOB;
    const { shell, tryAgain } = await refusedReady(readyAs("bob"));
    shell.bobSignsInAgain(7);
    await shell.readAsBob(PR);
    await tryAgain();
    expect(shell.codeHostWriteAs).toHaveBeenCalledTimes(1);
    expect(shell.codeHostWrite).not.toHaveBeenCalled();
  });

  it("sends nothing once another workspace user is signed in", async () => {
    const { shell, tryAgain } = await refusedReady(readyAs("alice"));
    workspaceUser.id = "user-2";
    await tryAgain();
    expect(shell.codeHostWrite).toHaveBeenCalledTimes(1);
    expect(shell.codeHostWriteAs).not.toHaveBeenCalled();
  });
});
