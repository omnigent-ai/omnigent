// Convert to draft, Ready for review and Enable auto-merge on the new page, through the real
// write route, hooks and store over a fake shell: what each sends and to which head, what it
// leaves shown, the hold an unknown outcome puts on every state change (Merge included) until
// Check Again, and the dialogs and ⌘K rows bound to the page that asked.

import { PullRequestChangesHereContext } from "@/pullRequestPage/pullRequestChangesHere";
import type * as IdentityModule from "@/lib/identity";
import type * as SonnerModule from "sonner";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { type ReactNode, useEffect } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { currentUser, identityListeners, toast, hostFetch } = vi.hoisted(() => ({
  currentUser: { id: "user-1" as string | null },
  identityListeners: new Set<() => void>(),
  toast: vi.fn(),
  hostFetch: vi.fn(),
}));
vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast: Object.assign(toast, { success: vi.fn(), error: vi.fn() }),
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
  type PullRequestInfo,
  useMergePullRequest,
  usePullRequestCommentComposer,
  usePullRequestMergeOptions,
} from "@/newPullRequestPage/hooks/usePullRequests";
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
  readScope,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import {
  type PullRequestActionOrigin,
  PullRequestActionOriginContext,
} from "@/lib/pullRequestActionOrigin";
import { usePaletteCommandsStore } from "@/store/paletteCommands";

import {
  ConvertToDraftPrompt,
  ReadyForReviewPrompt,
  EnableAutoMergeSlot,
  PullRequestChangeNotice,
  PullRequestLifecycleOwner,
  PullRequestStateMenu,
  usePullRequestStatePillProgress,
} from "./PullRequestLifecycleControls";
import {
  LifecycleSurfaceContext,
  lifecycleNoticeMessage,
  resetLifecycleActionsForTests,
} from "./pullRequestLifecycleActions";
import { resetStateChainsForTests } from "./pullRequestStateChain";
import {
  type LifecyclePullRequest,
  usePullRequestLifecycleActions,
} from "./usePullRequestLifecycleActions";

const PR = "https://github.com/acme/app/pull/42";
const HEAD = "a".repeat(40);
const NEWER = "c".repeat(40);
const SESSION = "session-1";

const ok = (data: unknown) => ({ ok: true, data, actor: { login: "mac-octo" } });
const refusal = (code: string, message: string) => ({ ok: false, error: { code, message } });
const draftAnswer = (isDraft: boolean) =>
  ok({ object: "session.github.pr_draft", pr_url: PR, is_draft: isDraft });
const autoMergeAnswer = () =>
  ok({ object: "session.github.pr_auto_merge", pr_url: PR, enabled_at: "2026-11-08T12:00:00Z" });
const MERGE_OPTIONS = {
  object: "session.github.merge_options",
  methods: ["squash", "merge"],
  default: "squash",
  merge_queue: false,
  merge_state_status: "BLOCKED",
  auto_merge: null,
};

function pause(ms: number) {
  return new Promise<void>((resolve) => {
    setTimeout(resolve, ms);
  });
}

interface Sent {
  operation: string;
  params: Record<string, unknown>;
}

/** A shell reading and writing GitHub as `mac-octo`, answering each write from `answers`. */
async function installShell(
  answers: Record<string, () => unknown | Promise<unknown>>,
  {
    mergeOptions = MERGE_OPTIONS as unknown,
    writes = LIFECYCLE_WRITE_OPERATIONS as readonly string[],
  } = {},
) {
  const sent: Sent[] = [];
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: vi.fn(async (request: { operation: string }) =>
      // A copy each time, as a fresh read from GitHub would be.
      request.operation === "pull_request_merge_options"
        ? { ok: true, data: { ...(mergeOptions as object) } }
        : { ok: true, data: [] },
    ),
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
  expect(
    acceptMacAnswer(macReadTicket("github", "github.com"), {
      identity: { host: "github.com", login: "mac-octo", generation: 1 },
    }),
  ).toBe("accepted");
  setMacWritesReleaseFlagForTests(true);
  setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
  setLifecycleFlagForTests(true);
  return sent;
}

const infoKey = () => ["github-info", SESSION, PR, ...readScope(PR)];

function pullRequest(over: Partial<LifecyclePullRequest> = {}): LifecyclePullRequest {
  return {
    conversationId: SESSION,
    prUrl: PR,
    number: 42,
    state: "OPEN",
    isDraft: false,
    headSha: HEAD,
    autoMergeOn: false,
    mergeable: true,
    ...over,
  };
}

/** The PR's read on this Mac, `pr` over an open, ready PR at HEAD. */
function info(pr: Record<string, unknown> = {}) {
  return {
    available: true,
    selected_pr_url: PR,
    pr: {
      url: PR,
      number: 42,
      state: "OPEN",
      is_draft: false,
      head_sha: HEAD,
      comments: [],
      ...pr,
    },
  } as unknown as PullRequestInfo;
}

/** `origin`: the Pull Requests page the PR is shown on; null elsewhere. */
function setup(origin: PullRequestActionOrigin | null = null, changesHere = true) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(infoKey(), info());
  // What the PR's read on this Mac answers when asked again.
  const reads = { scoped: async (): Promise<unknown> => queryClient.getQueryData(infoKey()) };
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <PullRequestChangesHereContext.Provider value={changesHere}>
        <PullRequestActionOriginContext.Provider value={origin}>
          <LifecycleSurfaceContext.Provider value="surface-1">
            {children}
          </LifecycleSurfaceContext.Provider>
        </PullRequestActionOriginContext.Provider>
      </PullRequestChangesHereContext.Provider>
    </QueryClientProvider>
  );
  const pr = () => queryClient.getQueryData<PullRequestInfo>(infoKey())?.pr;
  return { queryClient, wrapper, reads, pr };
}

/** The controller, the PR's read, and a Merge as the merge bar sends it. */
function renderActions(harness: ReturnType<typeof setup>, initial = pullRequest()) {
  return renderHook(
    ({ facts }: { facts: LifecyclePullRequest }) => {
      useQuery({
        queryKey: infoKey(),
        queryFn: () => harness.reads.scoped(),
        staleTime: Infinity,
      });
      return {
        actions: usePullRequestLifecycleActions(facts),
        merge: useMergePullRequest(SESSION),
      };
    },
    { wrapper: harness.wrapper, initialProps: { facts: initial } },
  );
}

/** Let the merge options read land, so auto-merge can say where it stands. */
async function optionsRead(result: {
  current: { actions: { availability: Record<string, { kind: string }> } };
}) {
  await waitFor(() =>
    expect(result.current.actions.availability.enable_auto_merge.kind).toBe("mac"),
  );
}

beforeEach(() => {
  currentUser.id = "user-1";
  identityListeners.clear();
  resetDesktopPullRequestWritesForTests();
  resetLifecycleActionsForTests();
  resetStateChainsForTests();
  toast.mockReset();
  hostFetch.mockReset();
  hostFetch.mockImplementation(
    async () =>
      new Response(JSON.stringify({ pr_url: PR }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
  );
});

function switchWorkspaceUser(id: string) {
  act(() => {
    currentUser.id = id;
    for (const listener of [...identityListeners]) listener();
  });
}

/** This Mac now reads GitHub as `login`, at credential `generation`. */
function macReadsAs(login: string, generation: number) {
  act(() => {
    acceptMacAnswer(macReadTicket("github", "github.com"), {
      identity: { host: "github.com", login, generation },
    });
  });
}

const unknown = () => refusal("outcome_unknown", "GitHub may have made this change.");

afterEach(() => {
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetLifecycleActionsForTests();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  githubMacReadsPreference.write("automatic");
});

describe("a view that may not change its pull request", () => {
  it("hands every state change to GitHub, though this Mac could make it", async () => {
    const sent = await installShell({ pull_request_convert_to_draft: () => draftAnswer(true) });
    const { result } = renderActions(setup(null, false));
    expect(result.current.actions.availability.convert_to_draft).toEqual({
      kind: "github",
      url: expect.stringContaining("github.com"),
    });
    expect(sent).toEqual([]);
  });
});

describe("converting to a draft and marking ready on this Mac", () => {
  it("asks first, then converts, and the PR reads as a draft", async () => {
    const sent = await installShell({ pull_request_convert_to_draft: () => draftAnswer(true) });
    const harness = setup();
    const { result } = renderActions(harness);
    expect(result.current.actions.availability.convert_to_draft).toEqual({ kind: "mac" });
    expect(result.current.actions.availability.ready_for_review).toEqual({ kind: "hidden" });
    act(() => result.current.actions.request("convert_to_draft"));
    expect(result.current.actions.state.confirming?.id).toBe("convert_to_draft");
    expect(sent).toEqual([]);
    act(() => result.current.actions.confirm());
    await waitFor(() => expect(result.current.actions.state.running).toBeNull());
    expect(sent).toEqual([
      {
        operation: "pull_request_convert_to_draft",
        params: { repository: "acme/app", number: 42 },
      },
    ]);
    expect(harness.pr()?.is_draft).toBe(true);
    expect(result.current.actions.state.notice).toBeNull();
    expect(toast).toHaveBeenCalledWith("Converted to draft · #42", expect.anything());
  });

  it("marks a draft ready at once, without asking", async () => {
    const sent = await installShell({ pull_request_ready_for_review: () => draftAnswer(false) });
    const harness = setup();
    harness.queryClient.setQueryData(infoKey(), info({ is_draft: true }));
    const { result } = renderActions(harness, pullRequest({ isDraft: true }));
    act(() => result.current.actions.request("ready_for_review"));
    expect(result.current.actions.state.confirming).toBeNull();
    await waitFor(() =>
      expect(sent.map((write) => write.operation)).toEqual(["pull_request_ready_for_review"]),
    );
    await waitFor(() => expect(harness.pr()?.is_draft).toBe(false));
  });

  it("tells the Pull Requests page it's shown on once GitHub made it", async () => {
    await installShell({ pull_request_ready_for_review: () => draftAnswer(false) });
    const succeeded = vi.fn();
    const origin = { begin: vi.fn(() => ({ succeeded })) };
    const harness = setup(origin);
    harness.queryClient.setQueryData(infoKey(), info({ is_draft: true }));
    const { result } = renderActions(harness, pullRequest({ isDraft: true }));
    act(() => result.current.actions.request("ready_for_review"));
    await waitFor(() => expect(succeeded).toHaveBeenCalledWith("marked_ready"));
    expect(origin.begin).toHaveBeenCalledWith(PR, expect.objectContaining({ login: "mac-octo" }));
    expect(succeeded).toHaveBeenCalledTimes(1);
  });

  it("keeps a refusal's reason until dismissed", async () => {
    await installShell({
      pull_request_convert_to_draft: () =>
        refusal("forbidden", "@mac-octo can't do this in acme/app."),
    });
    const { result } = renderActions(setup());
    act(() => result.current.actions.request("convert_to_draft"));
    act(() => result.current.actions.confirm());
    await waitFor(() => expect(result.current.actions.state.notice).not.toBeNull());
    expect(result.current.actions.state.notice?.outcome).toEqual({
      kind: "refused",
      code: "forbidden",
      message: "@mac-octo can't do this in acme/app.",
    });
    act(() => result.current.actions.dismiss());
    expect(result.current.actions.state.notice).toBeNull();
  });

  it("offers GitHub instead where the shell doesn't list it, and sends nothing", async () => {
    const sent = await installShell({}, { writes: ["pull_request_close", "pull_request_reopen"] });
    const { result } = renderActions(setup());
    expect(result.current.actions.availability.convert_to_draft).toEqual({
      kind: "github",
      url: PR,
    });
    act(() => result.current.actions.request("convert_to_draft"));
    expect(result.current.actions.state.confirming).toBeNull();
    expect(sent).toEqual([]);
  });

  it("sends nothing when the workspace user changes while the dialog is open", async () => {
    const sent = await installShell({ pull_request_convert_to_draft: () => draftAnswer(true) });
    const { result } = renderActions(setup());
    act(() => result.current.actions.request("convert_to_draft"));
    act(() => {
      currentUser.id = "user-2";
      for (const listener of [...identityListeners]) listener();
    });
    // The new user's page has no dialog open; the old one's confirmation sends nothing.
    expect(result.current.actions.state.confirming).toBeNull();
    act(() => result.current.actions.confirm());
    await act(async () => {});
    expect(sent).toEqual([]);
  });
});

describe("an unknown outcome", () => {
  it("holds every state change, Merge included, until Check Again's fresh read", async () => {
    let convert: unknown = refusal("outcome_unknown", "GitHub may have made this change.");
    const sent = await installShell({
      pull_request_convert_to_draft: () => convert,
      pull_request_merge: () => ok({ object: "session.github.pr_merge", pr_url: PR }),
    });
    const harness = setup();
    const { result } = renderActions(harness);
    act(() => result.current.actions.request("convert_to_draft"));
    act(() => result.current.actions.confirm());
    await waitFor(() => expect(result.current.actions.state.notice?.outcome.kind).toBe("unknown"));
    expect(result.current.actions.availability.convert_to_draft.kind).toBe("held");
    // Neither the action again, a dismiss, nor Merge from the merge bar sends anything.
    act(() => result.current.actions.request("convert_to_draft"));
    act(() => result.current.actions.dismiss());
    act(() =>
      result.current.merge.mutate({ prUrl: PR, mergeMethod: "squash", expectedHead: HEAD }),
    );
    await act(async () => {});
    expect(sent.map((write) => write.operation)).toEqual(["pull_request_convert_to_draft"]);
    expect(result.current.actions.state.notice?.outcome.kind).toBe("unknown");
    // A fresh read of the PR on this Mac settles it, and Merge goes through again.
    await act(async () => result.current.actions.checkAgain());
    await waitFor(() => expect(result.current.actions.state.notice).toBeNull());
    act(() =>
      result.current.merge.mutate({ prUrl: PR, mergeMethod: "squash", expectedHead: HEAD }),
    );
    await waitFor(() =>
      expect(sent.map((write) => write.operation)).toEqual([
        "pull_request_convert_to_draft",
        "pull_request_merge",
      ]),
    );
    convert = draftAnswer(true);
  });

  it("stays held when Check Again's read fails, and says so", async () => {
    await installShell({
      pull_request_convert_to_draft: () =>
        refusal("outcome_unknown", "GitHub may have made this change."),
    });
    const harness = setup();
    const { result } = renderActions(harness);
    act(() => result.current.actions.request("convert_to_draft"));
    act(() => result.current.actions.confirm());
    await waitFor(() => expect(result.current.actions.state.notice?.outcome.kind).toBe("unknown"));
    harness.reads.scoped = async () => {
      throw new Error("offline");
    };
    await act(async () => result.current.actions.checkAgain());
    expect(result.current.actions.state.checkFailed).toBe(true);
    expect(result.current.actions.state.notice?.outcome.kind).toBe("unknown");
  });

  it("settles from a read whose PR URL GitHub spells in another case", async () => {
    await installShell({
      pull_request_convert_to_draft: () =>
        refusal("outcome_unknown", "GitHub may have made this change."),
    });
    const harness = setup();
    const { result } = renderActions(harness);
    act(() => result.current.actions.request("convert_to_draft"));
    act(() => result.current.actions.confirm());
    await waitFor(() => expect(result.current.actions.state.notice?.outcome.kind).toBe("unknown"));
    // Opened as acme/app; GitHub's own URL for it says ACME/App.
    harness.reads.scoped = async () =>
      info({ url: "https://github.com/ACME/App/pull/42", is_draft: true });
    await act(async () => result.current.actions.checkAgain());
    await waitFor(() => expect(result.current.actions.state.notice).toBeNull());
  });

  it("isn't settled by a read made as another Mac account", async () => {
    await installShell({
      pull_request_convert_to_draft: () =>
        refusal("outcome_unknown", "GitHub may have made this change."),
    });
    const harness = setup();
    const { result } = renderActions(harness);
    act(() => result.current.actions.request("convert_to_draft"));
    act(() => result.current.actions.confirm());
    await waitFor(() => expect(result.current.actions.state.notice?.outcome.kind).toBe("unknown"));
    act(() => {
      acceptMacAnswer(macReadTicket("github", "github.com"), {
        identity: { host: "github.com", login: "other-octo", generation: 2 },
      });
    });
    await act(async () => result.current.actions.checkAgain());
    expect(result.current.actions.state.notice?.outcome.kind).toBe("unknown");
  });
});

describe("Check Again after an auto-merge change", () => {
  const mayHave = () => refusal("outcome_unknown", "GitHub may have made this change.");
  const ON = { merge_method: "SQUASH", enabled_by: "amy", enabled_at: "2026-11-05T10:00:00Z" };

  /** Disable sent with an unknown outcome; the PR's next read answers `read`. */
  async function disableUnknown(read: Record<string, unknown>) {
    const sent = await installShell({ pull_request_auto_merge_disable: mayHave });
    const harness = setup();
    const { result } = renderActions(harness, pullRequest({ autoMergeOn: true }));
    // It doesn't ask first.
    act(() => result.current.actions.request("disable_auto_merge"));
    await waitFor(() => expect(result.current.actions.state.notice?.outcome.kind).toBe("unknown"));
    expect(result.current.actions.state.confirming).toBeNull();
    expect(result.current.actions.availability.convert_to_draft.kind).toBe("held");
    harness.reads.scoped = async () => info(read);
    await act(async () => result.current.actions.checkAgain());
    return { result, sent };
  }

  it("clears once a fresh read says auto-merge is off, and never sends again", async () => {
    const { result, sent } = await disableUnknown({ auto_merge: null });
    await waitFor(() => expect(result.current.actions.state.notice).toBeNull());
    expect(sent.map((write) => write.operation)).toEqual(["pull_request_auto_merge_disable"]);
  });

  it("says auto-merge is currently on where the read finds it on, and lets the PR's changes go", async () => {
    const { result, sent } = await disableUnknown({ auto_merge: ON });
    await waitFor(() =>
      expect(result.current.actions.state.notice?.outcome).toEqual({
        kind: "checked",
        autoMergeOn: true,
      }),
    );
    expect(lifecycleNoticeMessage(result.current.actions.state.notice!)).toBe(
      "Auto-merge is currently on.",
    );
    expect(result.current.actions.availability.convert_to_draft.kind).toBe("mac");
    expect(sent).toHaveLength(1);
  });

  it("stays held where the read can't say, as from an older shell, whatever its state", async () => {
    const { result, sent } = await disableUnknown({});
    expect(result.current.actions.state.notice?.outcome.kind).toBe("unknown");
    expect(result.current.actions.state.checkFailed).toBe(true);
    expect(result.current.actions.availability.convert_to_draft.kind).toBe("held");
    expect(sent).toHaveLength(1);
  });

  it("settles turning it on only from a read of auto-merge, not the PR's state alone", async () => {
    await installShell({ pull_request_auto_merge_enable: mayHave });
    const harness = setup();
    const { result } = renderActions(harness);
    await optionsRead(result);
    act(() => result.current.actions.request("enable_auto_merge"));
    act(() => result.current.actions.confirm("squash"));
    await waitFor(() => expect(result.current.actions.state.notice?.outcome.kind).toBe("unknown"));
    // An open PR, with nothing said of auto-merge, says nothing of the change.
    harness.reads.scoped = async () => info();
    await act(async () => result.current.actions.checkAgain());
    expect(result.current.actions.state.notice?.outcome.kind).toBe("unknown");
    harness.reads.scoped = async () => info({ auto_merge: null });
    await act(async () => result.current.actions.checkAgain());
    await waitFor(() =>
      expect(result.current.actions.state.notice?.outcome).toEqual({
        kind: "checked",
        autoMergeOn: false,
      }),
    );
    expect(lifecycleNoticeMessage(result.current.actions.state.notice!)).toBe(
      "Auto-merge is currently off.",
    );
  });
});

describe("turning on auto-merge on this Mac", () => {
  it("asks with the head and method shown, then sends exactly those", async () => {
    const sent = await installShell({ pull_request_auto_merge_enable: autoMergeAnswer });
    const harness = setup();
    const { result } = renderActions(harness);
    await optionsRead(result);
    act(() => result.current.actions.request("enable_auto_merge"));
    expect(result.current.actions.state.confirming).toMatchObject({
      id: "enable_auto_merge",
      headSha: HEAD,
      method: "squash",
    });
    act(() => result.current.actions.confirm("merge"));
    await waitFor(() => expect(result.current.actions.state.running).toBeNull());
    expect(sent).toEqual([
      {
        operation: "pull_request_auto_merge_enable",
        params: { repository: "acme/app", number: 42, merge_method: "merge", expected_head: HEAD },
      },
    ]);
    expect(harness.pr()?.auto_merge).toMatchObject({ merge_method: "merge" });
  });

  it.each([
    ["GitHub's stack is being read", { mergeable: false }],
    ["GitHub merges it with its stack", { mergeOnGitHub: { url: PR, unmergedBelow: 1 } }],
  ])(
    "sends nothing from a confirmation opened before %s",
    async (_label, later: Partial<LifecyclePullRequest>) => {
      const sent = await installShell({ pull_request_auto_merge_enable: autoMergeAnswer });
      const { result, rerender } = renderActions(setup());
      await optionsRead(result);
      act(() => result.current.actions.request("enable_auto_merge"));
      expect(result.current.actions.state.confirming?.id).toBe("enable_auto_merge");
      rerender({ facts: pullRequest(later) });
      expect(result.current.actions.availability.enable_auto_merge).toEqual({ kind: "hidden" });
      // The repository still allows squash, and its options are still cached.
      act(() => result.current.actions.confirm("squash"));
      await pause(5);
      expect(result.current.actions.state.confirming).toBeNull();
      expect(result.current.actions.state.running).toBeNull();
      expect(sent).toEqual([]);
    },
  );

  it("refuses a method the repository doesn't allow, sending nothing", async () => {
    const sent = await installShell({ pull_request_auto_merge_enable: autoMergeAnswer });
    const { result } = renderActions(setup());
    await optionsRead(result);
    act(() => result.current.actions.request("enable_auto_merge"));
    act(() => result.current.actions.confirm("rebase"));
    await act(async () => {});
    expect(sent).toEqual([]);
  });

  it("pins a later request to the head shown then, not the one before", async () => {
    const sent = await installShell({ pull_request_auto_merge_enable: autoMergeAnswer });
    const { result, rerender } = renderActions(setup());
    await optionsRead(result);
    // A push that leaves it BLOCKED: still offered, now at the newer head.
    rerender({ facts: pullRequest({ headSha: NEWER }) });
    expect(result.current.actions.availability.enable_auto_merge.kind).toBe("mac");
    act(() => result.current.actions.request("enable_auto_merge"));
    act(() => result.current.actions.confirm());
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].params.expected_head).toBe(NEWER);
  });

  it("leaves a newer read alone when the answer arrives after a push, and reads again", async () => {
    let answer!: (value: unknown) => void;
    const sent = await installShell({
      pull_request_auto_merge_enable: () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    });
    const harness = setup();
    const invalidate = vi.spyOn(harness.queryClient, "invalidateQueries");
    const { result } = renderActions(harness);
    await optionsRead(result);
    act(() => result.current.actions.request("enable_auto_merge"));
    act(() => result.current.actions.confirm());
    await waitFor(() => expect(sent).toHaveLength(1));
    // A read that finished after the click shows a newer push, auto-merge off.
    await pause(2);
    harness.queryClient.setQueryData(infoKey(), info({ head_sha: NEWER, auto_merge: null }));
    await act(async () => answer(autoMergeAnswer()));
    await waitFor(() => expect(result.current.actions.state.running).toBeNull());
    expect(harness.pr()?.head_sha).toBe(NEWER);
    expect(harness.pr()?.auto_merge).toBeNull();
    expect(invalidate).toHaveBeenCalled();
  });

  it("says a PR that can merge now should use Merge, without a toast", async () => {
    await installShell({
      pull_request_auto_merge_enable: () =>
        refusal("mergeable_now", "This pull request can merge now. Use Merge instead."),
    });
    const { result } = renderActions(setup());
    await optionsRead(result);
    act(() => result.current.actions.request("enable_auto_merge"));
    act(() => result.current.actions.confirm());
    await waitFor(() => expect(result.current.actions.state.notice).not.toBeNull());
    expect(result.current.actions.state.notice?.outcome).toMatchObject({
      kind: "refused",
      code: "mergeable_now",
    });
    expect(toast).not.toHaveBeenCalled();
  });

  const hiddenWhen: [string, Record<string, unknown>, Partial<LifecyclePullRequest>][] = [
    ["the base has a merge queue", { merge_queue: true }, {}],
    ["GitHub can't say whether it has one", { merge_queue: null }, {}],
    ["it can merge now", { merge_state_status: "CLEAN" }, {}],
    ["it has conflicts", { merge_state_status: "DIRTY" }, {}],
    ["auto-merge is on", { auto_merge: { merge_method: "squash" } }, {}],
    ["it's a draft", {}, { isDraft: true }],
    ["merging isn't offered here", {}, { mergeable: false }],
    ["GitHub merges it with its stack", {}, { mergeOnGitHub: { url: PR, unmergedBelow: 1 } }],
  ];
  it.each(hiddenWhen)("isn't offered where %s", async (_label, options, facts) => {
    await installShell({}, { mergeOptions: { ...MERGE_OPTIONS, ...options } });
    const { result } = renderActions(setup(), pullRequest(facts));
    await act(async () => {});
    await pause(5);
    expect(result.current.actions.availability.enable_auto_merge).toEqual({ kind: "hidden" });
  });
});

describe("the page's confirmations and ⌘K rows", () => {
  const rows = () =>
    Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) =>
      group.commands.map((command) => command.label),
    );

  function Page({
    active,
    facts = pullRequest(),
  }: {
    active: boolean;
    facts?: LifecyclePullRequest;
  }) {
    return <PullRequestLifecycleOwner pr={facts} active={active} />;
  }

  it("registers a row for each action this Mac can make, while the page is active", async () => {
    await installShell({});
    const harness = setup();
    const view = render(<Page active />, { wrapper: harness.wrapper });
    await waitFor(() => expect(rows()).toContain("Enable Auto-merge…"));
    expect(rows()).toContain("Convert Pull Request to Draft…");
    expect(rows()).not.toContain("Mark Pull Request Ready for Review");
    view.rerender(<Page active={false} />);
    expect(rows()).not.toContain("Convert Pull Request to Draft…");
    view.unmount();
  });

  it("offers Merge on GitHub for a PR in GitHub's stack, not auto-merge, and sends nothing", async () => {
    const sent = await installShell({});
    const opened = vi.fn();
    vi.stubGlobal("open", opened);
    const harness = setup();
    const facts = pullRequest({ mergeOnGitHub: { url: PR, unmergedBelow: 2 } });
    const view = render(<Page active facts={facts} />, { wrapper: harness.wrapper });
    await waitFor(() => expect(rows()).toContain("Convert Pull Request to Draft…"));
    await pause(5);
    expect(rows()).not.toContain("Enable Auto-merge…");
    const row = Object.values(usePaletteCommandsStore.getState().groups)
      .flatMap((group) => group.commands)
      .find((command) => command.label === "Merge Pull Request on GitHub")!;
    expect(row.description).toBe(
      "GitHub merges a stack together: merging this pull request also merges the 2 unmerged pull requests below it.",
    );
    act(() => row.run());
    expect(opened).toHaveBeenCalledWith(PR, "_blank", "noopener,noreferrer");
    expect(sent).toEqual([]);
    view.rerender(<Page active={false} facts={facts} />);
    expect(rows()).not.toContain("Merge Pull Request on GitHub");
    view.unmount();
    vi.unstubAllGlobals();
  });

  it("retires an open Enable auto-merge dialog once GitHub's stack decides the merge", async () => {
    const sent = await installShell({ pull_request_auto_merge_enable: autoMergeAnswer });
    const harness = setup();
    const view = render(<Page active />, { wrapper: harness.wrapper });
    await waitFor(() => expect(rows()).toContain("Enable Auto-merge…"));
    const row = Object.values(usePaletteCommandsStore.getState().groups)
      .flatMap((group) => group.commands)
      .find((command) => command.label === "Enable Auto-merge…")!;
    act(() => row.run());
    expect(await screen.findByText("Choose how #42 merges")).toBeTruthy();
    // A retarget starts a new read of GitHub's stack; then GitHub says it stacks the PR.
    view.rerender(<Page active facts={pullRequest({ mergeable: false })} />);
    await waitFor(() => expect(screen.queryByText("Choose how #42 merges")).toBeNull());
    const native = pullRequest({ mergeOnGitHub: { url: PR, unmergedBelow: 1 } });
    view.rerender(<Page active facts={native} />);
    expect(screen.queryByText("Choose how #42 merges")).toBeNull();
    // Merging here again never brings back the confirmation that was retired.
    view.rerender(<Page active />);
    await pause(5);
    expect(screen.queryByText("Choose how #42 merges")).toBeNull();
    expect(sent).toEqual([]);
    view.unmount();
  });

  it("opens the dialog from a row, and closes it, sending nothing, once the page is hidden", async () => {
    const sent = await installShell({ pull_request_convert_to_draft: () => draftAnswer(true) });
    const harness = setup();
    const view = render(<Page active />, { wrapper: harness.wrapper });
    await waitFor(() => expect(rows()).toContain("Convert Pull Request to Draft…"));
    const row = Object.values(usePaletteCommandsStore.getState().groups)
      .flatMap((group) => group.commands)
      .find((command) => command.label === "Convert Pull Request to Draft…")!;
    act(() => row.run());
    expect(await screen.findByText("Convert #42 to a draft?")).toBeTruthy();
    view.rerender(<Page active={false} />);
    await waitFor(() => expect(screen.queryByText("Convert #42 to a draft?")).toBeNull());
    expect(sent).toEqual([]);
    view.rerender(<Page active />);
    expect(screen.queryByText("Convert #42 to a draft?")).toBeNull();
    view.unmount();
  });

  it("converts from the dialog's button, with Cancel focused first", async () => {
    const sent = await installShell({ pull_request_convert_to_draft: () => draftAnswer(true) });
    const harness = setup();
    const { result } = renderHook(() => usePullRequestLifecycleActions(pullRequest()), {
      wrapper: harness.wrapper,
    });
    const view = render(<Page active />, { wrapper: harness.wrapper });
    act(() => result.current.request("convert_to_draft"));
    const cancel = await screen.findByRole("button", { name: "Cancel" });
    await waitFor(() => expect(document.activeElement).toBe(cancel));
    fireEvent.click(screen.getByRole("button", { name: "Convert to Draft" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    view.unmount();
  });
});

describe("recovering from an unknown outcome", () => {
  async function unknownConvert(facts = pullRequest()) {
    const sent = await installShell({
      pull_request_convert_to_draft: unknown,
      pull_request_merge: () => ok({ object: "session.github.pr_merge", pr_url: PR }),
    });
    // After the shell, so the PR's read is cached under this Mac's scope.
    const harness = setup();
    const rendered = renderActions(harness, facts);
    act(() => rendered.result.current.actions.request("convert_to_draft"));
    act(() => rendered.result.current.actions.confirm());
    await waitFor(() =>
      expect(rendered.result.current.actions.state.notice?.outcome.kind).toBe("unknown"),
    );
    return { ...rendered, harness, sent };
  }

  it("keeps Merge held on the session's machine too, once the read source switches", async () => {
    const { result } = await unknownConvert();
    // Reading from the session's machine now: Merge would go by the host, and still waits.
    act(() => githubMacReadsPreference.write("off"));
    const errors: string[] = [];
    act(() =>
      result.current.merge.mutate(
        { prUrl: PR, mergeMethod: "squash", expectedHead: HEAD },
        { onError: (error) => errors.push(error.message) },
      ),
    );
    await act(async () => {});
    expect(hostFetch).not.toHaveBeenCalled();
    expect(errors).toEqual([
      "GitHub may have made an earlier change to this pull request. Check it again before changing it.",
    ]);
  });

  it("shows the same notice where the PR is opened by a link in another case", async () => {
    const { harness } = await unknownConvert();
    const alias = "https://github.com/ACME/App/pull/42/";
    const { result } = renderHook(
      () => usePullRequestLifecycleActions(pullRequest({ prUrl: alias })),
      { wrapper: harness.wrapper },
    );
    expect(result.current.state.notice?.outcome.kind).toBe("unknown");
    expect(result.current.availability.convert_to_draft.kind).toBe("held");
  });

  it.each([
    ["this Mac's account changes", () => macReadsAs("other-octo", 2)],
    ["the workspace user changes", () => switchWorkspaceUser("user-2")],
    [
      "the read source switches to the session's machine",
      () => githubMacReadsPreference.write("off"),
    ],
  ])("isn't settled by a read during which %s", async (_label, change) => {
    const { result, harness } = await unknownConvert();
    // Only Check Again's own read waits; a read a new scope starts answers at once.
    let answer: ((value: unknown) => void) | undefined;
    const settled = harness.reads.scoped;
    harness.reads.scoped = () =>
      answer
        ? settled()
        : new Promise((resolve) => {
            answer = resolve;
          });
    let checked!: Promise<void>;
    act(() => {
      checked = result.current.actions.checkAgain();
    });
    await waitFor(() => expect(answer).toBeTypeOf("function"));
    act(() => change());
    await act(async () => {
      answer!(info({ is_draft: true }));
      await checked;
    });
    // Back as the first user, the notice and its hold are still there.
    if (currentUser.id !== "user-1") switchWorkspaceUser("user-1");
    expect(result.current.actions.state.notice?.outcome.kind).toBe("unknown");
  });

  it("is settled once this Mac reads as the same login again, after another account", async () => {
    const { result } = await unknownConvert();
    macReadsAs("other-octo", 2);
    await act(async () => result.current.actions.checkAgain());
    expect(result.current.actions.state.notice?.outcome.kind).toBe("unknown");
    // Back as mac-octo with a renewed credential: a fresh read as that login settles it.
    macReadsAs("mac-octo", 3);
    await act(async () => result.current.actions.checkAgain());
    await waitFor(() => expect(result.current.actions.state.notice).toBeNull());
  });
});

/** The pill's badge as the header renders it, marked while a change is on its way. */
function PendingBadge({ pr }: { pr: LifecyclePullRequest }) {
  const pending = usePullRequestStatePillProgress(pr).kind === "running";
  return <span data-pending={pending || undefined}>Open</span>;
}

describe("the page's controls", () => {
  const paletteRow = (label: string) =>
    Object.values(usePaletteCommandsStore.getState().groups)
      .flatMap((group) => group.commands)
      .find((command) => command.label === label);

  function Page({
    facts,
    active = true,
    mergeReady = true,
  }: {
    facts: LifecyclePullRequest;
    active?: boolean;
    mergeReady?: boolean;
  }) {
    return (
      <>
        <PullRequestStateMenu pr={facts} badge={<PendingBadge pr={facts} />} />
        <ConvertToDraftPrompt pr={facts} />
        <ReadyForReviewPrompt pr={facts} />
        <PullRequestChangeNotice pr={facts} />
        <section aria-label="Merge">
          <EnableAutoMergeSlot pr={facts} />
          <button type="button" data-merge-button disabled={!mergeReady}>
            Squash and merge
          </button>
        </section>
        <PullRequestLifecycleOwner pr={facts} active={active} />
      </>
    );
  }

  it("converts from the state pill's menu, after asking", async () => {
    const sent = await installShell({ pull_request_convert_to_draft: () => draftAnswer(true) });
    const harness = setup();
    const view = render(<Page facts={pullRequest()} />, { wrapper: harness.wrapper });
    fireEvent.pointerDown(screen.getByRole("button", { name: "Pull request state" }), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Convert to draft…" }));
    fireEvent.click(await screen.findByRole("button", { name: "Convert to Draft" }));
    await waitFor(() =>
      expect(sent.map((write) => write.operation)).toEqual(["pull_request_convert_to_draft"]),
    );
    view.unmount();
  });

  it("asks from 'Still in progress? Convert to draft'", async () => {
    await installShell({});
    const view = render(<Page facts={pullRequest()} />, { wrapper: setup().wrapper });
    expect(screen.getByText(/Still in progress\?/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Convert to draft" }));
    expect(await screen.findByText("Convert #42 to a draft?")).toBeTruthy();
    view.unmount();
  });

  it("marks a draft ready from 'Still a draft? Ready for review', with no draft prompt", async () => {
    const sent = await installShell({ pull_request_ready_for_review: () => draftAnswer(false) });
    const harness = setup();
    harness.queryClient.setQueryData(infoKey(), info({ is_draft: true }));
    const view = render(<Page facts={pullRequest({ isDraft: true })} />, {
      wrapper: harness.wrapper,
    });
    expect(screen.getByText(/Still a draft\?/)).toBeTruthy();
    expect(screen.queryByText(/Still in progress\?/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Ready for review" }));
    await waitFor(() =>
      expect(sent.map((write) => write.operation)).toEqual(["pull_request_ready_for_review"]),
    );
    view.unmount();
  });

  it("offers the pill's GitHub link and no prompt button on a shell without the write", async () => {
    await installShell({}, { writes: [] });
    const view = render(<Page facts={pullRequest()} />, { wrapper: setup().wrapper });
    expect(
      screen.getByRole("link", { name: "Convert to draft on GitHub" }).getAttribute("href"),
    ).toBe(PR);
    expect(screen.queryByRole("button", { name: "Convert to draft" })).toBeNull();
    view.unmount();
  });

  it("says a PR that can merge now should use Merge, and moves focus to Merge", async () => {
    await installShell({
      pull_request_auto_merge_enable: () =>
        refusal("mergeable_now", "This pull request can merge now. Use Merge instead."),
    });
    const view = render(<Page facts={pullRequest()} />, { wrapper: setup().wrapper });
    fireEvent.click(await screen.findByRole("button", { name: "Enable auto-merge" }));
    fireEvent.click(await screen.findByRole("button", { name: "Continue…" }));
    expect(
      await screen.findByText("This pull request can merge now. Use Merge instead."),
    ).toBeTruthy();
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "Squash and merge" })),
    );
    // Overview's notice leaves this one to the merge bar; ⌘K can dismiss it.
    expect(screen.queryByRole("status", { name: /Couldn’t turn on/ })).toBeNull();
    act(() => paletteRow("Dismiss Auto-merge Notice")!.run());
    await waitFor(() =>
      expect(screen.queryByText("This pull request can merge now. Use Merge instead.")).toBeNull(),
    );
    view.unmount();
  });

  it("focuses Merge once the read showing it can merge enables it, and only then", async () => {
    await installShell({
      pull_request_auto_merge_enable: () =>
        refusal("mergeable_now", "This pull request can merge now. Use Merge instead."),
    });
    const harness = setup();
    // The cached read still says BLOCKED: Merge is disabled when GitHub refuses.
    const view = render(<Page facts={pullRequest()} mergeReady={false} />, {
      wrapper: harness.wrapper,
    });
    fireEvent.click(await screen.findByRole("button", { name: "Enable auto-merge" }));
    fireEvent.click(await screen.findByRole("button", { name: "Continue…" }));
    expect(
      await screen.findByText("This pull request can merge now. Use Merge instead."),
    ).toBeTruthy();
    const merge = screen.getByRole("button", { name: "Squash and merge" });
    expect(document.activeElement).not.toBe(merge);
    // The refetch lands and enables Merge: it takes focus then.
    view.rerender(<Page facts={pullRequest()} mergeReady />);
    await waitFor(() => expect(document.activeElement).toBe(merge));
    // Once: moving on and re-rendering never takes it back.
    act(() => merge.blur());
    view.rerender(<Page facts={pullRequest()} mergeReady={false} />);
    view.rerender(<Page facts={pullRequest()} mergeReady />);
    await act(async () => {});
    expect(document.activeElement).not.toBe(merge);
    view.unmount();
  });

  it("reads the merge state again after 'can merge now', enabling Merge and focusing it", async () => {
    // GitHub's state as this Mac reads it: waiting at first, then mergeable by the time it asks.
    const options = { ...MERGE_OPTIONS };
    await installShell(
      {
        pull_request_auto_merge_enable: () => {
          options.merge_state_status = "CLEAN";
          return refusal("mergeable_now", "This pull request can merge now. Use Merge instead.");
        },
      },
      { mergeOptions: options },
    );
    const harness = setup();
    // The merge bar's own Merge, enabled from the same Mac read the page shows.
    function ReadMergeBar() {
      const read = usePullRequestMergeOptions(SESSION, PR, true);
      return <Page facts={pullRequest()} mergeReady={read.data?.merge_state_status === "CLEAN"} />;
    }
    const view = render(<ReadMergeBar />, { wrapper: harness.wrapper });
    fireEvent.click(await screen.findByRole("button", { name: "Enable auto-merge" }));
    fireEvent.click(await screen.findByRole("button", { name: "Continue…" }));
    expect(
      await screen.findByText("This pull request can merge now. Use Merge instead."),
    ).toBeTruthy();
    // No focus or navigation needed: the refusal itself reads the merge state again.
    const merge = screen.getByRole("button", { name: "Squash and merge" });
    await waitFor(() => expect((merge as HTMLButtonElement).disabled).toBe(false));
    await waitFor(() => expect(document.activeElement).toBe(merge));
    expect(screen.queryByRole("button", { name: "Enable auto-merge" })).toBeNull();
    view.unmount();
  });

  it("runs a ⌘K row with the head shown now, after a push that leaves it offered", async () => {
    const sent = await installShell({ pull_request_auto_merge_enable: autoMergeAnswer });
    const harness = setup();
    const view = render(<Page facts={pullRequest()} />, { wrapper: harness.wrapper });
    await waitFor(() => expect(paletteRow("Enable Auto-merge…")).toBeTruthy());
    view.rerender(<Page facts={pullRequest({ headSha: NEWER })} />);
    act(() => paletteRow("Enable Auto-merge…")!.run());
    fireEvent.click(await screen.findByRole("button", { name: "Continue…" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].params.expected_head).toBe(NEWER);
    view.unmount();
  });

  it("runs a ⌘K row for the workspace user signed in now", async () => {
    const sent = await installShell({ pull_request_convert_to_draft: () => draftAnswer(true) });
    const harness = setup();
    const view = render(<Page facts={pullRequest()} />, { wrapper: harness.wrapper });
    await waitFor(() => expect(paletteRow("Convert Pull Request to Draft…")).toBeTruthy());
    switchWorkspaceUser("user-2");
    // The new user's own read on this Mac, as their page takes one.
    macReadsAs("mac-octo", 1);
    await waitFor(() => expect(paletteRow("Convert Pull Request to Draft…")).toBeTruthy());
    act(() => paletteRow("Convert Pull Request to Draft…")!.run());
    fireEvent.click(await screen.findByRole("button", { name: "Convert to Draft" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    // What it came to is the new user's, under their key.
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith("Converted to draft · #42", expect.anything()),
    );
    view.unmount();
  });

  it("drops an unsent confirmation on navigating away, so the PR's actions work on return", async () => {
    const sent = await installShell({ pull_request_convert_to_draft: () => draftAnswer(true) });
    const harness = setup();
    const view = render(<Page facts={pullRequest()} />, { wrapper: harness.wrapper });
    fireEvent.click(screen.getByRole("button", { name: "Convert to draft" }));
    expect(await screen.findByText("Convert #42 to a draft?")).toBeTruthy();
    view.unmount();
    // Back on the PR, under a new page: no dialog left behind, and the action asks again.
    const again = render(
      <LifecycleSurfaceContext.Provider value="surface-2">
        <Page facts={pullRequest()} />
      </LifecycleSurfaceContext.Provider>,
      { wrapper: harness.wrapper },
    );
    expect(screen.queryByText("Convert #42 to a draft?")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Convert to draft" }));
    fireEvent.click(await screen.findByRole("button", { name: "Convert to Draft" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    again.unmount();
  });

  it("keeps the outcome of a change already sent when its page goes away", async () => {
    let answer!: (value: unknown) => void;
    await installShell({
      pull_request_convert_to_draft: () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    });
    const harness = setup();
    const view = render(<Page facts={pullRequest()} />, { wrapper: harness.wrapper });
    fireEvent.click(screen.getByRole("button", { name: "Convert to draft" }));
    fireEvent.click(await screen.findByRole("button", { name: "Convert to Draft" }));
    await waitFor(() => expect(answer).toBeTypeOf("function"));
    view.unmount();
    await act(async () => answer(unknown()));
    const back = render(<Page facts={pullRequest()} />, { wrapper: harness.wrapper });
    expect(
      await screen.findByText(/GitHub may have converted this pull request to a draft/),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Check Again" })).toBeTruthy();
    expect(paletteRow("Check Pull Request Again")).toBeTruthy();
    expect(paletteRow("Open Pull Request on GitHub")).toBeTruthy();
    back.unmount();
  });

  const stateAnswer = (state: string) =>
    ok({ object: "session.github.pr_state", pr_url: PR, state });
  const openPill = async () => {
    fireEvent.pointerDown(screen.getByRole("button", { name: "Pull request state" }), {
      button: 0,
      ctrlKey: false,
    });
    return screen.findByRole("menu");
  };
  /** The comment box's draft for this PR, as an unsent comment would leave it. */
  function Draft({ body }: { body: string }) {
    const composer = usePullRequestCommentComposer(SESSION, PR);
    const { setBody } = composer;
    useEffect(() => {
      setBody(body);
    }, [setBody, body]);
    return <output aria-label="Unsent comment">{composer.draft.body}</output>;
  }

  it("closes from the state pill alone, leaving the box's unsent comment as it was", async () => {
    const sent = await installShell({ pull_request_close: () => stateAnswer("CLOSED") });
    const view = render(
      <>
        <Page facts={pullRequest()} />
        <Draft body="Superseded by #12" />
      </>,
      { wrapper: setup().wrapper },
    );
    await openPill();
    fireEvent.click(await screen.findByRole("menuitem", { name: "Close Pull Request" }));
    await waitFor(() =>
      expect(sent.map((write) => write.operation)).toEqual(["pull_request_close"]),
    );
    expect(screen.getByRole("status", { name: "Unsent comment" })).toHaveTextContent(
      "Superseded by #12",
    );
    view.unmount();
  });

  it("shimmers the state pill while Close is on its way, and says so", async () => {
    let answer!: (value: unknown) => void;
    await installShell({
      pull_request_close: () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    });
    const view = render(<Page facts={pullRequest()} />, { wrapper: setup().wrapper });
    const pill = screen.getByRole("button", { name: "Pull request state" });
    expect(pill).not.toHaveAttribute("aria-busy");
    await openPill();
    fireEvent.click(await screen.findByRole("menuitem", { name: "Close Pull Request" }));
    await waitFor(() => expect(pill).toHaveAttribute("aria-busy", "true"));
    expect(pill.querySelector("[data-pending]")).not.toBeNull();
    expect(pill).toHaveAttribute("aria-description", "Closing…");
    expect(screen.getAllByRole("status").some((s) => s.textContent === "Closing…")).toBe(true);
    await act(async () => answer(stateAnswer("CLOSED")));
    await waitFor(() => expect(pill).not.toHaveAttribute("aria-busy"));
    expect(pill.querySelector("[data-pending]")).toBeNull();
    view.unmount();
  });

  it("marks the state pill when Close fails, until Try Again closes it", async () => {
    let answers = [
      () => refusal("forbidden", "You can't close this pull request."),
      () => stateAnswer("CLOSED"),
    ];
    await installShell({ pull_request_close: () => answers.shift()!() });
    const view = render(<Page facts={pullRequest()} />, { wrapper: setup().wrapper });
    await openPill();
    fireEvent.click(await screen.findByRole("menuitem", { name: "Close Pull Request" }));
    await screen.findByRole("alert");
    const pill = screen.getByRole("button", { name: "Pull request state" });
    expect(within(pill).getByTestId("state-pill-failed")).toBeTruthy();
    expect(pill).toHaveAttribute("aria-description", "Couldn’t close the pull request");
    fireEvent.click(screen.getByRole("button", { name: "Try Again" }));
    await waitFor(() => expect(within(pill).queryByTestId("state-pill-failed")).toBeNull());
    answers = [];
    view.unmount();
  });

  it("offers Reopen for a closed PR, and nothing for a merged one", async () => {
    await installShell({});
    const closed = render(<Page facts={pullRequest({ state: "CLOSED" })} />, {
      wrapper: setup().wrapper,
    });
    await openPill();
    expect(screen.getByRole("menuitem", { name: "Reopen Pull Request" })).toBeTruthy();
    expect(screen.queryByRole("menuitem", { name: "Close Pull Request" })).toBeNull();
    closed.unmount();
    const merged = render(<Page facts={pullRequest({ state: "MERGED" })} />, {
      wrapper: setup().wrapper,
    });
    expect(screen.queryByRole("button", { name: "Pull request state" })).toBeNull();
    merged.unmount();
  });

  it("links Close to GitHub where this Mac can't close it, with no ⌘K row", async () => {
    await installShell({}, { writes: ["pull_request_convert_to_draft"] });
    const linked = render(<Page facts={pullRequest()} />, { wrapper: setup().wrapper });
    await openPill();
    expect(screen.getByRole("menuitem", { name: "Close on GitHub" })).toHaveAttribute("href", PR);
    expect(paletteRow("Close Pull Request")).toBeUndefined();
    linked.unmount();
  });

  it("shows a refusal under the header with no comment box, and Try Again closes it", async () => {
    let answers = [
      () => refusal("forbidden", "You can't close this pull request."),
      () => stateAnswer("CLOSED"),
    ];
    const sent = await installShell({ pull_request_close: () => answers.shift()!() });
    const view = render(<Page facts={pullRequest()} />, { wrapper: setup().wrapper });
    await openPill();
    fireEvent.click(await screen.findByRole("menuitem", { name: "Close Pull Request" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The pull request wasn't closed");
    expect(paletteRow("Try Closing Again")).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: "Try Again" }));
    await waitFor(() =>
      expect(sent.map((write) => write.operation)).toEqual([
        "pull_request_close",
        "pull_request_close",
      ]),
    );
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    answers = [];
    view.unmount();
  });

  it("holds Close and Convert to draft on an unknown outcome until Check Again reads it", async () => {
    await installShell({ pull_request_close: unknown });
    const harness = setup();
    const view = render(<Page facts={pullRequest()} />, { wrapper: harness.wrapper });
    await openPill();
    fireEvent.click(await screen.findByRole("menuitem", { name: "Close Pull Request" }));
    await screen.findByRole("button", { name: "Check Again" });
    await openPill();
    expect(screen.getByRole("menuitem", { name: "Close Pull Request" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(screen.getByRole("menuitem", { name: "Convert to draft…" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(paletteRow("Close Pull Request")?.availability).toBeDefined();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    // GitHub did close it: a fresh read says so, and the hold clears.
    harness.queryClient.setQueryData(infoKey(), info({ state: "CLOSED" }));
    fireEvent.click(screen.getByRole("button", { name: "Check Again" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Check Again" })).toBeNull());
    view.unmount();
  });

  it("offers ⌘K Close once, from the page's owner, and runs it alone", async () => {
    const sent = await installShell({ pull_request_close: () => stateAnswer("CLOSED") });
    const view = render(<Page facts={pullRequest()} />, { wrapper: setup().wrapper });
    await waitFor(() => expect(paletteRow("Close Pull Request")).toBeDefined());
    const rows = Object.values(usePaletteCommandsStore.getState().groups)
      .flatMap((group) => group.commands)
      .filter((command) => command.label === "Close Pull Request");
    expect(rows).toHaveLength(1);
    act(() => rows[0].run());
    await waitFor(() =>
      expect(sent.map((write) => write.operation)).toEqual(["pull_request_close"]),
    );
    view.unmount();
    // A page that isn't shown offers nothing.
    const hidden = render(<Page facts={pullRequest()} active={false} />, {
      wrapper: setup().wrapper,
    });
    expect(paletteRow("Close Pull Request")).toBeUndefined();
    hidden.unmount();
  });
});
