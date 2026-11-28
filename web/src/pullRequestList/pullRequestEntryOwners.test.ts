// Which mounted list a page's PR menu comes from: the table for what it lists while it's
// mounted, else the source beside it, and nothing once neither has the PR.

import { describe, expect, it, vi } from "vitest";

import type { RowAction } from "@/shell/PullRequestListRows";

import { createPullRequestEntryOwners, type ListedEntries } from "./pullRequestEntryOwners";

const PR = "https://github.com/acme/app/pull/7";

function listing(name: string, urls: string[]): { current: ListedEntries } {
  const keys = new Set(urls.map((url) => url.toLowerCase()));
  return {
    current: {
      keys,
      actionsFor: (url) =>
        keys.has(url.toLowerCase()) ? [{ id: name, label: name } as unknown as RowAction] : null,
    },
  };
}

const menuName = (actions: RowAction[] | null) => actions?.[0]?.id ?? null;

describe("PR entry owners", () => {
  it("takes a PR from the table while it lists it, else from beside, however its URL is cased", () => {
    const owners = createPullRequestEntryOwners();
    const removeBeside = owners.register("beside", listing("beside", [PR]));
    expect(menuName(owners.menuFor(PR))).toBe("beside");
    const removeTable = owners.register("table", listing("table", [PR]));
    expect(menuName(owners.menuFor(PR.toUpperCase().replace("HTTPS", "https")))).toBe("table");
    expect(owners.tableKeys()).toBe(PR);

    removeTable();
    expect(menuName(owners.menuFor(PR))).toBe("beside");
    expect(owners.tableKeys()).toBe("");
    removeBeside();
    expect(owners.menuFor(PR)).toBeNull();
  });

  it("offers nothing from a table that doesn't list the PR", () => {
    const owners = createPullRequestEntryOwners();
    owners.register("table", listing("table", ["https://github.com/acme/app/pull/8"]));
    expect(owners.menuFor(PR)).toBeNull();
  });

  it("reads what each list has when asked, and says when it changes", () => {
    const owners = createPullRequestEntryOwners();
    const table = listing("table", []);
    owners.register("table", table);
    const heard = vi.fn();
    owners.subscribe(heard);
    const before = owners.version();
    table.current = listing("table", [PR]).current;
    owners.changed();
    expect(heard).toHaveBeenCalledTimes(1);
    expect(owners.version()).toBeGreaterThan(before);
    expect(owners.tableKeys()).toBe(PR);
    expect(menuName(owners.menuFor(PR))).toBe("table");
  });
});
