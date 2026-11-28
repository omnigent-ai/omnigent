// The selection's Open on GitHub, Copy Links, Copy review site Links and Copy Branch Names: what
// each covers of the selection, its labels, and the values it opens or copies, in list order.

import { describe, expect, it, vi } from "vitest";

import type { PullRequestListEntry } from "@/shell/PullRequestListRows";
import {
  EXAMPLE_REVIEW_SITE,
  EXAMPLE_DEVICE_HOST,
  withPullRequestIntegrations,
} from "@/test/pullRequestIntegrations";

import {
  NO_BRANCHES_REASON,
  OPEN_IN_SIDEBAR_AT_MOST,
  OPEN_WITHOUT_ASKING,
  TOO_MANY_FOR_SIDEBAR_REASON,
  type SelectionLinkEffects,
  selectionLinkActions,
} from "./selectionLinkActions";

// Example Host stand-in hosts are recognized by name, as a build that registers Example Host does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_DEVICE_HOST] });

const urlOf = (number: number) => `https://github.com/acme/app/pull/${number}`;

function entryOf(
  number: number,
  {
    headRef = `feature-${number}`,
    provider,
    url = urlOf(number),
  }: {
    headRef?: string | null;
    provider?: string;
    url?: string;
  } = {},
): PullRequestListEntry {
  return {
    association: { url, number, provider, repository: "acme/app", host: "github.com" },
    row: { url, number, title: `Change ${number}`, headRef, repository: "acme/app" },
  } as unknown as PullRequestListEntry;
}

function actionsFor(entries: PullRequestListEntry[], over: Partial<SelectionLinkEffects> = {}) {
  const effects: SelectionLinkEffects = {
    reviewSite: null,
    open: vi.fn(),
    confirmOpen: vi.fn(),
    copy: vi.fn(),
    ...over,
  };
  const actions = selectionLinkActions(entries, effects);
  const action = (id: string) => actions.find((candidate) => candidate.id === id);
  return { effects, actions, action };
}

describe("a selection's open and copy actions", () => {
  it("opens and copies every selected PR, in list order, without a duplicate", () => {
    const { effects, actions, action } = actionsFor([entryOf(3), entryOf(1), entryOf(3)]);
    expect(actions.map((candidate) => [candidate.label, candidate.paletteLabel])).toEqual([
      ["Open on GitHub", "Open 2 Pull Requests on GitHub"],
      ["Copy Links", "Copy Links to 2 Pull Requests"],
      ["Copy Branch Names", "Copy Branch Names of 2 Pull Requests"],
    ]);
    // The selection bar keeps them in its More menu.
    expect(actions.every((candidate) => candidate.inMoreMenu)).toBe(true);
    action("open-on-provider")!.run();
    expect(effects.open).toHaveBeenCalledWith([
      { url: urlOf(3), href: urlOf(3) },
      { url: urlOf(1), href: urlOf(1) },
    ]);
    action("copy-links")!.run();
    expect(effects.copy).toHaveBeenLastCalledWith({
      text: `${urlOf(3)}\n${urlOf(1)}`,
      copied: "Copied 2 links",
      failed: "Couldn’t copy the links",
    });
    action("copy-branch-names")!.run();
    expect(effects.copy).toHaveBeenLastCalledWith({
      text: "feature-3\nfeature-1",
      copied: "Copied 2 branch names",
      failed: "Couldn’t copy the branch names",
    });
  });

  it("says how many it copies where some branches aren't known yet, and why it can't for none", () => {
    const some = actionsFor([entryOf(1), entryOf(2, { headRef: null }), entryOf(3)]);
    expect(some.action("copy-branch-names")).toMatchObject({
      label: "Copy Branch Names (2 of 3)",
      paletteLabel: "Copy Branch Names of 2 Pull Requests",
      unavailableReason: undefined,
    });
    const none = actionsFor([entryOf(1, { headRef: null })]);
    expect(none.action("copy-branch-names")?.unavailableReason).toBe(NO_BRANCHES_REASON);
  });

  it("asks first above ten, with the links it would open then", () => {
    const eleven = Array.from({ length: OPEN_WITHOUT_ASKING + 1 }, (_, index) =>
      entryOf(index + 1),
    );
    const { effects, action } = actionsFor(eleven);
    expect(action("open-on-provider")).toMatchObject({
      label: "Open on GitHub…",
      paletteLabel: "Open 11 Pull Requests on GitHub…",
      movesFocus: true,
    });
    action("open-on-provider")!.run();
    expect(effects.open).not.toHaveBeenCalled();
    expect(effects.confirmOpen).toHaveBeenCalledWith(
      eleven.map((entry) => ({ url: entry.association.url, href: entry.association.url })),
      "Open 11 pull requests on GitHub?",
    );
    // Ten open at once.
    const ten = actionsFor(eleven.slice(0, OPEN_WITHOUT_ASKING));
    expect(ten.action("open-on-provider")?.label).toBe("Open on GitHub");
    ten.action("open-on-provider")!.run();
    expect(ten.effects.open).toHaveBeenCalledOnce();
  });

  it("opens in the browser where the selection mixes providers", () => {
    const exampleHost = "https://example-host.example.com/acme/app/pull/9";
    const { action } = actionsFor([
      entryOf(1),
      entryOf(9, { provider: "named-host", url: exampleHost }),
    ]);
    expect(action("open-on-provider")?.label).toBe("Open in Browser");
    expect(action("open-on-provider")?.paletteLabel).toBe("Open 2 Pull Requests in Browser");
  });

  it("copies review site links only where its links show, for the PRs it covers", () => {
    expect(actionsFor([entryOf(1)]).action("copy-review-site-links")).toBeUndefined();
    const { effects, action } = actionsFor([entryOf(1), entryOf(2)], {
      reviewSite: EXAMPLE_REVIEW_SITE,
    });
    expect(action("copy-review-site-links")?.label).toBe("Copy Example Review Links");
    action("copy-review-site-links")!.run();
    expect(vi.mocked(effects.copy).mock.calls[0]![0]).toMatchObject({
      copied: "Copied 2 Example Review links",
    });
    expect(vi.mocked(effects.copy).mock.calls[0]![0].text.split("\n")).toHaveLength(2);
  });

  it("opens every selected PR in the workspace sidebar, in list order, where the list can", () => {
    const openInWorkspace = vi.fn();
    const { actions, action } = actionsFor([entryOf(3), entryOf(1), entryOf(3)], {
      openInWorkspace,
    });
    expect(action("open-in-workspace")).toMatchObject({
      label: "Open in Workspace Sidebar",
      paletteLabel: "Open 2 Pull Requests in Workspace Sidebar",
      unavailableReason: undefined,
    });
    action("open-in-workspace")!.run();
    expect(openInWorkspace).toHaveBeenCalledExactlyOnceWith([urlOf(3), urlOf(1)]);
    // The row menu nests opens under Open › and copies under Copy ›, in that order.
    expect(actions.map((candidate) => [candidate.menuSection, candidate.nestedLabel])).toEqual([
      ["open", "In Workspace Sidebar"],
      ["open", "On GitHub"],
      ["copy", "Links"],
      ["copy", "Branch Names"],
    ]);
    // A list that doesn't open PRs in the workspace offers none.
    expect(actionsFor([entryOf(1)]).action("open-in-workspace")).toBeUndefined();
  });

  it("opens at most 20 in the sidebar, and says how to open them", () => {
    const entries = (count: number) =>
      Array.from({ length: count }, (_, index) => entryOf(index + 1));
    const openInWorkspace = vi.fn();
    expect(
      actionsFor(entries(OPEN_IN_SIDEBAR_AT_MOST), { openInWorkspace }).action("open-in-workspace")
        ?.unavailableReason,
    ).toBeUndefined();
    const tooMany = actionsFor(entries(OPEN_IN_SIDEBAR_AT_MOST + 1), { openInWorkspace });
    expect(tooMany.action("open-in-workspace")?.unavailableReason).toBe(
      "Select 20 or fewer to open them in the sidebar.",
    );
    expect(TOO_MANY_FOR_SIDEBAR_REASON).toBe("Select 20 or fewer to open them in the sidebar.");
  });
});
