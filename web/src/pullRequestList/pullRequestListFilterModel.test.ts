// The Pull Requests list's filter chips: what each field holds for a row, told apart as GitHub
// tells them, how chips combine, what the menus count, and that a row whose details aren't
// known is never taken for one that holds nothing.

import { describe, expect, it } from "vitest";

import type { PullRequest, PullRequestReviewer } from "@/newPullRequestPage/hooks/usePullRequests";
import type { PullRequestListEntry } from "@/shell/PullRequestListRows";

import { FILTER_FIELD_BY_ID } from "./pullRequestListFields";
import {
  fieldChoices,
  filterEntries,
  filterOperators,
  filterValuesSummary,
  type ListFilter,
  onlyFilterValue,
  onlyValueOf,
  operatorTitle,
  setFilterOperator,
  toggleChipValue,
  toggleFilterValue,
} from "./pullRequestListFilterModel";
import { EXAMPLE_DEVICE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// Example Host stand-in hosts are recognized by name, as a build that registers Example Host does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_DEVICE_HOST] });

interface Fixture {
  number: number;
  repository?: string;
  host?: string;
  provider?: string;
  /** Undefined: the row's own read hasn't answered. */
  pr?: Partial<PullRequest>;
  status?: PullRequestListEntry["status"];
  offline?: boolean;
}

function entry({
  number,
  repository = "acme/app",
  host = "github.com",
  provider,
  pr,
  status = pr ? "ready" : "loading",
  offline = false,
}: Fixture): PullRequestListEntry {
  const url = `https://${host}/${repository}/pull/${number}`;
  return {
    association: { url, host, repository, number, title: null, relationship: "attached", provider },
    row: { url, number, repository, author: pr?.author ?? null },
    pr: pr ? ({ number, url, author: "ada", ...pr } as PullRequest) : undefined,
    offline,
    status,
    error: null,
  } as unknown as PullRequestListEntry;
}

const label = (name: string) => ({ name, color: null, description: null });
const user = (login: string, state: PullRequestReviewer["state"] = "PENDING") =>
  ({ login, kind: "user", state }) as PullRequestReviewer;
const team = (slug: string) =>
  ({ login: slug, kind: "team", state: "PENDING" }) as PullRequestReviewer;
const numbers = (entries: readonly PullRequestListEntry[]) =>
  entries.map((each) => each.association.number);
const keys = (field: Parameters<typeof fieldChoices>[2], entries: PullRequestListEntry[]) =>
  fieldChoices(entries, [], field).choices.map((choice) => choice.key);

describe("each field's values, told apart as GitHub tells them", () => {
  it("keeps branches case-sensitive: Feature isn't feature", () => {
    const entries = [
      entry({ number: 1, pr: { head_ref: "Feature", base_ref: "main" } }),
      entry({ number: 2, pr: { head_ref: "feature", base_ref: "main" } }),
    ];
    expect(keys("head", entries).sort()).toEqual(["Feature", "feature"]);
    const kept = filterEntries(entries, [
      { field: "head", operator: "anyOf", values: ["Feature"] },
    ]);
    expect(numbers(kept)).toEqual([1]);
  });

  it("keeps a user and a team of the same name apart", () => {
    const entries = [
      entry({ number: 1, pr: { reviewers: [user("infra")] } }),
      entry({ number: 2, pr: { reviewers: [team("infra")] } }),
    ];
    const choices = fieldChoices(entries, [], "reviewer").choices;
    expect(choices.map((choice) => [choice.key, choice.label])).toEqual([
      ["team:github:github.com/acme/infra", "@acme/infra"],
      ["user:github:github.com/infra", "@infra"],
    ]);
  });

  it("keeps one login on two hosts as two people, and ignores a login's case on one", () => {
    const entries = [
      entry({ number: 1, pr: { author: "alice", assignees: ["Bob"] } }),
      entry({ number: 2, host: "github.example.com", pr: { author: "alice", assignees: ["bob"] } }),
      entry({ number: 3, pr: { author: "Alice", assignees: ["bob"] } }),
    ];
    expect(keys("author", entries)).toEqual([
      "github:github.com/alice",
      "github:github.example.com/alice",
    ]);
    expect(keys("assignee", entries)).toEqual([
      "github:github.com/bob",
      "github:github.example.com/bob",
    ]);
  });

  it("keeps a repository per provider and host, ignoring its name's case", () => {
    const entries = [
      entry({ number: 1, repository: "Acme/App" }),
      entry({ number: 2, repository: "acme/app" }),
      entry({
        number: 3,
        repository: "acme/app",
        host: "devices.example.com",
        provider: "named-host",
      }),
    ];
    expect(keys("repository", entries)).toEqual([
      "github:github.com/acme/app",
      "named-host:devices.example.com/acme/app",
    ]);
  });

  it("takes Bug and bug as one label, named as most rows write it", () => {
    const entries = [
      entry({ number: 1, pr: { labels: [label("bug")] } }),
      entry({ number: 2, pr: { labels: [label("bug")] } }),
      entry({ number: 3, pr: { labels: [label("Bug")] } }),
    ];
    const [choice] = fieldChoices(entries, [], "label").choices;
    expect(choice).toMatchObject({ key: "github:github.com/bug", label: "bug", count: 3 });
  });
});

describe("rows whose details aren't known", () => {
  it("never match a chip of the field, and say how many can't tell", () => {
    const entries = [
      entry({ number: 1, pr: { labels: [label("bug")] } }),
      entry({ number: 2, pr: { labels: [] } }),
      // Still being read; offline; and a source that doesn't report labels.
      entry({ number: 3 }),
      entry({ number: 4, offline: true, status: "error" }),
      entry({ number: 5, pr: { labels: undefined } }),
    ];
    const bug = "github:github.com/bug";
    expect(
      numbers(filterEntries(entries, [{ field: "label", operator: "anyOf", values: [bug] }])),
    ).toEqual([1]);
    // Leaving bug out keeps only the row known to lack it.
    expect(
      numbers(filterEntries(entries, [{ field: "label", operator: "noneOf", values: [bug] }])),
    ).toEqual([2]);
    const menu = fieldChoices(entries, [], "label");
    expect([menu.loading, menu.unknown, menu.unsupported]).toEqual([1, 2, false]);
  });

  it("take a read naming no branch as not knowing it, never as having none", () => {
    for (const field of ["head", "base"] as const) {
      const ref = field === "head" ? "head_ref" : "base_ref";
      const entries = [
        entry({ number: 1, pr: { [ref]: "main" } }),
        entry({ number: 2, pr: { [ref]: null } }),
        entry({ number: 3, pr: { [ref]: undefined } }),
        entry({ number: 4, pr: { [ref]: "" } }),
        entry({ number: 5, pr: { [ref]: "release" } }),
      ];
      const notMain: ListFilter = { field, operator: "noneOf", values: ["main"] };
      // Only the row known to be on another branch is not on main.
      expect(numbers(filterEntries(entries, [notMain]))).toEqual([5]);
      const menu = fieldChoices(entries, [], field);
      expect([menu.unknown, menu.loading, menu.unsupported]).toEqual([3, 0, false]);
      expect(menu.choices.map((choice) => choice.key)).toEqual(["main", "release"]);
    }
  });

  it("call a field no row's source reports unsupported, not empty", () => {
    const entries = [
      entry({ number: 1, pr: { assignees: undefined } }),
      entry({ number: 2, pr: { assignees: undefined } }),
    ];
    expect(fieldChoices(entries, [], "assignee")).toMatchObject({
      choices: [],
      unsupported: true,
    });
    // One row that reports none at all: known, and empty.
    const reported = [...entries, entry({ number: 3, pr: { assignees: [] } })];
    expect(fieldChoices(reported, [], "assignee").unsupported).toBe(false);
    // Rows still loading may yet report it.
    expect(fieldChoices([entry({ number: 4 })], [], "assignee").unsupported).toBe(false);
  });
});

describe("chips", () => {
  const bug = "github:github.com/bug";
  const docs = "github:github.com/docs";
  const entries = [
    entry({ number: 1, pr: { labels: [label("bug"), label("docs")], base_ref: "main" } }),
    entry({ number: 2, pr: { labels: [label("bug")], base_ref: "release" } }),
    entry({ number: 3, pr: { labels: [label("docs")], base_ref: "main" } }),
    entry({ number: 4, pr: { labels: [], base_ref: "main" } }),
  ];

  it("combine their values by their operator, and every chip applies", () => {
    const of = (filters: ListFilter[]) => numbers(filterEntries(entries, filters));
    expect(of([{ field: "label", operator: "anyOf", values: [bug, docs] }])).toEqual([1, 2, 3]);
    expect(of([{ field: "label", operator: "allOf", values: [bug, docs] }])).toEqual([1]);
    expect(of([{ field: "label", operator: "noneOf", values: [bug] }])).toEqual([3, 4]);
    expect(
      of([
        { field: "label", operator: "anyOf", values: [bug] },
        { field: "base", operator: "anyOf", values: ["main"] },
      ]),
    ).toEqual([1]);
    expect(of([])).toEqual([1, 2, 3, 4]);
  });

  it("count as the menus count: a field's own chip narrows only a multi-valued field's", () => {
    const base: ListFilter = { field: "base", operator: "anyOf", values: ["main"] };
    // Base's own chip doesn't narrow Base's counts, or release would count 0.
    expect(
      fieldChoices(entries, [base], "base").choices.map((choice) => [choice.key, choice.count]),
    ).toEqual([
      ["main", 3],
      ["release", 1],
    ]);
    // Of those labelled bug, one is labelled docs; order still follows every row's use.
    const labelled: ListFilter = { field: "label", operator: "anyOf", values: [bug] };
    expect(
      fieldChoices(entries, [labelled], "label").choices.map((choice) => [
        choice.key,
        choice.count,
      ]),
    ).toEqual([
      [bug, 2],
      [docs, 1],
    ]);
    // Another field's chip narrows: on main, one is labelled bug.
    expect(fieldChoices(entries, [base], "label").choices.find((c) => c.key === bug)?.count).toBe(
      1,
    );
  });

  it("keep a picked value no row has any more, with 0", () => {
    const gone: ListFilter = {
      field: "label",
      operator: "anyOf",
      values: ["github:github.com/old"],
    };
    expect(fieldChoices(entries, [gone], "label").choices.at(-1)).toMatchObject({
      key: "github:github.com/old",
      count: 0,
      checked: true,
    });
  });

  it("toggle into the chip that has the value, the field's chip, or a new one", () => {
    let filters = toggleFilterValue([], "label", bug, false);
    expect(filters).toEqual([
      { id: expect.any(String), field: "label", operator: "anyOf", values: [bug] },
    ]);
    const { id } = filters[0]!;
    filters = toggleFilterValue(filters, "label", docs, false);
    expect(filters[0]).toMatchObject({ id, values: [bug, docs] });
    // Excluding goes to its own chip.
    filters = toggleFilterValue(filters, "label", "github:github.com/wip", true);
    expect(filters.map((filter) => filter.operator)).toEqual(["anyOf", "noneOf"]);
    // Unchecking the last value removes its chip.
    filters = toggleChipValue(filters, filters[1]!, "github:github.com/wip");
    expect(filters).toHaveLength(1);
  });

  it("keep one chip per field without an Exclude submenu, whatever its operator", () => {
    // Base Branch main, turned to "is not": main stays that chip's, never a second chip.
    const notMain: ListFilter = { id: "c1", field: "base", operator: "noneOf", values: ["main"] };
    expect(toggleFilterValue([notMain], "base", "main")).toEqual([]);
    expect(toggleFilterValue([notMain], "base", "release")).toEqual([
      { ...notMain, values: ["main", "release"] },
    ]);
    // Label keeps requiring and leaving out apart.
    const notBug: ListFilter = { id: "c2", field: "label", operator: "noneOf", values: [bug] };
    expect(toggleFilterValue([notBug], "label", bug, false)).toHaveLength(2);
  });

  it("keep an edited chip's id, so the chip stays the same one", () => {
    const chip: ListFilter = { id: "c3", field: "label", operator: "anyOf", values: [bug] };
    expect(setFilterOperator([chip], chip, "noneOf")[0]!.id).toBe("c3");
    expect(toggleChipValue([chip], chip, docs)[0]!.id).toBe("c3");
  });

  it("offer all-of only where a pull request holds several", () => {
    expect(filterOperators("label")).toEqual(["anyOf", "allOf", "noneOf"]);
    // With one value, any of and all of read alike: only the one in use is offered.
    expect(filterOperators("label", { operator: "anyOf", values: [bug] })).toEqual([
      "anyOf",
      "noneOf",
    ]);
    expect(filterOperators("label", { operator: "allOf", values: [bug] })).toEqual([
      "allOf",
      "noneOf",
    ]);
    expect(filterOperators("label", { operator: "noneOf", values: [bug, docs] })).toEqual([
      "anyOf",
      "allOf",
      "noneOf",
    ]);
    expect(filterOperators("base")).toEqual(["anyOf", "noneOf"]);
    expect(FILTER_FIELD_BY_ID.reviewer.multi).toBe(true);
    const chip: ListFilter = { field: "base", operator: "anyOf", values: ["main"] };
    expect(setFilterOperator([chip], chip, "allOf")).toEqual([chip]);
    expect(setFilterOperator([chip], chip, "noneOf")[0]!.operator).toBe("noneOf");
  });

  it("read as the reference's chips do", () => {
    expect(operatorTitle("anyOf", 1)).toBe("is");
    expect(operatorTitle("anyOf", 2)).toBe("is any of");
    expect(operatorTitle("allOf", 2)).toBe("is all of");
    expect(operatorTitle("noneOf", 1)).toBe("is not");
    expect(filterValuesSummary("label", ["bug"])).toBe("bug");
    expect(filterValuesSummary("head", ["a", "b"])).toBe("2 branches");
  });

  it("narrow to one value, as an author's header asks, keeping exclusions", () => {
    const excluded: ListFilter = { field: "author", operator: "noneOf", values: ["x"] };
    const narrowed = onlyFilterValue(
      [{ field: "author", operator: "anyOf", values: ["a", "b"] }, excluded],
      "author",
      "c",
    );
    expect(narrowed).toEqual([
      excluded,
      { id: expect.any(String), field: "author", operator: "anyOf", values: ["c"] },
    ]);
    expect(onlyValueOf([{ field: "author", operator: "anyOf", values: ["c"] }], "author")).toBe(
      "c",
    );
    expect(onlyValueOf(narrowed, "author")).toBeNull();
    expect(onlyFilterValue(narrowed, "author", null)).toEqual([excluded]);
  });
});

describe("Example Host rows beside github.com's", () => {
  const DEVICE_HOST = "2.host.example.com";
  const UNIVERSE = "example-org/universe";
  const deviceHost = (number: number, pr: Partial<PullRequest>) =>
    entry({ number, host: DEVICE_HOST, provider: "named-host", repository: UNIVERSE, pr });

  it("keys each login, label and reviewer by its own host, never by a _data suffix", () => {
    const entries = [
      entry({ number: 1, pr: { author: "alice_data", labels: [label("infra")] } }),
      deviceHost(2, { author: "alice", labels: [label("infra")], reviewers: [user("alice")] }),
      entry({ number: 3, pr: { author: "alice", reviewers: [user("alice")] } }),
    ];
    // github.com's alice_data, github.com's alice and the deviceHost's alice are three people.
    expect(keys("author", entries).sort()).toEqual([
      "github:github.com/alice",
      "github:github.com/alice_data",
      `named-host:${DEVICE_HOST}/alice`,
    ]);
    expect(keys("label", entries).sort()).toEqual([
      "github:github.com/infra",
      `named-host:${DEVICE_HOST}/infra`,
    ]);
    expect(keys("reviewer", entries).sort()).toEqual([
      "user:github:github.com/alice",
      `user:named-host:${DEVICE_HOST}/alice`,
    ]);
  });

  it("matches a github.com chip only on github.com, and a deviceHost's only on that deviceHost", () => {
    const entries = [
      entry({ number: 1, pr: { author: "alice", labels: [label("infra")] } }),
      deviceHost(2, { author: "alice", labels: [label("infra")] }),
    ];
    const only = (filter: ListFilter) => numbers(filterEntries(entries, [filter]));
    expect(
      only({ field: "author", operator: "anyOf", values: ["github:github.com/alice"] }),
    ).toEqual([1]);
    expect(
      only({ field: "author", operator: "anyOf", values: [`named-host:${DEVICE_HOST}/alice`] }),
    ).toEqual([2]);
    expect(
      only({ field: "label", operator: "noneOf", values: ["github:github.com/infra"] }),
    ).toEqual([2]);
    expect(
      only({
        field: "repository",
        operator: "anyOf",
        values: [`named-host:${DEVICE_HOST}/${UNIVERSE}`],
      }),
    ).toEqual([2]);
  });

  it("matches a deviceHost's branches and assignees as that deviceHost names them", () => {
    const entries = [
      entry({ number: 1, pr: { head_ref: "fix", base_ref: "master", assignees: ["bob"] } }),
      deviceHost(2, { head_ref: "fix", base_ref: "master", assignees: ["bob"] }),
    ];
    expect(keys("assignee", entries).sort()).toEqual([
      "github:github.com/bob",
      `named-host:${DEVICE_HOST}/bob`,
    ]);
    expect(
      numbers(
        filterEntries(entries, [
          { field: "assignee", operator: "anyOf", values: [`named-host:${DEVICE_HOST}/bob`] },
          { field: "base", operator: "anyOf", values: ["master"] },
        ]),
      ),
    ).toEqual([2]);
  });
});
