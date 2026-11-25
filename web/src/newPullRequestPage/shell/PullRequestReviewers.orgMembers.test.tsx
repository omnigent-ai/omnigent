// The reviewer picker with a desktop shell that reads GitHub on this Mac and keeps its
// organization's people: anyone in the org can be found by login or name as the user
// types, the first fill explains itself in one calm line, and a failed read never shows
// transport text or hides the list. The people this account worked with recently come first.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  desktopReadTarget,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
} from "@/lib/desktopReadPath";
import type { OrgMembersAnswer } from "@/hooks/useOrgMembers";
import type { RecentReviewersAnswer } from "@/hooks/useRecentReviewers";
import { PullRequestReviewers } from "./PullRequestReviewers";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const PR_URL = "https://github.com/acme/app/pull/7";
const REVIEWERS_PATH = "/v1/sessions/conv_1/resources/github/reviewers";
const IDENTITY = { host: "github.com", login: "mac-octo", generation: 1 };

const assignable = {
  object: "list",
  data: [{ login: "alice", kind: "user", name: "Alice Liddell" }],
  has_more: false,
};

function members(
  state: OrgMembersAnswer["state"],
  extra: Partial<OrgMembersAnswer> = {},
): OrgMembersAnswer {
  return {
    object: "list",
    organization: "acme",
    data: [
      { login: "cyrus-o", name: "Cyrus Omidvar" },
      { login: "zed", name: null },
    ],
    state,
    complete: state === "ready" || state === "refreshing",
    truncated: false,
    fetched_at: null,
    next_attempt_at: null,
    failure: null,
    ...extra,
  };
}

type Identity = typeof IDENTITY;
type Answer =
  | { ok: true; data: unknown; identity?: Identity }
  | { ok: false; error: { code: string; message: string } };

/** A shell reading GitHub as `mac-octo`; `operations` is what it offers beyond the panel's. */
async function installShell(
  answers: Record<string, () => Answer | Promise<Answer>>,
  operations = ["pull_request_review_candidates", "organization_members"],
) {
  const query = vi.fn(async (request: { operation: string; params: Record<string, unknown> }) => {
    const answer = await answers[request.operation]?.();
    if (!answer) return { ok: true, data: [], identity: IDENTITY };
    return answer.ok ? { identity: IDENTITY, ...answer } : answer;
  });
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: query,
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
          operations: [...GITHUB_MAC_READ_OPERATIONS, ...operations],
          accounts: true,
        },
      ],
    }),
  };
  githubMacReadsPreference.write("on");
  await desktopReadTarget(PR_URL);
  return query;
}

/** The session's host: reviewer requests post here while writes stay off this Mac. */
function stubHost(candidates: () => Response = () => new Response(JSON.stringify(assignable))) {
  const posts: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname !== REVIEWERS_PATH) return new Response("{}", { status: 404 });
      if (init?.method === "POST") {
        posts.push(JSON.parse(String(init.body)));
        return new Response(JSON.stringify({ object: "list", data: [] }));
      }
      return candidates();
    }),
  );
  return posts;
}

function picker(client: QueryClient, prUrl: string) {
  return (
    <QueryClientProvider client={client}>
      <PullRequestReviewers
        conversationId="conv_1"
        prUrl={prUrl}
        reviewers={[]}
        author="dev"
        editable
      />
    </QueryClientProvider>
  );
}

function renderPicker() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { rerender } = render(picker(client, PR_URL));
  fireEvent.click(screen.getByRole("button", { name: "Request reviewers" }));
  return Object.assign(client, {
    /** Show another PR in the same mounted panel, as selecting one does. */
    showPullRequest: (prUrl: string) => rerender(picker(client, prUrl)),
  });
}

const type = (text: string) =>
  fireEvent.change(screen.getByPlaceholderText("Filter or type a login…"), {
    target: { value: text },
  });

afterEach(() => {
  cleanup();
  githubMacReadsPreference.write("automatic");
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  vi.unstubAllGlobals();
});

describe("people from the organization", () => {
  it("finds and requests someone listed only in the organization", async () => {
    const query = await installShell({
      pull_request_review_candidates: () => ({ ok: true, data: assignable }),
      organization_members: () => ({ ok: true, data: members("ready") }),
    });
    const posts = stubHost();
    renderPicker();

    type("cyr");
    const option = await screen.findByRole("option", { name: /^cyrus-o/ });
    expect(screen.queryByRole("option", { name: /^alice/ })).toBeNull();
    // The match leads, so Enter requests it rather than the partial login typed.
    expect(screen.getAllByRole("option").map((item) => item.textContent)).toEqual([
      "cyrus-oCyrus Omidvar",
      "cyrNot in listGitHub checks it when you close",
    ]);
    fireEvent.click(option);
    // Closing the picker sends what it checked.
    fireEvent.keyDown(screen.getByRole("combobox", { name: "Filter reviewers" }), {
      key: "Escape",
    });
    await waitFor(() =>
      expect(posts).toEqual([
        { pr_url: PR_URL, add: [{ login: "cyrus-o", kind: "user" }], remove: [] },
      ]),
    );
    // Candidates are read on this Mac too, though reviewer changes still go to the host.
    expect(query.mock.calls.map(([request]) => request.operation)).toEqual(
      expect.arrayContaining(["pull_request_review_candidates", "organization_members"]),
    );
    expect(query).toHaveBeenCalledWith(
      expect.objectContaining({
        operation: "organization_members",
        params: { organization: "acme" },
      }),
    );
  });

  it("matches a name, not just a login", async () => {
    await installShell({
      pull_request_review_candidates: () => ({ ok: true, data: assignable }),
      organization_members: () => ({ ok: true, data: members("ready") }),
    });
    stubHost();
    renderPicker();
    type("omid");
    expect(await screen.findByRole("option", { name: /^cyrus-o/ })).toBeInTheDocument();
  });

  it("explains a first fill in one line, and typing never waits for it", async () => {
    await installShell({
      pull_request_review_candidates: () => ({ ok: true, data: assignable }),
      organization_members: () => ({ ok: true, data: members("filling", { data: [] }) }),
    });
    stubHost();
    renderPicker();
    expect(
      await screen.findByText(
        "Loading people in acme for the first time. This happens about once a week.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Loading reviewers…")).toBeNull();
    type("someone");
    expect(screen.getByRole("option", { name: /^someone\s*Not in list/ })).toBeInTheDocument();
  });

  it("says nothing while a loaded list refreshes, and says when it's truncated", async () => {
    let answer = members("refreshing");
    await installShell({
      pull_request_review_candidates: () => ({ ok: true, data: assignable }),
      organization_members: () => ({ ok: true, data: answer }),
    });
    stubHost();
    const client = renderPicker();
    await screen.findByRole("option", { name: /^cyrus-o/ });
    expect(screen.queryByText(/acme/)).toBeNull();

    answer = members("ready", { truncated: true });
    await client.refetchQueries({ queryKey: ["code-host-mac", "organization_members"] });
    expect(
      await screen.findByText(
        "Showing the first 50,000 people in acme. Type a login to request anyone else.",
      ),
    ).toBeInTheDocument();
  });

  it.each([
    [
      "missing_scope",
      "Listing people in acme needs GitHub's read:org permission. Run gh auth refresh -s read:org, or type a login.",
    ],
    [
      "sso_required",
      "GitHub didn't let @mac-octo list people in acme. Authorize the GitHub CLI for acme's single sign-on, or type a login.",
    ],
    [
      "rate_limited",
      "GitHub is limiting requests. The people list will finish loading later; you can still type a login.",
    ],
  ])("explains %s without hiding the repository's people", async (code, line) => {
    await installShell({
      pull_request_review_candidates: () => ({ ok: true, data: assignable }),
      organization_members: () => ({
        ok: true,
        data: members(code === "rate_limited" ? "paused" : "unavailable", {
          data: [],
          failure: { code },
        }),
      }),
    });
    stubHost();
    renderPicker();
    expect(await screen.findByText(line)).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /^alice/ })).toBeInTheDocument();
  });

  it("asks the shell again after the user fixes the scope", async () => {
    let fixed = false;
    const query = await installShell({
      pull_request_review_candidates: () => ({ ok: true, data: assignable }),
      organization_members: () => ({
        ok: true,
        data: fixed
          ? members("ready")
          : members("unavailable", { data: [], failure: { code: "missing_scope" } }),
      }),
    });
    stubHost();
    renderPicker();
    const tryAgain = await screen.findByRole("button", { name: "Try again" });
    fixed = true;
    fireEvent.click(tryAgain);
    type("cyr");
    expect(await screen.findByRole("option", { name: /^cyrus-o/ })).toBeInTheDocument();
    expect(query).toHaveBeenCalledWith(
      expect.objectContaining({
        operation: "organization_members",
        params: { organization: "acme", retry: true },
      }),
    );
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it.each([
    [
      "an unreadable answer",
      () => ({
        ok: false as const,
        error: { code: "query_failed", message: "runner resource endpoint failed" },
      }),
      "Couldn't try again just now. Try again in a moment, or type a login.",
    ],
    [
      "a sign-out meanwhile",
      () => ({
        ok: false as const,
        error: {
          code: "signed_out",
          message: "No GitHub account for github.com is signed in on this Mac.",
        },
      }),
      "No GitHub account for github.com is signed in on this Mac. You can still type a login.",
    ],
    [
      "a bridge that throws",
      () => {
        throw new Error("IPC channel closed");
      },
      "Couldn't try again just now. Try again in a moment, or type a login.",
    ],
  ])("explains a retry refused by %s and keeps the picker working", async (_name, refuse, line) => {
    let retried = false;
    await installShell({
      pull_request_review_candidates: () => ({ ok: true, data: assignable }),
      organization_members: () =>
        retried
          ? refuse()
          : {
              ok: true,
              data: members("unavailable", { data: [], failure: { code: "missing_scope" } }),
            },
    });
    stubHost();
    renderPicker();
    const tryAgain = await screen.findByRole("button", { name: "Try again" });
    retried = true;
    fireEvent.click(tryAgain);
    expect(await screen.findByText(line)).toBeInTheDocument();
    expect(screen.queryByText(/runner resource endpoint failed|IPC channel closed/)).toBeNull();
    // The repository's people, the typed login and another try all still work.
    expect(screen.getByRole("option", { name: /^alice/ })).toBeInTheDocument();
    type("cyrus");
    expect(screen.getByRole("option", { name: /^cyrus\s*Not in list/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
  });

  it("drops a failed retry's line once an ordinary reread succeeds", async () => {
    let phase: "scope" | "refused" | "fixed" = "scope";
    await installShell({
      pull_request_review_candidates: () => ({ ok: true, data: assignable }),
      organization_members: () =>
        phase === "refused"
          ? {
              ok: false,
              error: { code: "query_failed", message: "runner resource endpoint failed" },
            }
          : phase === "fixed"
            ? { ok: true, data: members("ready") }
            : {
                ok: true,
                data: members("unavailable", { data: [], failure: { code: "missing_scope" } }),
              },
    });
    stubHost();
    const client = renderPicker();
    const tryAgain = await screen.findByRole("button", { name: "Try again" });
    phase = "refused";
    fireEvent.click(tryAgain);
    const refused = "Couldn't try again just now. Try again in a moment, or type a login.";
    expect(await screen.findByText(refused)).toBeInTheDocument();

    phase = "fixed";
    await client.refetchQueries({ queryKey: ["code-host-mac", "organization_members"] });
    await waitFor(() => expect(screen.queryByText(refused)).toBeNull());
    type("cyr");
    expect(await screen.findByRole("option", { name: /^cyrus-o/ })).toBeInTheDocument();
  });

  it("never shows one organization's failed retry on another PR's picker", async () => {
    const OTHER_PR = "https://github.com/globex/site/pull/3";
    let refuse = false;
    await installShell({
      pull_request_review_candidates: () => ({ ok: true, data: assignable }),
      organization_members: () =>
        refuse
          ? {
              ok: false,
              error: {
                code: "signed_out",
                message: "No GitHub account for github.com is signed in on this Mac.",
              },
            }
          : {
              ok: true,
              data: members("unavailable", { data: [], failure: { code: "missing_scope" } }),
            },
    });
    await desktopReadTarget(OTHER_PR);
    stubHost();
    const client = renderPicker();
    const tryAgain = await screen.findByRole("button", { name: "Try again" });
    refuse = true;
    fireEvent.click(tryAgain);
    expect(await screen.findByText(/No GitHub account for github.com/)).toBeInTheDocument();

    refuse = false;
    client.showPullRequest(OTHER_PR);
    // The other PR's own state shows, not the retry made for the first one.
    await waitFor(() => expect(screen.queryByText(/No GitHub account for github.com/)).toBeNull());
    expect(await screen.findByText(/needs GitHub's read:org permission/)).toBeInTheDocument();
  });

  it("offers no retry during a rate-limit pause", async () => {
    await installShell({
      pull_request_review_candidates: () => ({ ok: true, data: assignable }),
      organization_members: () => ({
        ok: true,
        data: members("paused", { data: [], failure: { code: "rate_limited" } }),
      }),
    });
    stubHost();
    renderPicker();
    await screen.findByText(/GitHub is limiting requests/);
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("keeps the organization's people when the repository's candidates fail", async () => {
    await installShell({
      pull_request_review_candidates: () => ({
        ok: false,
        error: { code: "query_failed", message: "GitHub couldn't answer. Try again in a moment." },
      }),
      organization_members: () => ({ ok: true, data: members("ready") }),
    });
    stubHost();
    renderPicker();
    expect(
      await screen.findByText(
        "Couldn't load who can review in acme/app. Type a login to request anyone.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /^cyrus-o/ })).toBeInTheDocument();
  });

  it("works as before with a shell that keeps no organization list", async () => {
    const query = await installShell(
      { pull_request_review_candidates: () => ({ ok: true, data: assignable }) },
      ["pull_request_review_candidates"],
    );
    stubHost();
    renderPicker();
    expect(await screen.findByRole("option", { name: /^alice/ })).toBeInTheDocument();
    expect(query.mock.calls.some(([request]) => request.operation === "organization_members")).toBe(
      false,
    );
    expect(screen.queryByText(/for the first time/)).toBeNull();
  });
});

/** The shell's recent-reviewers answer for acme: `logins`, most worked with first. */
function recent(
  logins: string[],
  state: RecentReviewersAnswer["state"] = "ready",
): RecentReviewersAnswer {
  return {
    object: "list",
    organization: "acme",
    data: logins.map((login) => ({ login })),
    state,
    complete: state === "ready",
    fetched_at: null,
    next_attempt_at: null,
    failure: null,
  };
}

const ORG_ONLY = members("ready", {
  data: [
    { login: "cyrus-o", name: "Cyrus Omidvar" },
    { login: "zara", name: null },
    { login: "zoey", name: "Zoey Deschanel" },
  ],
});
const RECENT_OPERATIONS = [
  "pull_request_review_candidates",
  "organization_members",
  "recent_reviewers",
];
const users = () => screen.getAllByRole("option").map((item) => item.textContent);

describe("people worked with recently", () => {
  it("come first, win a tie against an organization-only match, and never offer the author", async () => {
    const query = await installShell(
      {
        pull_request_review_candidates: () => ({ ok: true, data: assignable }),
        organization_members: () => ({ ok: true, data: ORG_ONLY }),
        recent_reviewers: () => ({ ok: true, data: recent(["zoey", "dev", "bea"]) }),
      },
      RECENT_OPERATIONS,
    );
    stubHost();
    renderPicker();
    // Recent people first, with names from the other lists; the author, dev, isn't offered.
    await waitFor(() =>
      expect(users()).toEqual([
        "zoeyZoey Deschanel",
        "bea",
        "aliceAlice Liddell",
        "cyrus-oCyrus Omidvar",
        "zara",
      ]),
    );
    type("z");
    // zara and zoey match "z" equally, and zara sorts first, but zoey worked with this account.
    expect(users()).toEqual([
      "zoeyZoey Deschanel",
      "zara",
      "zNot in listGitHub checks it when you close",
    ]);
    expect(query).toHaveBeenCalledWith(
      expect.objectContaining({ operation: "recent_reviewers", params: { organization: "acme" } }),
    );
  });

  it.each([
    ["a shell without the read", ["pull_request_review_candidates", "organization_members"]],
    ["a shell whose switch is off", RECENT_OPERATIONS],
  ])("rank as before with %s", async (_name, operations) => {
    const query = await installShell(
      {
        pull_request_review_candidates: () => ({ ok: true, data: assignable }),
        organization_members: () => ({ ok: true, data: ORG_ONLY }),
        recent_reviewers: () => ({ ok: true, data: recent([], "off") }),
      },
      operations,
    );
    stubHost();
    renderPicker();
    await screen.findByRole("option", { name: /^zara/ });
    type("z");
    expect(users()).toEqual([
      "zara",
      "zoeyZoey Deschanel",
      "zNot in listGitHub checks it when you close",
    ]);
    const asked = query.mock.calls.some(([request]) => request.operation === "recent_reviewers");
    expect(asked).toBe(operations.includes("recent_reviewers"));
  });

  it("rank as before with a bridge that can't read code hosts", async () => {
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      getDesktopFeatures: async () => ({
        codeHosts: [{ id: "github", operations: RECENT_OPERATIONS, accounts: true }],
      }),
    };
    stubHost();
    renderPicker();
    expect(await screen.findByRole("option", { name: /^alice/ })).toBeInTheDocument();
    type("al");
    expect(users()).toEqual(["aliceAlice Liddell", "alNot in listGitHub checks it when you close"]);
  });

  it("never show the previous account's people once the account changes", async () => {
    let releaseOld: (answer: Answer) => void = () => {};
    let generation = 1;
    const other = { host: "github.com", login: "mac-other", generation: 2 };
    await installShell(
      {
        pull_request_review_candidates: () => ({ ok: true, data: assignable }),
        organization_members: () => ({
          ok: true,
          data: ORG_ONLY,
          identity: generation === 1 ? IDENTITY : other,
        }),
        recent_reviewers: () =>
          generation === 1
            ? new Promise<Answer>((resolve) => {
                releaseOld = resolve;
              })
            : { ok: true, data: recent(["zoey"]), identity: other },
      },
      RECENT_OPERATIONS,
    );
    stubHost();
    const client = renderPicker();
    await screen.findByRole("option", { name: /^zoey/ });
    // Another account is chosen; the next answer says so, while the old read is still out.
    generation = 2;
    await client.refetchQueries({ queryKey: ["code-host-mac", "organization_members"] });
    releaseOld({ ok: true, data: recent(["zara"]), identity: IDENTITY });
    await waitFor(() => expect(users()[0]).toBe("zoeyZoey Deschanel"));
    type("z");
    expect(users()).toEqual([
      "zoeyZoey Deschanel",
      "zara",
      "zNot in listGitHub checks it when you close",
    ]);
  });

  it("drop the previous account's people at once when the account changes", async () => {
    let generation = 1;
    let releaseNew: (answer: Answer) => void = () => {};
    const other = { host: "github.com", login: "mac-other", generation: 2 };
    await installShell(
      {
        pull_request_review_candidates: () => ({ ok: true, data: assignable }),
        organization_members: () => ({
          ok: true,
          data: ORG_ONLY,
          identity: generation === 1 ? IDENTITY : other,
        }),
        recent_reviewers: () =>
          generation === 1
            ? { ok: true, data: recent(["zara"]) }
            : new Promise<Answer>((resolve) => {
                releaseNew = resolve;
              }),
      },
      RECENT_OPERATIONS,
    );
    stubHost();
    const client = renderPicker();
    type("z");
    await waitFor(() =>
      expect(users()).toEqual([
        "zara",
        "zoeyZoey Deschanel",
        "zNot in listGitHub checks it when you close",
      ]),
    );
    expect(screen.queryByText("Zoey Deschanel")).not.toBeNull();
    // The old account's recent people go while the new account's read is still out.
    generation = 2;
    await client.refetchQueries({ queryKey: ["code-host-mac", "organization_members"] });
    await waitFor(() =>
      expect(
        client
          .getQueriesData({ queryKey: ["code-host-mac", "recent_reviewers"] })
          .map(([, data]) => data),
      ).toEqual([undefined]),
    );
    releaseNew({ ok: true, data: recent(["zoey"]), identity: other });
    await waitFor(() => expect(users()[0]).toBe("zoeyZoey Deschanel"));
  });

  it("keep the highlighted person when recent people arrive and reorder the list", async () => {
    let answer = recent([], "filling");
    await installShell(
      {
        pull_request_review_candidates: () => ({ ok: true, data: assignable }),
        organization_members: () => ({ ok: true, data: ORG_ONLY }),
        recent_reviewers: () => ({ ok: true, data: answer }),
      },
      RECENT_OPERATIONS,
    );
    stubHost();
    const client = renderPicker();
    await screen.findByRole("option", { name: /^zara/ });
    const input = screen.getByPlaceholderText("Filter or type a login…");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    const highlighted = () =>
      screen.getAllByRole("option").find((item) => item.getAttribute("aria-selected") === "true");
    expect(highlighted()?.textContent).toBe("cyrus-oCyrus Omidvar");

    answer = recent(["zoey"]);
    await client.refetchQueries({ queryKey: ["code-host-mac", "recent_reviewers"] });
    await waitFor(() => expect(users()[0]).toBe("zoeyZoey Deschanel"));
    expect(highlighted()?.textContent).toBe("cyrus-oCyrus Omidvar");
  });
});

describe("the session's host", () => {
  it("never shows its transport error, and typing a login still works", async () => {
    stubHost(
      () =>
        new Response(JSON.stringify({ detail: "runner resource endpoint failed" }), {
          status: 502,
        }),
    );
    renderPicker();
    expect(
      await screen.findByText(
        "Couldn't load who can review in acme/app. Type a login to request anyone.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/runner resource endpoint failed/)).toBeNull();
    type("cyrus");
    expect(screen.getByRole("option", { name: /^cyrus\s*Not in list/ })).toBeInTheDocument();
  });
});
