import { describe, expect, it, vi } from "vitest";

import * as match from "@/components/ui/quick-pick/match";
import { organizationListRereadMs } from "@/hooks/useMacOrganizationList";
import type { OrgMembersAnswer } from "@/hooks/useOrgMembers";
import {
  mergePeople,
  orgMembersNote,
  orgMembersRetryable,
  PEOPLE_LIMIT,
  preparePeople,
  rankPeople,
  reviewCandidatesFailureNote,
  type OrgMembersStatus,
  type Person,
} from "./peopleSearch";

const user = (login: string, name: string | null = null): Person => ({ login, kind: "user", name });
const logins = (query: string, people: ReturnType<typeof preparePeople>, limit?: number) =>
  rankPeople(query, people, limit).map(({ person }) => person.login);

describe("mergePeople", () => {
  it("lists each person once, in the first source that has them, keeping any name", () => {
    const merged = mergePeople({
      reviewer: [user("Alice")],
      assignable: [user("alice", "Alice Liddell"), { login: "core", kind: "team", name: "Core" }],
      member: [user("bob"), user("ALICE")],
    });
    expect(merged).toEqual([
      { person: user("Alice", "Alice Liddell"), source: "reviewer" },
      { person: { login: "core", kind: "team", name: "Core" }, source: "assignable" },
      { person: user("bob"), source: "member" },
    ]);
  });

  it("keeps a team and a user with the same name apart", () => {
    const merged = mergePeople({
      assignable: [user("core"), { login: "core", kind: "team", name: null }],
    });
    expect(merged).toHaveLength(2);
  });
});

describe("rankPeople", () => {
  const people = preparePeople(
    mergePeople({
      assignable: [user("cyan"), user("dcyrus")],
      member: [
        user("cyrus-o", "Cyrus Omidvar"),
        user("ocyrus"),
        user("cxyxrxus"),
        user("bob", "Cyrus Bobson"),
        user("zoe", "Zoë Ångström"),
      ],
    }),
  );

  it("ranks a login prefix, then a name word, then substrings, then subsequences", () => {
    expect(logins("cyrus", people)).toEqual(["cyrus-o", "bob", "dcyrus", "ocyrus", "cxyxrxus"]);
  });

  it("puts someone this user requested here ahead of better matches, but not an exact login", () => {
    const sources = preparePeople(
      mergePeople({
        requested: [user("cyrus-n_data", "Cyrus N"), user("rcyrus")],
        assignable: [user("cyrus"), user("cyrus-a", "Cyrus A")],
        member: [user("cyrus-b", "Cyrus B")],
      }),
    );
    // A word-start match of a requested person beats every other non-exact match; a login
    // matched only in its middle gets no boost.
    expect(logins("cyrus", sources)).toEqual([
      "cyrus",
      "cyrus-n_data",
      "cyrus-a",
      "cyrus-b",
      "rcyrus",
    ]);
    expect(logins("cyr", sources)).toEqual([
      "cyrus-n_data",
      "cyrus",
      "cyrus-a",
      "cyrus-b",
      "rcyrus",
    ]);
    // With nothing typed they follow the PR's own reviewers, most recent first.
    expect(logins("", sources).slice(0, 2)).toEqual(["cyrus-n_data", "rcyrus"]);
  });

  it("puts the people `leads` names ahead of requested and better matches, but not an exact login", () => {
    const sources = preparePeople(
      mergePeople({
        reviewer: [user("cyrus-p_data"), user("xcyrus"), user("cyrus-me")],
        requested: [user("cyrus-n_data", "Cyrus N")],
        assignable: [user("cyrus"), user("cyrus-a", "Cyrus A")],
      }),
    );
    const leads = ({ source, person }: (typeof sources)[number]) =>
      source === "reviewer" && person.login !== "cyrus-me";
    const ranked = (query: string) =>
      rankPeople(query, sources, undefined, { leads }).map(({ person }) => person.login);
    // A login matched only in its middle gets no lead, nor does someone `leads` leaves out.
    expect(ranked("cyr")).toEqual([
      "cyrus-p_data",
      "cyrus-n_data",
      "cyrus",
      "cyrus-a",
      "cyrus-me",
      "xcyrus",
    ]);
    expect(ranked("cyrus")[0]).toBe("cyrus");
    // Without `leads`, as in the reviewer picker, the order is the one before.
    expect(logins("cyr", sources).slice(0, 2)).toEqual(["cyrus-n_data", "cyrus"]);
  });

  it("breaks ties by source: the repository's people before the rest of the organization", () => {
    const tied = preparePeople(
      mergePeople({ assignable: [user("sam-b")], member: [user("sam-a")] }),
    );
    expect(logins("sam", tied)).toEqual(["sam-b", "sam-a"]);
  });

  it("ranks the people worked with recently after the PR's reviewers and before everyone else", () => {
    const sources = preparePeople(
      mergePeople({
        reviewer: [user("rex")],
        recent: [user("sam-r"), user("ann")],
        assignable: [user("sam-b"), user("ann", "Ann Lee")],
        member: [user("sam-a"), user("sam-r", "Sam Ray")],
      }),
    );
    // A tie goes to the recent person; with no query, recent people follow the reviewers
    // in the shell's order, most worked with first.
    expect(logins("sam", sources)).toEqual(["sam-r", "sam-b", "sam-a"]);
    expect(logins("", sources)).toEqual(["rex", "sam-r", "ann", "sam-b", "sam-a"]);
    // A recent person takes their name from another source, and a better match still wins.
    expect(sources.find(({ person }) => person.login === "sam-r")?.person.name).toBe("Sam Ray");
    expect(logins("sam-a", sources)).toEqual(["sam-a"]);
  });

  it("matches names without their accents and ignores a leading @", () => {
    expect(logins("angstrom", people)).toEqual(["zoe"]);
    expect(logins("@cyan", people)).toEqual(["cyan"]);
  });

  it("doesn't match a name by scattered letters", () => {
    expect(
      logins("cmd", preparePeople(mergePeople({ member: [user("x", "Cyrus Omidvar")] }))),
    ).toEqual([]);
  });

  it("lists people in source order for an empty query, up to the limit", () => {
    expect(logins("", people, 3)).toEqual(["cyan", "dcyrus", "cyrus-o"]);
  });

  it("keeps only the best `limit` matches", () => {
    const many = preparePeople(
      mergePeople({ member: Array.from({ length: 200 }, (_, index) => user(`dev${index}`)) }),
    );
    const ranked = logins("dev", many);
    expect(ranked).toHaveLength(PEOPLE_LIMIT);
    // Shorter logins are better prefix matches, so the single digits lead.
    expect(ranked.slice(0, 10)).toEqual(Array.from({ length: 10 }, (_, index) => `dev${index}`));
  });
});

describe("a large organization", () => {
  const directory = Array.from({ length: 50_000 }, (_, index) =>
    user(`member${index.toString(36)}`, `Person ${index}`),
  );
  directory.push(user("cyrus-o", "Cyrus Omidvar"));

  it("folds each person once, then only matches on each keystroke", () => {
    const fold = vi.spyOn(match, "prepareFields");
    const people = preparePeople(mergePeople({ member: directory }));
    expect(fold).toHaveBeenCalledTimes(directory.length);
    fold.mockClear();
    const tokenize = vi.spyOn(match, "tokenizeQuery");
    for (const query of ["c", "cy", "cyr", "cyrus"]) rankPeople(query, people);
    expect(fold).not.toHaveBeenCalled();
    expect(tokenize).toHaveBeenCalledTimes(4);
    fold.mockRestore();
    tokenize.mockRestore();
  });

  it("ranks each keystroke well within a frame budget", () => {
    const people = preparePeople(mergePeople({ member: directory }));
    const times: number[] = [];
    let last: string[] = [];
    for (const query of ["c", "cy", "cyr", "cyrus"]) {
      const started = performance.now();
      last = logins(query, people);
      times.push(performance.now() - started);
    }
    expect(last[0]).toBe("cyrus-o");
    expect(last.length).toBeLessThanOrEqual(PEOPLE_LIMIT);
    // Generous, so a slow CI machine doesn't flake: the point is no per-key fold or full sort.
    expect(Math.max(...times.slice(1))).toBeLessThan(400);
  });
});

describe("orgMembersNote", () => {
  const status = (overrides: Partial<OrgMembersStatus>): OrgMembersStatus => ({
    organization: "acme",
    state: "ready",
    complete: false,
    truncated: false,
    failure: null,
    login: "mac-octo",
    ...overrides,
  });

  it("explains a first fill calmly and says nothing once a list has loaded", () => {
    expect(orgMembersNote(status({ state: "filling" }))).toBe(
      "Loading people in acme for the first time. This happens about once a week.",
    );
    expect(orgMembersNote(status({ state: "ready", complete: true }))).toBeNull();
    expect(orgMembersNote(status({ state: "refreshing", complete: true }))).toBeNull();
    expect(
      orgMembersNote(status({ state: "ready", complete: true, failure: "no_access" })),
    ).toBeNull();
    expect(orgMembersNote(status({ state: "off" }))).toBeNull();
    expect(orgMembersNote(null)).toBeNull();
  });

  it("says when a list is truncated", () => {
    expect(orgMembersNote(status({ complete: true, truncated: true }))).toBe(
      "Showing the first 50,000 people in acme. Type a login to request anyone else.",
    );
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
      "account_invalid",
      "The GitHub sign-in for @mac-octo on this Mac has expired. Run gh auth login, or type a login.",
    ],
    [
      "no_access",
      "GitHub didn't let @mac-octo list people in acme. Type a login to request anyone.",
    ],
    ["query_failed", "Couldn't load people in acme. Type a login to request anyone."],
    ["something_new", "Couldn't load people in acme. Type a login to request anyone."],
  ])("explains %s without a list", (failure, line) => {
    expect(orgMembersNote(status({ state: "unavailable", failure }))).toBe(line);
  });

  it("explains a rate-limit pause during a first fill", () => {
    expect(orgMembersNote(status({ state: "paused", failure: "rate_limited" }))).toBe(
      "GitHub is limiting requests. The people list will finish loading later; you can still type a login.",
    );
  });

  it("offers a retry only for failures the user can fix", () => {
    expect(orgMembersRetryable(status({ state: "unavailable", failure: "missing_scope" }))).toBe(
      true,
    );
    expect(orgMembersRetryable(status({ state: "unavailable", failure: "sso_required" }))).toBe(
      true,
    );
    expect(orgMembersRetryable(status({ state: "paused", failure: "rate_limited" }))).toBe(false);
    expect(
      orgMembersRetryable(status({ state: "ready", complete: true, failure: "no_access" })),
    ).toBe(false);
    expect(orgMembersRetryable(status({ state: "filling" }))).toBe(false);
    expect(orgMembersRetryable(null)).toBe(false);
  });

  it("names the repository when its candidates fail", () => {
    expect(reviewCandidatesFailureNote("acme/app")).toBe(
      "Couldn't load who can review in acme/app. Type a login to request anyone.",
    );
    expect(reviewCandidatesFailureNote(null)).toBe(
      "Couldn't load who can review here. Type a login to request anyone.",
    );
  });
});

describe("organizationListRereadMs", () => {
  const answer = (overrides: Partial<OrgMembersAnswer>): OrgMembersAnswer => ({
    object: "list",
    organization: "acme",
    data: [],
    state: "ready",
    complete: true,
    truncated: false,
    fetched_at: null,
    next_attempt_at: null,
    failure: null,
    ...overrides,
  });

  it("re-reads the shell's memory often while filling, at the pause's end, and rarely when ready", () => {
    const now = Date.parse("2026-11-06T12:00:00Z");
    expect(organizationListRereadMs(answer({ state: "filling" }), now)).toBe(3_000);
    expect(organizationListRereadMs(answer({ state: "refreshing" }), now)).toBe(3_000);
    expect(
      organizationListRereadMs(
        answer({ state: "paused", next_attempt_at: "2026-11-06T12:10:00Z" }),
        now,
      ),
    ).toBe(600_000);
    expect(
      organizationListRereadMs(
        answer({ state: "paused", next_attempt_at: "2026-11-06T12:00:01Z" }),
        now,
      ),
    ).toBe(30_000);
    expect(organizationListRereadMs(answer({ state: "ready" }), now)).toBe(300_000);
    expect(organizationListRereadMs(answer({ state: "off" }), now)).toBe(false);
    expect(organizationListRereadMs(undefined, now)).toBe(false);
  });
});
