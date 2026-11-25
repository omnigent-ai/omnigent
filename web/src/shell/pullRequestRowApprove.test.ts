import type * as IdentityModule from "@/lib/identity";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
  subscribeIdentity: () => () => {},
}));

import {
  GITHUB_WRITE_SET_V1,
  REVIEW_WRITE_OPERATIONS,
  resetDesktopPullRequestWritesForTests,
  setMacWritesReleaseFlagForTests,
  setPageWriteOperationsForTests,
  setReviewsFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import {
  acceptMacAnswer,
  desktopReadTarget,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  macReadTicket,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";

import { resetMacNamedReadsForTests } from "@/lib/macNamedReads";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";
import {
  BOB,
  HOST_LOGIN,
  installNamedWriteShell,
  removeNamedWriteShell,
} from "@/test/macNamedWriteShell";

import { rowOffersApprove } from "./pullRequestRowApprove";

const PR = "https://github.com/acme/app/pull/42";
const VIEWER = "mac-octo";

/** A shell that reads GitHub as `VIEWER` on this Mac and lists `operations` as writes. */
async function macReadsAndWrites(operations: readonly string[]) {
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: vi.fn(async () => ({ ok: true, data: [] })),
    codeHostWrite: vi.fn(),
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
          operations: [...GITHUB_MAC_READ_OPERATIONS, "pull_request_review_check"],
          accounts: true,
          writes: { version: 1, operations },
        },
      ],
    }),
  };
  githubMacReadsPreference.write("on");
  await desktopReadTarget(PR);
  acceptMacAnswer(macReadTicket("github", "github.com"), {
    identity: { host: "github.com", login: VIEWER, generation: 1 },
  });
  setMacWritesReleaseFlagForTests(true);
  setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
  setReviewsFlagForTests(true);
}

const REVIEWS = [...GITHUB_WRITE_SET_V1, ...REVIEW_WRITE_OPERATIONS, "pull_request_review_check"];
type RowPullRequest = Parameters<typeof rowOffersApprove>[1];
type Reviewer = NonNullable<RowPullRequest["reviewers"]>[number];
const reviewer = (login: string, kind: Reviewer["kind"]) =>
  ({ login, kind, state: "PENDING" }) as Reviewer;
const asking = (fields: Partial<RowPullRequest> = {}): RowPullRequest => ({
  state: "OPEN",
  author: "author-octo",
  reviewers: [reviewer("Mac-Octo", "user")],
  ...fields,
});

beforeEach(() => resetDesktopPullRequestWritesForTests());
afterEach(() => {
  setMacWritesReleaseFlagForTests(null);
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  removeNamedWriteShell();
});

describe("rowOffersApprove", () => {
  it("offers Approve on an open pull request asking this Mac's account for a review", async () => {
    await macReadsAndWrites(REVIEWS);
    expect(rowOffersApprove(PR, asking(), null)).toBe(true);
  });

  it("never offers it on the account's own pull request", async () => {
    await macReadsAndWrites(REVIEWS);
    expect(rowOffersApprove(PR, asking({ author: "MAC-OCTO" }), null)).toBe(false);
  });

  it("offers it only where this Mac submits reviews", async () => {
    await macReadsAndWrites(GITHUB_WRITE_SET_V1);
    expect(rowOffersApprove(PR, asking(), null)).toBe(false);
    resetDesktopReadPathForTests();
    delete (window as unknown as Record<string, unknown>).omnigentDesktop;
    expect(rowOffersApprove(PR, asking(), null)).toBe(false);
  });

  it("leaves out closed pull requests and ones asking only a team or someone else", async () => {
    await macReadsAndWrites(REVIEWS);
    expect(rowOffersApprove(PR, asking({ state: "MERGED" }), null)).toBe(false);
    const team = asking({ reviewers: [reviewer("mac-octo", "team")] });
    expect(rowOffersApprove(PR, team, null)).toBe(false);
    const other = asking({ reviewers: [reviewer("someone", "user")] });
    expect(rowOffersApprove(PR, other, null)).toBe(false);
  });

  it("hides it on a shell that can't write by name, wherever the list reads this host by name", async () => {
    await macReadsAndWrites(REVIEWS);
    // A list read as the page's own login never offers a one-click approve as the Mac's,
    // even where that login is gh's active account: changes by name are off on this shell.
    expect(rowOffersApprove(PR, asking(), { host: "github.com", login: "olafurpg" })).toBe(false);
    expect(rowOffersApprove(PR, asking(), { host: "github.com", login: "MAC-octo" })).toBe(false);
    // Automatic still approves as gh's active account here.
    expect(rowOffersApprove(PR, asking(), null)).toBe(true);
  });

  it("offers it as the list's own login on a shell that writes by name", async () => {
    const shell = installNamedWriteShell();
    await shell.ready(PR);
    const askingBob = asking({ reviewers: [reviewer("bob", "user")] });
    expect(rowOffersApprove(PR, askingBob, BOB)).toBe(true);
    // Only for a review asked of that login, not of gh's active account.
    expect(rowOffersApprove(PR, asking({ reviewers: [reviewer(HOST_LOGIN, "user")] }), BOB)).toBe(
      false,
    );
    // Nor on the login's own pull request.
    expect(rowOffersApprove(PR, { ...askingBob, author: "BOB" }, BOB)).toBe(false);
    // Picked by name as gh's active account too, once that login's read by name is proven.
    await shell.readNamed(PR, { host: "github.com", login: HOST_LOGIN });
    const askingAlice = asking({ reviewers: [reviewer(HOST_LOGIN, "user")] });
    expect(rowOffersApprove(PR, askingAlice, { host: "github.com", login: "ALICE" })).toBe(true);
  });

  it("keeps it where the list's own login is for another host", async () => {
    await macReadsAndWrites(REVIEWS);
    expect(rowOffersApprove(PR, asking(), { host: "ghe.example.com", login: "carol" })).toBe(true);
  });
});
