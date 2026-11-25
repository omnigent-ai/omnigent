import { afterEach, describe, expect, it, vi } from "vitest";

import type { PullRequestInfo } from "@/newPullRequestPage/hooks/usePullRequests";
import type { CodeHostContext } from "@/lib/codeHostContext";
import type * as IdentityModule from "@/lib/identity";
import {
  type BranchDiscovery,
  branchChoiceCommands,
  branchFacts,
  chooseBranchPullRequest,
  contextRepositories,
  discoverBranchPullRequest,
  MAX_BRANCH_REPOSITORIES,
  repositoryContextKey,
  resetBranchChoicesForTests,
  withBranchOnStoredList,
  withBranchPullRequest,
} from "./branchPullRequest";
import { resetDesktopReadPathForTests } from "./desktopReadPath";
import { EXAMPLE_SPOKE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// GitSpoke stand-in hosts are recognized by name, as a build that registers GitSpoke does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_SPOKE_HOST] });

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
}));

type Ok = Extract<CodeHostContext, { status: "ok" }>;
const HEAD = "a".repeat(40);

function context(overrides: Partial<Ok> = {}): Ok {
  return {
    status: "ok",
    context_key: "ctx-1",
    branch: "feature",
    head_sha: HEAD,
    push: {
      remote_name: "fork",
      branch: "feature",
      host: "github.com",
      owner: "me",
      repo: "app",
      tracking_sha: HEAD,
    },
    push_unresolved: null,
    remotes: [
      { name: "origin", host: "github.com", owner: "acme", repo: "app" },
      { name: "fork", host: "github.com", owner: "me", repo: "app" },
    ],
    unsupported_remote_count: 0,
    truncated_remotes: false,
    ...overrides,
  };
}

const candidate = (number: number, overrides = {}) => ({
  url: `https://github.com/acme/app/pull/${number}`,
  number,
  title: `PR ${number}`,
  state: "OPEN",
  is_draft: false,
  base_repository: "acme/app",
  head_repository: "me/app",
  head_ref: "feature",
  head_sha: HEAD,
  ...overrides,
});

/** A shell answering branch_pull_requests with `answer`, optionally keeping choices. */
function installShell(answer: unknown, { remembers = true } = {}) {
  const choices: unknown[] = [];
  const codeHostQuery = vi.fn(async () => ({
    ok: true,
    data: answer,
    identity: { host: "github.com", login: "octo", generation: 1 },
  }));
  const localPullRequestLinks = vi.fn(async (request: Record<string, unknown>) => {
    if (request.action === "choose_branch") {
      const { branch, repositoryContext, provider, host, choice } = request;
      if (choice) {
        choices.push({
          branch,
          repository_context: repositoryContext,
          provider,
          host,
          choice,
          chosen_at: 1,
        });
      }
      return { ok: true, data: choices };
    }
    return { ok: true, data: choices };
  });
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery,
    localPullRequestLinks,
    getDesktopFeatures: async () => ({
      codeHosts: [{ id: "github", operations: ["branch_pull_requests"], accounts: true }],
      localPullRequestLinks: true,
      ...(remembers ? { localPullRequestBranchChoices: 1 } : {}),
    }),
  };
  return { codeHostQuery, localPullRequestLinks };
}

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  resetDesktopReadPathForTests();
  resetBranchChoicesForTests();
});

describe("branchFacts", () => {
  it("asks the push repository first, then the push host's other remotes", () => {
    const facts = branchFacts(
      context({
        remotes: [
          { name: "origin", host: "github.com", owner: "acme", repo: "app" },
          { name: "mirror", host: "ghe.example.com", owner: "acme", repo: "app" },
          { name: "fork", host: "github.com", owner: "me", repo: "app" },
        ],
      }),
    )!;
    expect(facts.host).toBe("github.com");
    expect(facts.repositories).toEqual(["me/app", "acme/app"]);
    expect(facts.push).toEqual({ owner: "me", repo: "app", branch: "feature" });
    expect(facts.truncated).toBe(false);
  });

  it("uses the remotes' one host without push evidence, and none across several", () => {
    expect(branchFacts(context({ push: null }))?.host).toBe("github.com");
    expect(
      branchFacts(
        context({
          push: null,
          remotes: [
            { name: "origin", host: "github.com", owner: "acme", repo: "app" },
            { name: "spoke", host: "2.spoke.example.com", owner: "eng", repo: "app" },
          ],
        }),
      ),
    ).toBeNull();
  });

  it("marks the facts truncated past the cap or with remotes it couldn't read", () => {
    const remotes = Array.from({ length: MAX_BRANCH_REPOSITORIES + 1 }, (_, index) => ({
      name: `r${index}`,
      host: "github.com",
      owner: "acme",
      repo: `app${index}`,
    }));
    const capped = branchFacts(context({ push: null, remotes }))!;
    expect(capped.repositories).toHaveLength(MAX_BRANCH_REPOSITORIES);
    expect(capped.truncated).toBe(true);
    expect(branchFacts(context({ truncated_remotes: true }))!.truncated).toBe(true);
    expect(branchFacts(context({ unsupported_remote_count: 1 }))!.truncated).toBe(true);
  });

  it("names nothing for a repository with neither a branch nor a commit", () => {
    expect(branchFacts(context({ branch: null, push: null, head_sha: null }))).toBeNull();
  });
});

describe("repositoryContextKey", () => {
  const key = (overrides: Partial<Ok>) =>
    repositoryContextKey("github", branchFacts(context(overrides))!);

  it("stays the same across commits, so a choice survives them", async () => {
    expect(await key({ head_sha: "b".repeat(40) })).toBe(await key({}));
  });

  it("changes with a replaced remote, also when nothing says where the branch pushes", async () => {
    const one = await key({
      push: null,
      remotes: [{ name: "origin", host: "github.com", owner: "acme", repo: "one" }],
    });
    const two = await key({
      push: null,
      remotes: [{ name: "origin", host: "github.com", owner: "acme", repo: "two" }],
    });
    expect(one).not.toBe(two);
    expect(one).toMatch(/^[0-9a-f]{64}$/);
  });

  it("changes with the base repositories while the push target stays", async () => {
    const before = await key({});
    const after = await key({
      remotes: [
        { name: "upstream", host: "github.com", owner: "other", repo: "app" },
        { name: "fork", host: "github.com", owner: "me", repo: "app" },
      ],
    });
    expect(before).not.toBe(after);
  });

  describe("past the repository cap", () => {
    const remotes = (repos: string[]) =>
      repos.map((repo, index) => ({
        name: `r${index}`,
        host: "github.com",
        owner: "acme",
        repo,
      }));
    const nine = Array.from({ length: MAX_BRANCH_REPOSITORIES + 1 }, (_, index) => `app${index}`);
    const keyOf = (repos: string[], head = HEAD) =>
      key({ push: null, remotes: remotes(repos), head_sha: head });

    it("changes when only a repository past the cap is replaced", async () => {
      const replaced = [...nine.slice(0, -1), "other"];
      expect(
        branchFacts(context({ push: null, remotes: remotes(replaced) }))!.repositories,
      ).toEqual(branchFacts(context({ push: null, remotes: remotes(nine) }))!.repositories);
      expect(await keyOf(replaced)).not.toBe(await keyOf(nine));
    });

    it("stays across reordered remotes, a switch and back, and new commits", async () => {
      const original = await keyOf(nine);
      expect(await keyOf([...nine].reverse())).toBe(original);
      expect(await keyOf(["elsewhere"])).not.toBe(original);
      expect(await keyOf(nine)).toBe(original);
      expect(await keyOf(nine, "c".repeat(40))).toBe(original);
    });
  });

  it("changes with the push target", async () => {
    const pushed = await key({});
    const elsewhere = await key({
      push: { ...context().push!, branch: "renamed" },
    });
    expect(pushed).not.toBe(elsewhere);
  });
});

describe("discoverBranchPullRequest", () => {
  it("asks main with the facts as this Mac's account", async () => {
    const shell = installShell({
      status: "found",
      evidence: "push",
      pull_request: candidate(7),
      matches_head: true,
    });
    const discovery = (await discoverBranchPullRequest("conv_1", context(), "github"))!;
    expect(discovery.result).toMatchObject({ status: "found", evidence: "push" });
    expect(shell.codeHostQuery).toHaveBeenCalledWith(
      expect.objectContaining({
        provider: "github",
        host: "github.com",
        account: "mac",
        operation: "branch_pull_requests",
        params: {
          repositories: ["me/app", "acme/app"],
          branch: "feature",
          push: { owner: "me", repo: "app", branch: "feature" },
          head_sha: HEAD,
          tracking_sha: HEAD,
        },
      }),
    );
  });

  it("never claims a single match or none when some repositories weren't searched", async () => {
    installShell({
      status: "found",
      evidence: "push",
      pull_request: candidate(7),
      matches_head: true,
    });
    const found = await discoverBranchPullRequest(
      "conv_1",
      context({ truncated_remotes: true }),
      "github",
    );
    expect(found!.result).toMatchObject({ status: "ambiguous", incomplete: true });
    installShell({ status: "none" });
    const none = await discoverBranchPullRequest(
      "conv_1",
      context({ truncated_remotes: true }),
      "github",
    );
    expect(none!.result).toEqual({ status: "incomplete" });
  });

  it("reads a malformed answer as incomplete", async () => {
    installShell({ status: "found", pull_request: { url: 7 } });
    expect((await discoverBranchPullRequest("conv_1", context(), "github"))!.result).toEqual({
      status: "incomplete",
    });
  });

  it("applies a remembered choice, and asks again once the chosen PR is gone", async () => {
    installShell({
      status: "ambiguous",
      candidates: [candidate(1), candidate(2)],
      incomplete: false,
    });
    const asked = (await discoverBranchPullRequest("conv_1", context(), "github"))!;
    expect(asked.result.status).toBe("ambiguous");
    expect(await chooseBranchPullRequest(asked.choiceKey!, { url: candidate(2).url })).toBe(true);
    const chosen = (await discoverBranchPullRequest("conv_1", context(), "github"))!;
    expect(chosen.result).toMatchObject({ status: "found", evidence: "choice" });
    installShell({
      status: "ambiguous",
      candidates: [candidate(1), candidate(3)],
      incomplete: false,
    });
    const gone = (await discoverBranchPullRequest("conv_1", context(), "github"))!;
    expect(gone.result.status).toBe("ambiguous");
  });

  it("keeps a choice for this run on a shell that can't remember it", async () => {
    const shell = installShell(
      { status: "ambiguous", candidates: [candidate(1), candidate(2)], incomplete: false },
      { remembers: false },
    );
    const asked = (await discoverBranchPullRequest("conv_1", context(), "github"))!;
    expect(await chooseBranchPullRequest(asked.choiceKey!, { none: true })).toBe(true);
    const chosen = (await discoverBranchPullRequest("conv_1", context(), "github"))!;
    expect(chosen.result).toEqual({ status: "none", chosen: true });
    expect(shell.localPullRequestLinks).not.toHaveBeenCalled();
  });
});

describe("a choice on a detached HEAD", () => {
  const OTHER = "d".repeat(40);
  const detached = (head = HEAD) => context({ branch: null, push: null, head_sha: head });
  const ambiguous = {
    status: "ambiguous",
    candidates: [candidate(1, { head_ref: "one" }), candidate(2, { head_ref: "two" })],
    incomplete: false,
  };

  it.each([
    ["the first PR", { url: candidate(1).url }, { status: "found", evidence: "choice" }],
    ["the second PR", { url: candidate(2).url }, { status: "found", evidence: "choice" }],
    ["none of them", { none: true as const }, { status: "none", chosen: true }],
  ])("keeps %s for this commit and this run only", async (_name, choice, expected) => {
    const shell = installShell(ambiguous);
    const asked = (await discoverBranchPullRequest("conv_1", detached(), "github"))!;
    expect(asked.choiceKey).toMatchObject({ branch: null, commit: HEAD });
    expect(asked.remembered).toBe(false);
    expect(await chooseBranchPullRequest(asked.choiceKey!, choice)).toBe(true);
    const chosen = (await discoverBranchPullRequest("conv_1", detached(), "github"))!;
    expect(chosen.result).toMatchObject(expected);
    if ("url" in choice) {
      expect(chosen.result).toMatchObject({ pull_request: { url: choice.url } });
    }
    // Never stored, and another commit asks again.
    expect(shell.localPullRequestLinks).not.toHaveBeenCalled();
    const elsewhere = (await discoverBranchPullRequest("conv_1", detached(OTHER), "github"))!;
    expect(elsewhere.result.status).toBe("ambiguous");
  });

  it("never reuses a branch's choice", async () => {
    installShell(ambiguous);
    const onBranch = (await discoverBranchPullRequest("conv_1", context(), "github"))!;
    await chooseBranchPullRequest(onBranch.choiceKey!, { url: candidate(1).url });
    const detachedHere = (await discoverBranchPullRequest("conv_1", detached(), "github"))!;
    expect(detachedHere.result.status).toBe("ambiguous");
  });
});

describe("whether a choice is remembered", () => {
  it.each([
    ["remembers it on a shell that keeps choices", true],
    ["says it won't on a shell that can't", false],
  ])("%s", async (_name, remembers) => {
    installShell(
      { status: "ambiguous", candidates: [candidate(1), candidate(2)], incomplete: false },
      { remembers },
    );
    const asked = (await discoverBranchPullRequest("conv_1", context(), "github"))!;
    expect(asked.remembered).toBe(remembers);
  });
});

describe("withBranchPullRequest", () => {
  const discovery = (result: BranchDiscovery["result"]): BranchDiscovery => ({
    contextKey: "ctx-1",
    provider: "github",
    host: "github.com",
    result,
    choiceKey: null,
    remembered: false,
  });
  const info = (overrides: Partial<PullRequestInfo> = {}): PullRequestInfo => ({
    object: "session.github.info",
    available: true,
    prs: [
      {
        url: "https://github.com/acme/app/pull/99",
        host: "github.com",
        repository: "acme/app",
        number: 99,
        relationship: "inferred",
      },
      {
        url: "https://github.com/acme/app/pull/5",
        host: "github.com",
        repository: "acme/app",
        number: 5,
        relationship: "attached",
      },
    ],
    selected_pr_url: "https://github.com/acme/app/pull/99",
    ...overrides,
  });

  it("replaces the PR the session's machine inferred and selects the one found", () => {
    const merged = withBranchPullRequest(
      info(),
      discovery({
        status: "found",
        evidence: "push",
        pull_request: candidate(7) as never,
        matches_head: true,
      }),
    );
    expect(merged.prs?.map((pr) => pr.url)).toEqual([
      "https://github.com/acme/app/pull/7",
      "https://github.com/acme/app/pull/5",
    ]);
    expect(merged.selected_pr_url).toBe("https://github.com/acme/app/pull/7");
    expect(merged.prs?.[0]).toMatchObject({ relationship: "inferred", provider: "github" });
  });

  it("keeps a selected linked PR, and drops the inferred one when nothing was found", () => {
    const selectedLinked = withBranchPullRequest(
      info({ selected_pr_url: "https://github.com/acme/app/pull/5" }),
      discovery({
        status: "found",
        evidence: "push",
        pull_request: candidate(7) as never,
        matches_head: true,
      }),
    );
    expect(selectedLinked.selected_pr_url).toBe("https://github.com/acme/app/pull/5");
    const none = withBranchPullRequest(info(), discovery({ status: "none", chosen: false }));
    expect(none.prs?.map((pr) => pr.number)).toEqual([5]);
    expect(none.selected_pr_url).toBe("https://github.com/acme/app/pull/5");
  });
});

describe("branch choice commands and context repositories", () => {
  it("offers each candidate and none, or choosing again after none", () => {
    const ambiguous = branchChoiceCommands({
      contextKey: "",
      provider: "github",
      host: "github.com",
      result: { status: "ambiguous", candidates: [candidate(1) as never], incomplete: false },
      choiceKey: null,
      remembered: false,
    });
    expect(ambiguous.map((command) => command.paletteLabel)).toEqual([
      "Choose #1 as this branch’s pull request",
      "This branch has none of these pull requests",
    ]);
    const again = branchChoiceCommands({
      contextKey: "",
      provider: "github",
      host: "github.com",
      result: { status: "none", chosen: true },
      choiceKey: null,
      remembered: false,
    });
    expect(again.map((command) => command.choice)).toEqual([null]);
  });

  it("lists the context's repositories on known hosts only", () => {
    const known = (host: string) => (host === "github.com" ? "github" : null);
    expect(
      contextRepositories(
        context({
          remotes: [
            { name: "origin", host: "github.com", owner: "acme", repo: "app" },
            { name: "lab", host: "gitlab.com", owner: "acme", repo: "app" },
          ],
        }),
        known,
      ),
    ).toEqual([
      { provider: "github", host: "github.com", repository: "me/app" },
      { provider: "github", host: "github.com", repository: "acme/app" },
    ]);
    expect(contextRepositories({ status: "unsupported" }, known)).toEqual([]);
  });

  it("keeps a repository whose host is still being checked, with no provider yet", () => {
    expect(
      contextRepositories(context({ push: null }), (host) =>
        host === "github.com" ? "pending" : null,
      ),
    ).toEqual([
      { provider: null, host: "github.com", repository: "acme/app" },
      { provider: null, host: "github.com", repository: "me/app" },
    ]);
  });
});

describe("withBranchOnStoredList", () => {
  const stored = [
    {
      url: "https://github.com/acme/app/pull/99",
      host: "github.com",
      repository: "acme/app",
      number: 99,
      relationship: "inferred" as const,
    },
    {
      url: "https://github.com/acme/app/pull/5",
      host: "github.com",
      repository: "acme/app",
      number: 5,
      relationship: "attached" as const,
    },
  ];
  const mac = {
    url: "https://github.com/acme/app/pull/7",
    host: "github.com",
    repository: "acme/app",
    number: 7,
    relationship: "inferred" as const,
    found_on_mac: true,
  };

  it("puts the Mac's branch PR first, in place of the one the session's machine inferred", () => {
    expect(
      withBranchOnStoredList(stored, { branch_on_mac: true, prs: [mac] }).map((pr) => pr.number),
    ).toEqual([7, 5]);
  });

  it("drops the inferred one even when the Mac found none", () => {
    expect(
      withBranchOnStoredList(stored, { branch_on_mac: true, prs: [] }).map((pr) => pr.number),
    ).toEqual([5]);
  });

  it("leaves the stored list alone while the session's machine finds the branch's PR", () => {
    expect(withBranchOnStoredList(stored, { prs: [mac] })).toBe(stored);
  });
});
