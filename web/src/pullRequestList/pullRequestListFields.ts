// The fields the Pull Requests list filters by, each once: what a row holds for it, how one
// value is told from another, and whether a pull request can hold several. A row whose details
// haven't been read, or whose source doesn't report a field, holds nothing known for it, which
// the filters never mistake for holding none.

import type { PullRequestListEntry } from "@/shell/PullRequestListRows";

import { authorFilterKey, authorOf } from "./authorFilter";

export type FilterFieldId =
  "repository" | "author" | "label" | "head" | "base" | "assignee" | "reviewer";

/** One value a field can hold: `key` tells values apart; `label` names it. */
export interface FieldValue {
  key: string;
  label: string;
}

/**
 * What a row holds for a field: its values, or why they aren't known. `loading`: its live read
 * hasn't answered yet. `unknown`: it won't answer (offline, or the read failed). `unsupported`:
 * the source that read it doesn't report the field, such as an older desktop app's assignees.
 */
export type FieldValues =
  | { known: true; values: readonly FieldValue[] }
  | { known: false; reason: "loading" | "unknown" | "unsupported" };

export interface FilterField {
  id: FilterFieldId;
  label: string;
  /** A pull request holds several, so a chip can ask for all of them. */
  multi: boolean;
  valuesOf: (entry: PullRequestListEntry) => FieldValues;
  /** A value's name from its key alone, for a picked value no row holds any more. */
  labelOfKey: (key: string) => string;
}

/** What follows a key's provider and host, e.g. `acme/app` of `github:github.com/acme/app`. */
const afterHost = (key: string) => key.slice(key.indexOf("/") + 1);
const person = (key: string) => `@${key.slice(key.lastIndexOf("/") + 1)}`;

const known = (values: readonly FieldValue[]): FieldValues => ({ known: true, values });

/** Why a row's live details aren't there to read. */
function notRead(entry: PullRequestListEntry): FieldValues {
  if (entry.status === "loading" && !entry.offline) return { known: false, reason: "loading" };
  return { known: false, reason: "unknown" };
}

/** The provider and host a row's PR lives on, as the filters key them. */
function hostKey(entry: PullRequestListEntry): string {
  return `${entry.association.provider ?? "github"}:${entry.association.host.toLowerCase()}`;
}

/** A repository, as the filters key it: provider, host and `owner/name`, ignoring case. */
export function repositoryValueKey(entry: PullRequestListEntry): string {
  return `${hostKey(entry)}/${entry.association.repository.toLowerCase()}`;
}

/** Values a live read reports in `list`, keyed by `key`; unsupported where it reports none. */
function fromLive<Item>(
  entry: PullRequestListEntry,
  list: (pr: NonNullable<PullRequestListEntry["pr"]>) => readonly Item[] | undefined,
  value: (item: Item) => FieldValue | null,
): FieldValues {
  if (!entry.pr) return notRead(entry);
  const items = list(entry.pr);
  if (items === undefined) return { known: false, reason: "unsupported" };
  const seen = new Set<string>();
  const values: FieldValue[] = [];
  for (const item of items) {
    const found = value(item);
    if (!found || seen.has(found.key)) continue;
    seen.add(found.key);
    values.push(found);
  }
  return known(values);
}

/**
 * A branch, by its exact name: git refs are case-sensitive, so `Feature` isn't `feature`. Every
 * pull request has both, so a read that names none couldn't tell: not known, rather than none.
 */
function branchOf(entry: PullRequestListEntry, ref: "head_ref" | "base_ref"): FieldValues {
  if (!entry.pr) return notRead(entry);
  const name = entry.pr[ref];
  if (typeof name !== "string" || !name) return { known: false, reason: "unknown" };
  return known([{ key: name, label: name }]);
}

export const FILTER_FIELDS: readonly FilterField[] = [
  {
    id: "repository",
    label: "Repository",
    multi: false,
    valuesOf: (entry) =>
      known([{ key: repositoryValueKey(entry), label: entry.association.repository }]),
    labelOfKey: afterHost,
  },
  {
    id: "author",
    label: "Author",
    multi: false,
    valuesOf: (entry) => {
      // The row names its author once any read did, its own live read or the list's.
      if (!entry.pr && !entry.row.author) return notRead(entry);
      const author = authorOf(entry);
      // A deleted account leaves the PR without one: known, and none.
      return known(author ? [{ key: authorFilterKey(author), label: `@${author.login}` }] : []);
    },
    labelOfKey: person,
  },
  {
    id: "label",
    label: "Label",
    multi: true,
    // GitHub's label names are unique ignoring case, so `Bug` and `bug` are one label.
    valuesOf: (entry) =>
      fromLive(
        entry,
        (pr) => pr.labels,
        (label) =>
          label.name
            ? { key: `${hostKey(entry)}/${label.name.toLowerCase()}`, label: label.name }
            : null,
      ),
    labelOfKey: afterHost,
  },
  {
    id: "head",
    label: "Head Branch",
    multi: false,
    valuesOf: (entry) => branchOf(entry, "head_ref"),
    labelOfKey: (key) => key,
  },
  {
    id: "base",
    label: "Base Branch",
    multi: false,
    valuesOf: (entry) => branchOf(entry, "base_ref"),
    labelOfKey: (key) => key,
  },
  {
    id: "assignee",
    label: "Assignee",
    multi: true,
    valuesOf: (entry) =>
      fromLive(
        entry,
        (pr) => pr.assignees,
        (login) =>
          login
            ? {
                key: authorFilterKey({
                  provider: entry.association.provider,
                  host: entry.association.host,
                  login,
                }),
                label: `@${login}`,
              }
            : null,
      ),
    labelOfKey: person,
  },
  {
    id: "reviewer",
    label: "Reviewer",
    multi: true,
    valuesOf: (entry) =>
      fromLive(
        entry,
        (pr) => pr.reviewers,
        (reviewer) => {
          if (!reviewer.login) return null;
          if (reviewer.kind === "team") {
            // GitHub names a team by its slug; the teams a PR can ask are its repository owner's.
            const owner = entry.association.repository.split("/")[0]!.toLowerCase();
            return {
              key: `team:${hostKey(entry)}/${owner}/${reviewer.login.toLowerCase()}`,
              label: `@${owner}/${reviewer.login}`,
            };
          }
          return {
            key: `user:${hostKey(entry)}/${reviewer.login.toLowerCase()}`,
            label: `@${reviewer.login}`,
          };
        },
      ),
    // A team's key names its organization too: `@acme/infra`.
    labelOfKey: (key) =>
      key.startsWith("team:") ? `@${afterHost(key.slice("team:".length))}` : person(key),
  },
];

export const FILTER_FIELD_BY_ID: Readonly<Record<FilterFieldId, FilterField>> = Object.fromEntries(
  FILTER_FIELDS.map((field) => [field.id, field]),
) as Record<FilterFieldId, FilterField>;

export function isFilterFieldId(value: unknown): value is FilterFieldId {
  return FILTER_FIELDS.some((field) => field.id === value);
}
