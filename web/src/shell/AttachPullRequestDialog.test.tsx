import type * as IdentityModule from "@/lib/identity";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import type { PullRequestInfo } from "@/newPullRequestPage/hooks/usePullRequests";
import type * as UseSessionPullRequestsModule from "@/newPullRequestPage/hooks/useSessionPullRequests";
import type * as UseWorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import { SESSION_PULL_REQUEST_LIMIT_REASON } from "@/lib/sessionPullRequestMembership";
import { openPullRequestAttach, usePullRequestAttachStore } from "@/store/pullRequestAttach";

const state = vi.hoisted(() => ({
  info: undefined as PullRequestInfo | undefined,
  stored: { prs: [], synced: true } as { prs: PullRequestInfo["prs"]; synced: boolean },
  storedRefreshFailed: false,
  serveable: true as boolean | undefined,
  sandboxAsleep: false,
  attach: vi.fn(),
  toast: vi.fn(),
}));

vi.mock("@/newPullRequestPage/hooks/usePullRequests", () => ({
  usePullRequestInfo: () => ({ data: state.info, error: null }),
  useUpdateSessionPr: () => ({ mutateAsync: state.attach }),
}));
vi.mock("@/newPullRequestPage/hooks/useSessionPullRequests", async (importOriginal) => {
  const original = await importOriginal<typeof UseSessionPullRequestsModule>();
  return {
    ...original,
    // The server keeps every link the host answered, so the read after one shows it.
    readSessionPullRequestsAgain: async (client: QueryClient, id: string) => {
      const answered = Object.values(
        useSessionPullRequestIntentStore.getState().bySession[id] ?? {},
      ).filter((intent) => intent.action === "link" && intent.requestDone);
      const linked = answered.map((intent) => ({ url: intent.url, relationship: "attached" }));
      await client
        .fetchQuery({
          queryKey: original.sessionPullRequestsKey(id),
          queryFn: async () => ({ prs: [...(state.stored?.prs ?? []), ...linked], synced: true }),
          staleTime: 0,
        })
        .catch(() => {});
    },
    storesSessionPullRequests: () => true,
    // A refetch that fails keeps the last list it had, as React Query does.
    useSessionPullRequests: () => ({ data: state.stored, isError: state.storedRefreshFailed }),
  };
});
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => ({
  ...(await importOriginal<typeof UseWorkspaceChangedFilesModule>()),
  useWorkspaceAsleep: () => state.sandboxAsleep,
  useWorkspaceServeable: () => state.serveable,
}));
vi.mock("@/hooks/useSandboxResume", () => ({
  useSandboxResume: () => ({
    status: state.sandboxAsleep ? "asleep" : "none",
    failure: null,
    availability: { available: true },
    resume: () => {},
  }),
}));
vi.mock("@/components/ui/toast", () => ({ showToast: state.toast }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
}));

import { resetCodeHostContextReads } from "@/lib/codeHostContext";
import { resetWorkspaceFastTrackForTests } from "@/lib/workspaceFastTrack";
import { AttachPullRequestDialog } from "./AttachPullRequestDialog";
import {
  resetPullRequestIntentsForTests,
  useSessionPullRequestIntentStore,
} from "@/store/sessionPullRequestIntents";
import { EXAMPLE_DEVICE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// Example Host stand-in hosts are recognized by name, as a build that registers Example Host does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_DEVICE_HOST] });

const otherSession = {
  id: "other",
  title: "Login fixes",
  updated_at: 0,
  pull_request: {
    url: "https://github.com/acme/app/pull/12",
    provider: "github",
    repository: "acme/app",
    number: 12,
    title: "Fix login redirect",
    state: "OPEN",
  },
} as Conversation;

function renderDialog() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <AttachPullRequestDialog conversations={[otherSession]} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
  act(() => openPullRequestAttach("current"));
  return screen.findByRole("combobox", { name: "Link a pull request" });
}

/** The host refusing a link, as the server relays it: a message with its HTTP status. */
function hostRefusal(message: string, status = 400) {
  return Object.assign(new Error(message), { status });
}

function activeOption(input: HTMLElement) {
  const id = input.getAttribute("aria-activedescendant");
  return id ? document.getElementById(id) : null;
}

beforeEach(() => {
  resetPullRequestIntentsForTests();
  usePullRequestAttachStore.setState({
    openSessionId: null,
    drafts: {},
    attempts: {},
    lastAttached: null,
  });
  state.serveable = true;
  state.sandboxAsleep = false;
  state.storedRefreshFailed = false;
  state.attach.mockReset();
  state.toast.mockReset();
  // The server answers info with its stored list, so both name #1.
  state.stored = {
    prs: [
      {
        url: "https://github.com/acme/app/pull/1",
        host: "github.com",
        repository: "acme/app",
        number: 1,
        relationship: "attached",
      },
    ],
    synced: true,
  };
  state.info = {
    object: "session.github.info",
    available: true,
    provider: "github",
    tracking_available: true,
    repo: { name_with_owner: "acme/app" },
    prs: [
      {
        url: "https://github.com/acme/app/pull/1",
        host: "github.com",
        repository: "acme/app",
        number: 1,
        relationship: "attached",
      },
    ],
    pr: {
      url: "https://github.com/acme/app/pull/2",
      number: 2,
      title: "Branch work",
      state: "OPEN",
      is_draft: false,
    } as PullRequestInfo["pr"],
  };
});

afterEach(() => {
  cleanup();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  resetWorkspaceFastTrackForTests();
  resetCodeHostContextReads();
});

const OWN_ACCOUNT = { login: "octo", active: true, state: "success", host: "github.com" };

function suggestion(number: number, title: string, extra: Record<string, unknown> = {}) {
  return {
    url: `https://github.com/acme/app/pull/${number}`,
    number,
    title,
    state: "OPEN",
    is_draft: false,
    repository: "acme/app",
    updated_at: "2026-11-03T10:00:00Z",
    ...extra,
  };
}

/** A desktop shell whose fast track answers `suggestions` for pull_request_suggestions. */
function installFastTrackShell(
  suggestions: unknown,
  operations = ["account_status", "pull_request_suggestions"],
) {
  const githubQuery = vi.fn(async (operation: string, account: { login: string }) =>
    operation === "account_status" ? { ok: true, data: { login: account.login } } : suggestions,
  );
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    githubQuery,
    getDesktopFeatures: async () => ({
      githubFastTrack: true,
      githubFastTrackOperations: operations,
    }),
  };
  return githubQuery;
}

function signInHost() {
  state.info = {
    ...state.info!,
    auth: {
      authenticated: true,
      hint: null,
      cli: null,
      accounts: [OWN_ACCOUNT],
      selected_account: "octo",
    },
  } as PullRequestInfo;
}

describe("AttachPullRequestDialog", () => {
  it("suggests the branch PR and PRs from other sessions", async () => {
    await renderDialog();
    expect(screen.getByText("Current branch")).toBeVisible();
    expect(screen.getByRole("option", { name: /Branch work/ })).toBeVisible();
    expect(screen.getByText("From other sessions")).toBeVisible();
    expect(screen.getByRole("option", { name: /Fix login redirect/ })).toHaveTextContent(
      "acme/app#12 · Login fixes",
    );
  });

  it("links a typed number, closes, selects it, and confirms", async () => {
    state.attach.mockResolvedValue({
      ...state.info,
      selected_pr_url: "https://github.com/acme/app/pull/42",
    });
    const input = await renderDialog();
    await userEvent.type(input, "#42");
    expect(activeOption(input)).toHaveTextContent("Link acme/app#42");
    await userEvent.keyboard("{Enter}");
    expect(state.attach).toHaveBeenCalledWith({
      url: "https://github.com/acme/app/pull/42",
      action: "attach",
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(usePullRequestAttachStore.getState().lastAttached).toEqual({
      sessionId: "current",
      url: "https://github.com/acme/app/pull/42",
    });
    expect(usePullRequestAttachStore.getState().drafts.current).toBeUndefined();
    expect(state.toast).toHaveBeenCalledWith("Linked acme/app#42");
  });

  describe("a bare number before any PR is linked", () => {
    // A host whose gh reaches the repository reports it, but no account or PR.
    beforeEach(() => {
      state.info = { ...state.info!, prs: [], pr: null };
      state.stored = { prs: [], synced: true };
    });

    const remoteContext = (host: string) => ({
      ok: true,
      status: 200,
      route: "workspace-helper",
      body: {
        status: "ok",
        context_key: `key-${host}`,
        branch: "feature",
        head_sha: null,
        push: null,
        push_unresolved: null,
        remotes: [{ name: "origin", host, owner: "acme", repo: "app" }],
        unsupported_remote_count: 0,
        truncated_remotes: false,
      },
    });

    /**
     * A desktop shell that reads the workspace's remotes. Each read waits for
     * the next answer given, in order; answers given early are used at once.
     */
    function repositoryContextShell() {
      const answers: unknown[] = [];
      const waiting: ((reply: unknown) => void)[] = [];
      const workspaceRequest = vi.fn(
        () =>
          new Promise((resolve) => {
            if (answers.length > 0) resolve(answers.shift());
            else waiting.push(resolve);
          }),
      );
      (window as unknown as Record<string, unknown>).omnigentDesktop = {
        kind: "electron",
        workspaceRequest,
        getDesktopFeatures: async () => ({
          workspaceFastTrack: true,
          workspaceFastTrackCapabilities: ["repository_context"],
        }),
      };
      const answer = (reply: unknown) => {
        const next = waiting.shift();
        if (next) next(reply);
        else answers.push(reply);
      };
      return {
        workspaceRequest,
        remoteOn: (host: string) => answer(remoteContext(host)),
        cantRead: () => answer({ ok: false, reason: "unsupported" }),
        failed: () =>
          answer({ ok: true, status: 200, route: "workspace-helper", body: { status: "failed" } }),
      };
    }

    it("links it on the workspace remote's host", async () => {
      repositoryContextShell().remoteOn("ghe.example.com");
      state.attach.mockResolvedValue(state.info);
      const input = await renderDialog();
      await userEvent.type(input, "250");
      await waitFor(() => expect(activeOption(input)).toHaveTextContent("Link acme/app#250"));
      await userEvent.keyboard("{Enter}");
      expect(state.attach).toHaveBeenCalledWith({
        url: "https://ghe.example.com/acme/app/pull/250",
        action: "attach",
      });
    });

    it("waits for the remote before linking on the signed-in account's host", async () => {
      signInHost();
      const shell = repositoryContextShell();
      state.attach.mockResolvedValue(state.info);
      const input = await renderDialog();
      await userEvent.type(input, "250{Enter}");
      expect(state.attach).not.toHaveBeenCalled();
      expect(screen.getByText("Checking this workspace’s git remote…")).toBeVisible();

      shell.remoteOn("ghe.example.com");
      await waitFor(() => expect(activeOption(input)).toHaveTextContent("Link acme/app#250"));
      await userEvent.keyboard("{Enter}");
      expect(state.attach).toHaveBeenCalledWith({
        url: "https://ghe.example.com/acme/app/pull/250",
        action: "attach",
      });
    });

    it("offers no suggestion for a number that waits, even one that matches it", async () => {
      signInHost();
      const shell = repositoryContextShell();
      state.attach.mockResolvedValue(state.info);
      const input = await renderDialog();
      // Another session's acme/app#12 matches both, by number and fuzzily.
      for (const typed of ["1", "12"]) {
        // oxlint-disable-next-line no-await-in-loop -- each input is typed and checked in turn.
        await userEvent.clear(input);
        // oxlint-disable-next-line no-await-in-loop -- each input is typed and checked in turn.
        await userEvent.type(input, `${typed}{Enter}`);
        expect(screen.queryByRole("option")).toBeNull();
        expect(state.attach).not.toHaveBeenCalled();
      }
      expect(
        screen.getByText("Checking which server hosts acme/app before linking 12…", {
          selector: "[data-slot=quick-pick-empty]",
        }),
      ).toBeVisible();

      shell.remoteOn("ghe.example.com");
      await waitFor(() => expect(activeOption(input)).toHaveTextContent("Link acme/app#12"));
      await userEvent.keyboard("{Enter}");
      expect(state.attach).toHaveBeenCalledWith({
        url: "https://ghe.example.com/acme/app/pull/12",
        action: "attach",
      });
    });

    it("waits for a new read on reopen instead of trusting the last answer", async () => {
      signInHost();
      const shell = repositoryContextShell();
      shell.failed();
      state.attach.mockResolvedValue(state.info);
      let input = await renderDialog();
      await userEvent.type(input, "250");
      // The remotes couldn't be read, so the account's host is offered.
      await waitFor(() =>
        expect(activeOption(input)).toHaveTextContent("https://github.com/acme/app/pull/250"),
      );
      await userEvent.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

      act(() => openPullRequestAttach("current"));
      input = await screen.findByRole("combobox", { name: "Link a pull request" });
      await waitFor(() => expect(shell.workspaceRequest).toHaveBeenCalledTimes(2));
      await userEvent.keyboard("{Enter}");
      expect(state.attach).not.toHaveBeenCalled();
      expect(screen.getByText("Checking this workspace’s git remote…")).toBeVisible();

      shell.remoteOn("ghe.example.com");
      await waitFor(() =>
        expect(activeOption(input)).toHaveTextContent("https://ghe.example.com/acme/app/pull/250"),
      );
      await userEvent.keyboard("{Enter}");
      expect(state.attach).toHaveBeenCalledWith({
        url: "https://ghe.example.com/acme/app/pull/250",
        action: "attach",
      });
    });

    it("uses the signed-in account's host once the shell can't read the remote", async () => {
      signInHost();
      const shell = repositoryContextShell();
      state.attach.mockResolvedValue(state.info);
      const input = await renderDialog();
      await userEvent.type(input, "250");
      shell.cantRead();
      await waitFor(() => expect(activeOption(input)).toHaveTextContent("Link acme/app#250"));
      expect(screen.queryByText("Checking this workspace’s git remote…")).toBeNull();
      await userEvent.keyboard("{Enter}");
      expect(state.attach).toHaveBeenCalledWith({
        url: "https://github.com/acme/app/pull/250",
        action: "attach",
      });
    });

    it("says why it can't, with the repository's own link shape, where the remote can't be read", async () => {
      const input = await renderDialog();
      await userEvent.type(input, "250");
      await waitFor(() =>
        expect(input).toHaveAccessibleDescription(
          "Paste the full link, like https://github.com/acme/app/pull/250. It isn’t known yet which GitHub server hosts acme/app.",
        ),
      );
      await userEvent.keyboard("{Enter}");
      expect(state.attach).not.toHaveBeenCalled();
    });
  });

  it("hints at the repository's own link shape, with no Example Host example", async () => {
    const input = await renderDialog();
    await userEvent.type(input, "nothing matches this");
    const empty = await screen.findByText(/^No matching pull requests\./, {
      selector: "[data-slot=quick-pick-empty]",
    });
    expect(empty).toHaveTextContent(
      "No matching pull requests. Paste a link like https://github.com/acme/app/pull/123, or type #123.",
    );
    expect(empty).not.toHaveTextContent("named-host");
  });

  it("keeps the picker open with the host's error when linking fails", async () => {
    state.attach.mockRejectedValue(hostRefusal("You don’t have access to acme/app#42."));
    const input = await renderDialog();
    await userEvent.type(input, "#42{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You don’t have access to acme/app#42.",
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await userEvent.type(input, "3");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("offers the branch PR the host inferred, since it isn't linked yet", async () => {
    state.info!.prs!.push({
      url: "https://github.com/acme/app/pull/2",
      host: "github.com",
      repository: "acme/app",
      number: 2,
      relationship: "inferred",
    });
    const input = await renderDialog();
    expect(screen.getByRole("option", { name: /Branch work/ })).toBeVisible();
    await userEvent.type(input, "#2");
    expect(activeOption(input)).toHaveTextContent("Link acme/app#2");
  });

  function deferAttach() {
    const pending: { resolve: (value: unknown) => void; reject: (error: Error) => void }[] = [];
    state.attach.mockImplementation(
      () =>
        new Promise((resolve, reject) => {
          pending.push({ resolve, reject });
        }),
    );
    return pending;
  }

  it("keeps a link in flight while the query is edited and ignores a repeat Enter", async () => {
    const pending = deferAttach();
    const input = await renderDialog();
    await userEvent.type(input, "#42{Enter}");
    expect(await screen.findByText("Linking…")).toBeVisible();
    await userEvent.type(input, "9");
    await userEvent.keyboard("{Enter}");
    expect(screen.getByText("Linking…")).toBeVisible();
    expect(state.attach).toHaveBeenCalledTimes(1);

    await act(async () =>
      pending[0].resolve({ ...state.info, selected_pr_url: "https://github.com/acme/app/pull/42" }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(usePullRequestAttachStore.getState().lastAttached).toEqual({
      sessionId: "current",
      url: "https://github.com/acme/app/pull/42",
    });
    expect(state.toast).toHaveBeenCalledWith("Linked acme/app#42");
  });

  it("confirms a link that finishes after the picker was closed", async () => {
    const pending = deferAttach();
    const input = await renderDialog();
    await userEvent.type(input, "#42{Enter}");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await act(async () => pending[0].resolve({ ...state.info }));
    expect(usePullRequestAttachStore.getState().lastAttached?.url).toBe(
      "https://github.com/acme/app/pull/42",
    );
    expect(state.toast).toHaveBeenCalledWith("Linked acme/app#42");
  });

  it("keeps another session's picker open when an earlier link finishes", async () => {
    const pending = deferAttach();
    const input = await renderDialog();
    await userEvent.type(input, "#42{Enter}");
    act(() => {
      openPullRequestAttach("other");
      usePullRequestAttachStore.getState().setDraft("other", "#7");
    });
    await act(async () => pending[0].resolve({ ...state.info }));

    const store = usePullRequestAttachStore.getState();
    expect(store.openSessionId).toBe("other");
    expect(store.drafts.other).toBe("#7");
    expect(store.lastAttached).toEqual({
      sessionId: "current",
      url: "https://github.com/acme/app/pull/42",
    });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  // Closing, opening another session's picker, and coming back remounts the picker.
  async function visitOtherSessionAndReturn() {
    await userEvent.keyboard("{Escape}");
    act(() => openPullRequestAttach("other"));
    await userEvent.keyboard("{Escape}");
    act(() => openPullRequestAttach("current"));
    return screen.findByRole("combobox", { name: "Link a pull request" });
  }

  it("keeps a pending link across a visit to another session's picker", async () => {
    const pending = deferAttach();
    let input = await renderDialog();
    await userEvent.type(input, "#42{Enter}");
    input = await visitOtherSessionAndReturn();
    expect(screen.getByText("Linking…")).toBeVisible();
    await userEvent.type(input, "3{Enter}");
    expect(state.attach).toHaveBeenCalledTimes(1);

    await act(async () => pending[0].resolve({ ...state.info }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const store = usePullRequestAttachStore.getState();
    expect(store.lastAttached?.url).toBe("https://github.com/acme/app/pull/42");
    expect(store.drafts.current).toBe("#423");
    expect(state.toast).toHaveBeenCalledTimes(1);
  });

  it("shows a failure after a visit to another session's picker, and retries", async () => {
    const pending = deferAttach();
    const input = await renderDialog();
    await userEvent.type(input, "#42{Enter}");
    await visitOtherSessionAndReturn();
    await act(async () => pending[0].reject(hostRefusal("GitHub said no.")));
    expect(await screen.findByRole("alert")).toHaveTextContent("GitHub said no.");

    expect(await visitOtherSessionAndReturn()).toHaveValue("#42");
    expect(screen.getByRole("alert")).toHaveTextContent("GitHub said no.");
    await userEvent.keyboard("{Enter}");
    await userEvent.keyboard("{Enter}");
    expect(state.attach).toHaveBeenCalledTimes(2);
    await act(async () => pending[1].resolve({ ...state.info }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(state.toast).toHaveBeenCalledTimes(1);
  });

  it("links a pasted link from another provider as the host reports it", async () => {
    state.attach.mockResolvedValue({ ...state.info });
    const input = await renderDialog();
    await userEvent.type(input, "https://gitlab.com/acme/app/-/merge_requests/42/");
    expect(activeOption(input)).toHaveTextContent(
      "Link https://gitlab.com/acme/app/-/merge_requests/42",
    );
    await userEvent.keyboard("{Enter}");
    expect(state.attach).toHaveBeenCalledWith({
      url: "https://gitlab.com/acme/app/-/merge_requests/42",
      action: "attach",
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("keeps a failure visible until the query changes, and retries on Enter", async () => {
    const pending = deferAttach();
    const input = await renderDialog();
    await userEvent.type(input, "#42{Enter}");
    await act(async () => pending[0].reject(hostRefusal("GitHub said no.")));
    expect(await screen.findByRole("alert")).toHaveTextContent("GitHub said no.");

    await userEvent.keyboard("{Enter}");
    expect(state.attach).toHaveBeenCalledTimes(2);
    expect(await screen.findByText("Linking…")).toBeVisible();
    await act(async () => pending[1].resolve({ ...state.info }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("offers to link a PR only the host's raw rows name, once the server's synced list lacks it", async () => {
    // A server that couldn't store the host's report answers info with the host's raw rows.
    state.info = {
      ...state.info!,
      prs: [
        ...(state.info!.prs ?? []),
        {
          url: "https://github.com/acme/app/pull/3",
          host: "github.com",
          repository: "acme/app",
          number: 3,
          relationship: "worked_on",
        },
      ],
    };
    const input = await renderDialog();
    await userEvent.type(input, "#3");
    expect(input).not.toHaveAccessibleDescription(
      "That pull request is already linked to this session.",
    );
    expect(activeOption(input)).toHaveTextContent("Link acme/app#3");
  });

  it("keeps the synced list the truth when its refresh failed", async () => {
    state.storedRefreshFailed = true;
    state.info = {
      ...state.info!,
      prs: [
        ...(state.info!.prs ?? []),
        {
          url: "https://github.com/acme/app/pull/3",
          host: "github.com",
          repository: "acme/app",
          number: 3,
          relationship: "worked_on",
        },
      ],
    };
    const input = await renderDialog();
    await userEvent.type(input, "#3");
    expect(activeOption(input)).toHaveTextContent("Link acme/app#3");
  });

  describe("a session that links 50 pull requests", () => {
    const fifty = () =>
      Array.from({ length: 50 }, (_, index) => ({
        url: `https://github.com/acme/app/pull/${100 + index}`,
        host: "github.com",
        repository: "acme/app",
        number: 100 + index,
        relationship: "attached" as const,
      }));

    beforeEach(() => {
      state.stored = { prs: fifty(), synced: true };
      state.info = { ...state.info!, prs: fifty() };
    });

    it("says why it can't link another, and sends nothing", async () => {
      const input = await renderDialog();
      await userEvent.type(input, "#999");
      expect(input).toHaveAccessibleDescription(SESSION_PULL_REQUEST_LIMIT_REASON);
      await userEvent.keyboard("{Enter}");
      expect(state.attach).not.toHaveBeenCalled();
      // The footer says so too, before anything is typed.
      expect(
        screen.getAllByText(SESSION_PULL_REQUEST_LIMIT_REASON, { selector: "span.truncate" }),
      ).toHaveLength(1);
    });

    it("still says a PR it links is already linked", async () => {
      const input = await renderDialog();
      await userEvent.type(input, "#100");
      expect(input).toHaveAccessibleDescription(
        "That pull request is already linked to this session.",
      );
    });
  });

  it("explains already-linked and invalid input inline", async () => {
    const input = await renderDialog();
    await userEvent.type(input, "#1");
    expect(input).toHaveAccessibleDescription(
      "That pull request is already linked to this session.",
    );
    await userEvent.clear(input);
    await userEvent.type(input, "https://github.com/acme/app/issues/5");
    expect(input).toHaveAttribute("aria-invalid", "true");
  });

  it.each([
    {
      offline: "the host is disconnected",
      serveable: false,
      sandboxAsleep: false,
      message: "To link a PR, please reconnect the session’s host.",
    },
    {
      offline: "the sandbox is asleep",
      serveable: true,
      sandboxAsleep: true,
      message: "To link a PR, resume the session’s sandbox.",
    },
  ])("asks for the host instead of linking while $offline", async (offline) => {
    state.serveable = offline.serveable;
    state.sandboxAsleep = offline.sandboxAsleep;
    const input = await renderDialog();
    expect(
      screen.getByText(offline.message, { selector: "[data-slot=quick-pick-empty]" }),
    ).toBeVisible();
    expect(screen.queryByRole("option")).toBeNull();
    await userEvent.type(input, "#42{Enter}");
    expect(input).toHaveAccessibleDescription(offline.message);
    expect(state.attach).not.toHaveBeenCalled();
  });

  it("suggests GitHub's PRs for the host's account, after the branch PR and without repeats", async () => {
    signInHost();
    const githubQuery = installFastTrackShell({
      ok: true,
      data: {
        repository: [suggestion(2, "Branch work"), suggestion(30, "Speed up search")],
        mine: [
          suggestion(31, "My draft", {
            is_draft: true,
            repository: "acme/tools",
            url: "https://github.com/acme/tools/pull/31",
          }),
        ],
        recent: [
          suggestion(1, "Already linked"),
          suggestion(32, "Shipped it", { state: "MERGED" }),
        ],
      },
    });
    await renderDialog();
    await screen.findByText("Open in this repository");
    expect(githubQuery).toHaveBeenCalledWith(
      "pull_request_suggestions",
      { host: "github.com", login: "octo" },
      { repository: "acme/app" },
    );
    const headings = [
      "Current branch",
      "Open in this repository",
      "Your open pull requests",
      "Recently updated",
      "From other sessions",
    ];
    const text = document.body.textContent ?? "";
    const positions = headings.map((heading) => text.indexOf(heading));
    expect(positions.every((position) => position >= 0)).toBe(true);
    expect([...positions].sort((a, b) => a - b)).toEqual(positions);
    // The branch PR shows once, under its own group; the linked PR not at all.
    expect(screen.getAllByRole("option", { name: /Branch work/ })).toHaveLength(1);
    expect(screen.queryByRole("option", { name: /Already linked/ })).toBeNull();
    expect(screen.getByRole("option", { name: /My draft/ })).toHaveTextContent("acme/tools#31");
    expect(screen.getByRole("option", { name: /Shipped it/ })).toHaveTextContent("Merged");
  });

  it("says why suggestions didn't load, and still links a pasted number", async () => {
    signInHost();
    installFastTrackShell({
      ok: false,
      error: {
        code: "signed_out",
        message: "GitHub CLI isn't signed in to this host on this Mac. Run gh auth login.",
      },
    });
    const input = await renderDialog();
    expect(
      await screen.findByText(/Couldn’t load suggestions\. GitHub CLI isn't signed in/),
    ).toBeVisible();
    await userEvent.type(input, "#42");
    expect(activeOption(input)).toHaveTextContent("Link acme/app#42");
  });

  it("keeps the paste-only picker without the host's account or the shell operation", async () => {
    // No selected account: never guess one.
    const githubQuery = installFastTrackShell({
      ok: true,
      data: { repository: [], mine: [], recent: [] },
    });
    await renderDialog();
    expect(screen.getByText("Current branch")).toBeVisible();
    cleanup();
    // A shell that predates the operation (it lists only the commits reads).
    signInHost();
    const olderQuery = installFastTrackShell(
      { ok: true, data: { repository: [], mine: [], recent: [] } },
      ["account_status", "pull_request_commits", "commit_diff"],
    );
    await renderDialog();
    // Let the shell's feature check settle; it never shows progress for suggestions.
    await act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 20);
      });
    });
    expect(screen.queryByText(/Finding pull requests/)).toBeNull();
    expect(screen.queryByText(/Couldn’t load suggestions/)).toBeNull();
    expect(screen.getByText("Current branch")).toBeVisible();
    expect(olderQuery).not.toHaveBeenCalledWith(
      "pull_request_suggestions",
      expect.anything(),
      expect.anything(),
    );
    expect(githubQuery).not.toHaveBeenCalledWith(
      "pull_request_suggestions",
      expect.anything(),
      expect.anything(),
    );
  });
});

describe("a typed number resolved from the checkout's repositories", () => {
  const ENTERPRISE = "ghe.acme.com";

  /** A shell serving a checkout with a github.com remote and one on an unknown host. */
  function installContextShell() {
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      workspaceRequest: async (request: { url: string }) =>
        String(request.url).endsWith("/repository-context")
          ? {
              ok: true,
              status: 200,
              route: "workspace-helper",
              body: {
                status: "ok",
                context_key: "ctx-1",
                branch: "feature",
                head_sha: "a".repeat(40),
                push: null,
                push_unresolved: null,
                remotes: [
                  { name: "origin", host: "github.com", owner: "acme", repo: "tools" },
                  { name: "enterprise", host: ENTERPRISE, owner: "acme", repo: "internal" },
                ],
                unsupported_remote_count: 0,
                truncated_remotes: false,
              },
            }
          : { ok: false, reason: "unsupported" },
      getDesktopFeatures: async () => ({
        workspaceFastTrack: true,
        workspaceFastTrackCapabilities: ["repository_context"],
      }),
    };
  }

  beforeEach(() => {
    // The session's machine doesn't say which repository this is.
    state.info = { ...state.info!, repo: null, prs: [] } as unknown as PullRequestInfo;
    state.stored = { prs: [], synced: true };
  });

  afterEach(() => {
    resetCodeHostContextReads();
    resetWorkspaceFastTrackForTests();
  });

  it("names the one GitHub repository, leaving out a remote on a host not known to serve it", async () => {
    installContextShell();
    const input = await renderDialog();
    await userEvent.type(input, "#5");
    expect(await screen.findByRole("option", { name: /acme\/tools#5/ })).toBeVisible();
  });
});
