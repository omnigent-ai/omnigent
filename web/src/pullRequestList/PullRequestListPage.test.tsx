import { listRemovalOf } from "./subAgentRemoval";
import { createRef } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type * as ReactVirtual from "@tanstack/react-virtual";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CheckIcon, ExternalLinkIcon, XIcon } from "lucide-react";
import { toast } from "sonner";
import type * as Clipboard from "@/lib/clipboard";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  PullRequestChecks,
  PullRequestReviewer,
} from "@/newPullRequestPage/hooks/usePullRequests";
import type { PullRequestListStatsResult } from "@/hooks/usePullRequestListStats";
import type { PullRequestListStatsGap } from "@/lib/pullRequestListStats";
import type { PullRequestListRow } from "@/shell/pullRequestListModel";
import type { PullRequestListEntry, RowAction } from "@/shell/PullRequestListRows";
import type {
  PullRequestListEntries,
  PullRequestListProps,
} from "@/shell/usePullRequestListEntries";
import { isCommandAvailable, usePaletteCommandsStore } from "@/store/paletteCommands";
import { registeredPaletteCommands } from "@/test/paletteCommands";
import { resetCollapsedStacks } from "./collapsedStacks";
import type { ListSelectionAction } from "./listSelectionActions";
import {
  type PullRequestListFocus,
  PullRequestListPage,
  type PullRequestListPageProps,
} from "./PullRequestListPage";
import { createPullRequestEntryOwners } from "./pullRequestEntryOwners";
import { pullRequestKey } from "@/lib/pullRequestReference";
import { runningBulkRuns, startBulkRun } from "./bulkRuns";
import type { RemovalProgress } from "./removalProgress";
import type { PullRequestListMore } from "./PullRequestListMore";
import { DEFAULT_PAGE_DISPLAY, type PageGrouping } from "./pullRequestListPageModel";
import {
  DEFAULT_PULL_REQUEST_LIST_PAGE_VIEW,
  pullRequestListPageViewKey,
} from "./usePullRequestListPageView";
import {
  EXAMPLE_DEVICE_HOST,
  withPullRequestIntegrations,
  exampleMergeReadinessSource,
  EXAMPLE_STACK_FORMAT,
} from "@/test/pullRequestIntegrations";

// Merge readiness from a stand-in source, as a build that registers one shows it.
withPullRequestIntegrations({
  mergeReadiness: exampleMergeReadinessSource(async () => ({ kind: "checking" })),
});

// Stack descriptions in an example format, which the page reads once a build registers it.
withPullRequestIntegrations({ stackFormats: [EXAMPLE_STACK_FORMAT] });

// Example Host stand-in hosts are recognized by name, as a build that registers Example Host does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_DEVICE_HOST] });

const PR_ONE = "https://github.com/acme/app/pull/1";

it.each(["keyboard", "palette", "group"])(
  "%s removal leaves a sub-agent's PR linked to its own session",
  async (route) => {
    fixture.rows = [
      row({ number: 1, state: "merged" }),
      row({
        number: 2,
        state: "merged",
        sessionSource: { session: { id: "child-review", name: "Child" }, alsoIn: [] },
      }),
    ];
    const user = userEvent.setup();
    const { grid, onUnlink } = renderList();
    if (route === "group") {
      const removeAll = Object.values(usePaletteCommandsStore.getState().groups)
        .flatMap((group) => group.commands)
        .find((candidate) => candidate.id.endsWith("merged.remove"));
      expect(removeAll).toBeDefined();
      act(() => removeAll!.run());
    } else {
      await user.click(within(rowFor(fixture.rows[0]!.url)).getByRole("checkbox"));
      await user.click(within(rowFor(fixture.rows[1]!.url)).getByRole("checkbox"));
      if (route === "keyboard") {
        act(() => grid.focus());
        await user.keyboard("{Meta>}{Backspace}{/Meta}");
      } else {
        const removeSelected = Object.values(usePaletteCommandsStore.getState().groups)
          .flatMap((group) => group.commands)
          .find((candidate) => candidate.id === "pull-requests.remove-selected");
        expect(removeSelected).toBeDefined();
        act(() => removeSelected!.run());
      }
    }
    expect(onUnlink).toHaveBeenCalledExactlyOnceWith([PR_ONE]);
  },
);
it("the global Pull Requests list offers no sub-agent switch, command or Agent grouping", async () => {
  const user = userEvent.setup();
  renderList({ subAgentPullRequests: false });
  await user.click(screen.getByRole("button", { name: "Display Options" }));
  expect(screen.queryByRole("switch", { name: "Include sub-agent pull requests" })).toBeNull();
  await user.click(screen.getByRole("combobox", { name: "Group by" }));
  expect(await screen.findByRole("option", { name: /^Author$/ })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: /^Agent$/ })).toBeNull();
  const commands = Object.values(usePaletteCommandsStore.getState().groups).flatMap(
    (group) => group.commands,
  );
  expect(commands.map((command) => command.id)).not.toContain("pull-requests.include-sub-agents");
  const grouping = commands.find((command) => command.setting?.id === "pull-requests.grouping");
  const groupings = grouping?.setting?.choices.map((choice) => choice.label);
  expect(groupings).toContain("Author");
  expect(groupings).not.toContain("Agent");
});

it("a session's list offers the sub-agent switch, its command and Agent grouping", async () => {
  const user = userEvent.setup();
  renderList({ subAgentPullRequests: true });
  await user.click(screen.getByRole("button", { name: "Display Options" }));
  expect(
    screen.getByRole("switch", { name: "Include sub-agent pull requests" }),
  ).toBeInTheDocument();
  await user.click(screen.getByRole("combobox", { name: "Group by" }));
  expect(await screen.findByRole("option", { name: /^Agent$/ })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: /^Session$/ })).toBeNull();
  await user.keyboard("{Escape}");
  const commandIds = Object.values(usePaletteCommandsStore.getState().groups)
    .flatMap((group) => group.commands)
    .map((command) => command.id);
  expect(commandIds).toContain("pull-requests.include-sub-agents");
});

const removeSelectedCommand = () =>
  Object.values(usePaletteCommandsStore.getState().groups)
    .flatMap((group) => group.commands)
    .find((candidate) => candidate.id === "pull-requests.remove-selected");

it("leaves a sub-agent's PR alone where the user doesn't own the tree, and says who links it", async () => {
  fixture.rows = [
    row({
      number: 2,
      state: "open",
      sessionSource: { session: { id: "child-review", name: "Child" }, alsoIn: [] },
    }),
  ];
  const user = userEvent.setup();
  renderList({ ownsSessionTree: false });
  await user.click(within(rowFor(fixture.rows[0]!.url)).getByRole("checkbox"));
  const command = removeSelectedCommand();
  expect(command).toBeDefined();
  expect(isCommandAvailable(command!.availability)).toBe(false);
  expect(command!.availability).toEqual(
    expect.objectContaining({
      available: false,
      reason: "Still linked by Child, which you don’t own.",
    }),
  );
});

it("removes a sub-agent's PR from that sub-agent in a tree the user owns, from ⌘K and the bar", async () => {
  fixture.rows = [
    row({
      number: 2,
      state: "open",
      sessionSource: { session: { id: "child-review", name: "Child" }, alsoIn: [] },
    }),
  ];
  const user = userEvent.setup();
  const { onUnlink } = renderList({ ownsSessionTree: true });
  await user.click(within(rowFor(fixture.rows[0]!.url)).getByRole("checkbox"));
  const command = removeSelectedCommand()!;
  expect(command.label).toBe("Remove 1 selected pull request from Child");
  expect(isCommandAvailable(command.availability)).toBe(true);
  const bar = screen.getByRole("toolbar", { name: "Selected pull requests" });
  expect(within(bar).getByRole("button", { name: /^Remove/ })).toHaveAttribute(
    "title",
    expect.stringMatching(/^Remove from Child \(/),
  );
  act(() => command.run());
  expect(onUnlink).toHaveBeenCalledExactlyOnceWith([fixture.rows[0]!.url]);
});

it("names this session and its agents for a selection of both", async () => {
  fixture.rows = [
    row({ number: 1, state: "open" }),
    row({
      number: 2,
      state: "open",
      sessionSource: { session: { id: "child-review", name: "Child" }, alsoIn: [] },
    }),
  ];
  const user = userEvent.setup();
  renderList({ ownsSessionTree: true });
  await user.click(within(rowFor(fixture.rows[0]!.url)).getByRole("checkbox"));
  await user.click(within(rowFor(fixture.rows[1]!.url)).getByRole("checkbox"));
  expect(removeSelectedCommand()).toMatchObject({
    label: "Remove 2 selected pull requests from this session and its agents",
    description: "Unlinks them from this session and Child.",
  });
  // The bar's tooltip names them too.
  const bar = screen.getByRole("toolbar", { name: "Selected pull requests" });
  expect(within(bar).getByRole("button", { name: /^Remove/ })).toHaveAttribute(
    "title",
    expect.stringMatching(
      /^Remove from this session and its agents \(.+\)\nUnlinks them from this session and Child\.$/,
    ),
  );
});

const paletteCommandById = (id: string) =>
  Object.values(usePaletteCommandsStore.getState().groups)
    .flatMap((group) => group.commands)
    .find((candidate) => candidate.id === id);

describe("a finished group's Remove All", () => {
  const closedFromChild = () => {
    fixture.rows = [
      row({ number: 1, state: "merged" }),
      row({
        number: 2,
        state: "closed",
        sessionSource: { session: { id: "child-review", name: "Child" }, alsoIn: [] },
      }),
    ];
  };
  const toggleOf = (title: string) =>
    screen.getByRole("button", { name: new RegExp(`^${title} group`) });
  const removeAllOf = (title: string) =>
    within(toggleOf(title).closest(".pr-list-group-header") as HTMLElement).getByRole("button", {
      name: "Remove All",
    });

  /** Tab from the group's toggle to its Remove All, which shows `tooltip` on focus. */
  async function tabToRemoveAll(
    user: ReturnType<typeof userEvent.setup>,
    title: string,
    tooltip: string,
  ) {
    act(() => toggleOf(title).focus());
    await user.tab();
    const removeAll = removeAllOf(title);
    expect(removeAll).toHaveFocus();
    expect(await screen.findByRole("tooltip")).toHaveTextContent(tooltip);
    return removeAll;
  }

  /** Enter, Space and a click each leave the PRs linked. */
  async function expectNothingRemoved(
    user: ReturnType<typeof userEvent.setup>,
    removeAll: HTMLElement,
    onUnlink: ReturnType<typeof vi.fn>,
  ) {
    await user.keyboard("{Enter}");
    await user.keyboard(" ");
    await user.click(removeAll);
    expect(onUnlink).not.toHaveBeenCalled();
  }

  it("removes a sub-agent's closed PR in a tree the user owns, from the header and ⌘K", async () => {
    closedFromChild();
    const user = userEvent.setup();
    const { onUnlink } = renderList({ ownsSessionTree: true });
    const removeAll = await tabToRemoveAll(
      user,
      "Closed",
      "Remove 1 closed pull request from Child",
    );
    // What it does and how many, without naming every session it unlinks them from.
    expect(screen.getByRole("tooltip").textContent).toMatch(
      /^Remove 1 closed pull request from Child$/,
    );
    expect(removeAll).not.toHaveAttribute("aria-disabled");
    expect(
      isCommandAvailable(paletteCommandById("pull-requests.group.closed.remove")!.availability),
    ).toBe(true);
    await user.keyboard("{Enter}");
    expect(onUnlink).toHaveBeenCalledExactlyOnceWith([fixture.rows[1]!.url]);
  });

  it.each([
    [false, "Still linked by Child, which you don’t own."],
    [undefined, "Checking whether you own Child…"],
  ])(
    "stays reachable but removes nothing where ownership is %s, and says why",
    async (owns, reason) => {
      closedFromChild();
      const user = userEvent.setup();
      const { onUnlink } = renderList({ ownsSessionTree: owns });
      const removeAll = await tabToRemoveAll(user, "Closed", reason);
      expect(removeAll).toHaveAttribute("aria-disabled", "true");
      await expectNothingRemoved(user, removeAll, onUnlink);
      expect(paletteCommandById("pull-requests.group.closed.remove")).toMatchObject({
        label: "Remove the closed pull requests",
        availability: expect.objectContaining({ available: false, reason }),
      });
      // Merged's own link stays removable beside it.
      expect(removeAllOf("Merged")).not.toHaveAttribute("aria-disabled");
      expect(paletteCommandById("pull-requests.group.merged.remove")!.label).toBe(
        "Remove 1 merged pull request from this session",
      );
    },
  );

  it.each(["Merged", "Closed"])(
    "waits, reachable, while a removal is still running, in %s",
    async (title) => {
      closedFromChild();
      const user = userEvent.setup();
      const { onUnlink } = renderList({ ownsSessionTree: true, unlinkPending: true });
      // Read without the tooltip too, as a screen reader's cursor meets it.
      expect(removeAllOf(title)).toHaveAccessibleDescription(
        "Wait for the pull request being removed to finish.",
      );
      const removeAll = await tabToRemoveAll(
        user,
        title,
        "Wait for the pull request being removed to finish.",
      );
      expect(removeAll).toHaveAttribute("aria-disabled", "true");
      await expectNothingRemoved(user, removeAll, onUnlink);
    },
  );

  describe("while the removal under way has its rows", () => {
    const progressOf = (urls: string[], over: Partial<RemovalProgress> = {}): RemovalProgress => ({
      phase: "removing",
      urls: new Set(urls.map(pullRequestKey)),
      done: 1,
      total: 3,
      stop: "available",
      runId: null,
      ...over,
    });
    /** A running batch whose Stop the busy controls offer, and whether it was asked to stop. */
    const stoppableRun = () => {
      const run = startBulkRun({ label: "Removing pull requests", total: 3 });
      const stopped = () =>
        runningBulkRuns().find((snapshot) => snapshot.id === run.id)?.stopRequested ?? false;
      return { runId: run.id, stopped, end: () => run.end() };
    };
    const busyOf = (title: string) =>
      within(toggleOf(title).closest(".pr-list-group-header") as HTMLElement).getAllByRole(
        "button",
      )[1]!;

    it("shows the running group busy at rest with its count, Stop on it, and asks the other group to wait", async () => {
      closedFromChild();
      const user = userEvent.setup();
      const run = stoppableRun();
      const { onUnlink } = renderList({
        ownsSessionTree: true,
        unlinkPending: true,
        removalProgress: progressOf([fixture.rows[0]!.url], { runId: run.runId }),
      });
      const busy = busyOf("Merged");
      expect(busy).toHaveTextContent("Removing 1 of 3");
      expect(busy).toHaveAccessibleName("Stop removing");
      expect(busy).toHaveAttribute("aria-busy", "true");
      expect(busy).toHaveAttribute("data-stoppable", "true");
      expect(busy).not.toHaveAttribute("aria-disabled");
      expect(busy).toHaveAccessibleDescription("Removing 1 of 3 pull requests…");
      // Only the control is busy, not its group or the list.
      expect(busy.closest(".pr-list-group-header")).not.toHaveAttribute("aria-busy");
      await user.click(busy);
      expect(run.stopped()).toBe(true);
      expect(onUnlink).not.toHaveBeenCalled();
      run.end();

      const waiting = await tabToRemoveAll(
        user,
        "Closed",
        "Wait for the 3 pull requests being removed to finish.",
      );
      expect(waiting).toHaveAttribute("aria-disabled", "true");
      expect(waiting).not.toHaveAttribute("aria-busy");
      expect(paletteCommandById("pull-requests.group.merged.remove")?.availability).toEqual(
        expect.objectContaining({ available: false, reason: "Removing 1 of 3 pull requests…" }),
      );
      expect(paletteCommandById("pull-requests.group.closed.remove")?.availability).toEqual(
        expect.objectContaining({
          available: false,
          reason: "Wait for the 3 pull requests being removed to finish.",
        }),
      );
    });

    it("keeps a busy group that can't be stopped unavailable, removing nothing", async () => {
      closedFromChild();
      const user = userEvent.setup();
      const { onUnlink } = renderList({
        ownsSessionTree: true,
        unlinkPending: true,
        removalProgress: progressOf([fixture.rows[0]!.url], { stop: "none" }),
      });
      const busy = busyOf("Merged");
      expect(busy).toHaveAccessibleName("Removing 1 of 3");
      expect(busy).toHaveAttribute("aria-disabled", "true");
      expect(busy).not.toHaveAttribute("data-stoppable");
      await expectNothingRemoved(user, busy, onUnlink);
    });

    it.each([
      [
        "a single removal, which has no Stop",
        { done: 0, total: 1, stop: "none" as const },
        "Removing…",
        "Removing this pull request…",
      ],
      [
        "a batch asked to stop",
        { stop: "requested" as const },
        "Stopping…",
        "Stopping after the requests already sent…",
      ],
      [
        "a removal queued behind another",
        { phase: "waiting" as const, done: null, stop: "none" as const },
        "Waiting…",
        "Waiting for another removal to finish.",
      ],
    ])("says so for %s", (_case, over, label, reason) => {
      closedFromChild();
      renderList({
        ownsSessionTree: true,
        unlinkPending: true,
        removalProgress: progressOf([fixture.rows[0]!.url], over),
      });
      const busy = busyOf("Merged");
      expect(busy).toHaveAccessibleName(label);
      expect(busy).toHaveAccessibleDescription(reason);
      expect(busy.textContent).not.toMatch(/notification/);
    });

    it("shows the selection's Remove busy, as Stop, and removes nothing", async () => {
      closedFromChild();
      const user = userEvent.setup();
      const run = stoppableRun();
      const { onUnlink } = renderList({
        ownsSessionTree: true,
        unlinkPending: true,
        removalProgress: progressOf([fixture.rows[0]!.url], { runId: run.runId }),
      });
      await user.click(within(rowFor(fixture.rows[0]!.url)).getByRole("checkbox"));
      const bar = screen.getByRole("toolbar", { name: "Selected pull requests" });
      const remove = within(bar).getByRole("button", { name: "Stop removing" });
      expect(remove).toHaveTextContent("Removing 1 of 3");
      expect(remove).not.toHaveAttribute("aria-disabled");
      expect(remove).toHaveAttribute("aria-busy", "true");
      expect(remove).toHaveAccessibleDescription("Removing 1 of 3 pull requests…");
      await user.keyboard("{Meta>}{Backspace}{/Meta}");
      expect(run.stopped()).toBe(false);
      act(() => remove.focus());
      await user.keyboard("{Enter}");
      expect(run.stopped()).toBe(true);
      expect(onUnlink).not.toHaveBeenCalled();
      run.end();
    });

    it("keeps the selection's busy Remove unavailable where the removal can't be stopped", async () => {
      closedFromChild();
      const user = userEvent.setup();
      const { onUnlink } = renderList({
        ownsSessionTree: true,
        unlinkPending: true,
        removalProgress: progressOf([fixture.rows[0]!.url], { stop: "none" }),
      });
      await user.click(within(rowFor(fixture.rows[0]!.url)).getByRole("checkbox"));
      const bar = screen.getByRole("toolbar", { name: "Selected pull requests" });
      const remove = within(bar).getByRole("button", { name: /^Removing 1 of 3/ });
      expect(remove).toHaveAttribute("aria-disabled", "true");
      await user.click(remove);
      await user.keyboard("{Meta>}{Backspace}{/Meta}");
      expect(onUnlink).not.toHaveBeenCalled();
    });

    it("gives Remove All back once the removal settles", () => {
      closedFromChild();
      const props: Partial<PullRequestListPageProps> = {
        ownsSessionTree: true,
        unlinkPending: true,
        removalProgress: progressOf([fixture.rows[0]!.url]),
      };
      const view = renderList(props);
      expect(busyOf("Merged")).toHaveAccessibleName("Removing 1 of 3");
      const toggle = toggleOf("Merged");
      // The removal settles in the same list, which keeps its group.
      Object.assign(props, { unlinkPending: false, removalProgress: null });
      view.rerender();
      expect(toggleOf("Merged")).toBe(toggle);
      expect(removeAllOf("Merged")).not.toHaveAttribute("aria-disabled");
      expect(removeAllOf("Merged")).not.toHaveAttribute("aria-busy");
    });
  });
});

const NO_CHECKS: PullRequestChecks = { passing: 0, failing: 0, pending: 0, total: 0, runs: [] };

const fixture = vi.hoisted(() => ({
  rows: [] as PullRequestListRow[],
  host: "github.com",
  hostsFromUrls: false,
  canUnlink: true,
  stats: "ready" as "ready" | "unsupported" | "failed",
  /** The desktop shell can read stats, so Diff and Updated are laid out. */
  statsColumns: true,
  statsGaps: new Map<string, PullRequestListStatsGap>(),
  /** Where the row menu's "Show details" ran, and whether its menu was still open. */
  detailsShown: [] as { number: number; menuOpen: boolean }[],
  /** Descriptions the rows' reads brought, by URL; a row without one wasn't read. */
  bodies: new Map<string, string>(),
  /** What other fields the rows' reads brought, by URL, such as labels or the base branch. */
  prs: new Map<string, Record<string, unknown>>(),
  stacksFlag: false,
  /** Windowing, on by default; off draws every row as before. */
  windowFlag: true,
  /** What the list asked its rows' reads to follow, render by render. */
  readWindows: [] as ({ visible: readonly string[]; exceptions: readonly string[] } | undefined)[],
}));

vi.mock("@/hooks/useDesktopFeatureFlag", async () =>
  (await import("@/test/desktopFeatureFlagMock")).desktopFeatureFlagModule((name, fallback) => ({
    value:
      name === "pull-request-stacks"
        ? fixture.stacksFlag
        : name === "pull-request-list-pages"
          ? fixture.windowFlag
          : fallback,
    resolved: true,
  })),
);

const clipboard = vi.hoisted(() => ({ copied: [] as string[], refuse: false }));
vi.mock("@/lib/clipboard", async (importOriginal) => ({
  ...(await importOriginal<typeof Clipboard>()),
  copyText: async (text: string) => {
    if (clipboard.refuse) throw new Error("Refused");
    clipboard.copied.push(text);
  },
}));

vi.mock("@/shell/usePullRequestListEntries", () => ({
  usePullRequestListEntries: (
    props: PullRequestListProps,
    options?: { readWindow?: { visible: readonly string[]; exceptions: readonly string[] } },
  ): PullRequestListEntries => {
    fixture.readWindows.push(options?.readWindow);
    const entries: PullRequestListEntry[] = fixture.rows.map((listRow) => ({
      association: {
        url: listRow.url,
        // A row's own link names its host where a test lists more than one.
        host: fixture.hostsFromUrls ? new URL(listRow.url).hostname : fixture.host,
        repository: listRow.repository,
        number: listRow.number,
        relationship: "created",
      },
      row: listRow,
      pr:
        fixture.bodies.has(listRow.url) || fixture.prs.has(listRow.url)
          ? ({
              url: listRow.url,
              body: fixture.bodies.get(listRow.url),
              ...fixture.prs.get(listRow.url),
            } as PullRequestListEntry["pr"])
          : undefined,
      offline: false,
      statsService: "github",
      statsGap: fixture.statsGaps.get(listRow.url),
      status: "ready",
      error: undefined,
      mergeHead: undefined,
      mergeHint: undefined,
      mergeUnavailable: true,
    }));
    const actionsFor = (entry: PullRequestListEntry): RowAction[] => [
      {
        id: "open-on-provider",
        section: "open",
        label: "Open on GitHub",
        paletteLabel: `Open #${entry.row.number} on GitHub`,
        icon: ExternalLinkIcon,
        href: entry.row.url,
        run: () => {},
      },
      {
        id: "show-details" as RowAction["id"],
        section: "open",
        label: "Show details",
        paletteLabel: `Show #${entry.row.number}'s details`,
        icon: ExternalLinkIcon,
        movesFocus: true,
        run: () =>
          fixture.detailsShown.push({
            number: entry.row.number,
            menuOpen: document.querySelector('[role="menu"]') !== null,
          }),
      },
      ...(fixture.canUnlink
        ? [
            {
              id: "remove" as const,
              section: "remove" as const,
              label: "Remove from this session",
              paletteLabel: `Remove #${entry.row.number} from this session`,
              icon: XIcon,
              run: () => props.onUnlink?.([entry.row.url]),
            },
          ]
        : []),
    ];
    return {
      entries,
      rows: fixture.rows,
      entriesByUrl: new Map(entries.map((entry) => [entry.row.url, entry])),
      stats: {
        status: fixture.stats,
        byUrl: new Map(),
      } as unknown as PullRequestListStatsResult,
      statsColumns: fixture.statsColumns,
      // As the hook: a list that doesn't manage its session's links never removes any.
      canUnlink: fixture.canUnlink && !!props.onUnlink && props.managesSessionLinks !== false,
      // The real rule, from the rows' sources as the hook reads them.
      removalOf: (url) => {
        const canUnlink =
          fixture.canUnlink && !!props.onUnlink && props.managesSessionLinks !== false;
        return listRemovalOf({
          shown: fixture.rows.map((candidate) => candidate.url),
          sources: new Map(
            fixture.rows.flatMap((candidate) =>
              candidate.sessionSource ? [[candidate.url, candidate.sessionSource] as const] : [],
            ),
          ),
          viewedSessionId: props.conversationId,
          canEditViewed: canUnlink,
          ownsTree: canUnlink ? props.ownsSessionTree : false,
        })(url);
      },
      finished: [],
      actionsFor,
      mergeRequestedFor: () => false,
      unlinkFinishedCommand: undefined,
      rowCommands: [],
      mergeDialog: null,
      changeInFlightFor: () => null,
    };
  },
}));

function row(over: Partial<PullRequestListRow> = {}): PullRequestListRow {
  const number = over.number ?? 1;
  return {
    url: `https://github.com/acme/app/pull/${number}`,
    number,
    title: `Change ${number}`,
    repository: "acme/app",
    state: "open",
    author: "ada",
    headRef: `ada/change-${number}`,
    labels: [],
    checks: NO_CHECKS,
    ci: { source: "live", checks: NO_CHECKS },
    stateRead: true,
    checksIncomplete: false,
    review: undefined,
    reviewers: [],
    stats: {
      additions: 10 * number,
      deletions: number,
      changedFiles: 1,
      updatedAt: Date.now() - number * 60_000,
      headSha: "head",
      dispatchedAt: 0,
      extended: true,
    },
    needsYou: false,
    inferred: false,
    ...over,
  };
}

const reviewer = (login: string, state: PullRequestReviewer["state"]): PullRequestReviewer => ({
  login,
  state,
  kind: "user",
});

function renderList(
  over: Partial<PullRequestListPageProps> = {},
  { grouping }: { grouping?: PageGrouping } = {},
) {
  const onSelect = vi.fn();
  const onUnlink = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  let conversationId = "conv_1";
  // A list shown before with this grouping picks it up again.
  if (grouping) {
    client.setQueryData(pullRequestListPageViewKey(conversationId), {
      ...DEFAULT_PULL_REQUEST_LIST_PAGE_VIEW,
      display: { ...DEFAULT_PAGE_DISPLAY, grouping },
    });
  }
  const tree = () => (
    <QueryClientProvider client={client}>
      <PullRequestListPage
        conversationId={conversationId}
        active
        associations={[]}
        hostOffline={false}
        sandboxAsleep={false}
        onSelect={onSelect}
        onUnlink={onUnlink}
        unlinkPending={false}
        {...over}
      />
    </QueryClientProvider>
  );
  const view = render(tree());
  return {
    onSelect,
    onUnlink,
    get grid() {
      return screen.getByRole("grid", { name: "Linked pull requests" });
    },
    rerender: () => view.rerender(tree()),
    switchTo: (next: string) => {
      conversationId = next;
      view.rerender(tree());
    },
  };
}

const rowFor = (url: string) =>
  document.querySelector<HTMLElement>(`[role="row"][data-pull-request-row="${url}"]`)!;
const rowNamed = (title: string) =>
  screen.getAllByRole("row").find((element) => element.textContent?.includes(title))!;
/** A menu's own items, its submenus' triggers among them, not what those submenus hold. */
const topLevelItems = (menu: HTMLElement) =>
  within(menu)
    .getAllByRole("menuitem")
    .map((item) => item.textContent?.trim());
const selectedCount = () =>
  document.querySelector('[aria-label="Selected pull requests"] .pr-list-selection-count')
    ?.textContent ?? null;
const paletteCommand = (label: string) =>
  Object.values(usePaletteCommandsStore.getState().groups)
    .flatMap((group) => group.commands)
    .find((command) => command.label === label);

beforeEach(() => {
  fixture.rows = [
    row({ number: 1, title: "Retry uploads", needsYou: true, review: "review_requested" }),
    row({ number: 2, title: "Ship docs", state: "merged" }),
    row({ number: 3, title: "Try a new layout" }),
    row({ number: 4, title: "Old idea", state: "closed" }),
  ];
  fixture.host = "github.com";
  fixture.hostsFromUrls = false;
  fixture.canUnlink = true;
  fixture.stats = "ready";
  fixture.statsColumns = true;
  fixture.statsGaps = new Map();
  fixture.detailsShown = [];
  fixture.bodies = new Map();
  fixture.prs = new Map();
  fixture.stacksFlag = false;
  fixture.windowFlag = true;
  fixture.readWindows = [];
  localStorage.removeItem("omnigent:pull-requests-collapsed-stacks");
  resetCollapsedStacks();
});

afterEach(() => usePaletteCommandsStore.setState({ groups: {} }));

describe("PullRequestListPage", () => {
  it("keeps the pull request shown beside a long list in view, scrolling a windowed list to its row", async () => {
    const { useVirtualizer } = await import("@tanstack/react-virtual");
    fixture.rows = Array.from({ length: 40 }, (_, index) => row({ number: index + 1 }));
    const scrolledTo: number[] = [];
    const passThrough = vi.mocked(useVirtualizer).getMockImplementation()!;
    vi.mocked(useVirtualizer).mockImplementation(
      (options) =>
        ({
          ...passThrough(options),
          scrollToIndex: (index: number) => {
            scrolledTo.push(index);
          },
        }) as unknown as ReturnType<typeof useVirtualizer>,
    );
    try {
      let selectedUrl = fixture.rows[34]!.url;
      const client = new QueryClient();
      const tree = () => (
        <QueryClientProvider client={client}>
          <PullRequestListPage
            conversationId="conv_1"
            active
            associations={[]}
            hostOffline={false}
            sandboxAsleep={false}
            onSelect={vi.fn()}
            unlinkPending={false}
            selectedUrl={selectedUrl}
          />
        </QueryClientProvider>
      );
      const view = render(tree());
      // One group draws no header, so #35 is the list's 35th item.
      expect(scrolledTo).toEqual([34]);
      expect(rowFor(fixture.rows[34]!.url)).toHaveAttribute("aria-current", "true");
      selectedUrl = fixture.rows[0]!.url;
      view.rerender(tree());
      expect(scrolledTo).toEqual([34, 0]);
    } finally {
      vi.mocked(useVirtualizer).mockImplementation(passThrough);
    }
  });

  it("scrolls a narrow list's revealed row into view at the rows' stacked height", async () => {
    const { useVirtualizer } = await import("@tanstack/react-virtual");
    const actual = await vi.importActual<typeof ReactVirtual>("@tanstack/react-virtual");
    const passThrough = vi.mocked(useVirtualizer).getMockImplementation()!;
    vi.mocked(useVirtualizer).mockImplementation(actual.useVirtualizer);
    // A 400px-wide sidebar list, 300px tall, where the stylesheet draws each row 52px tall.
    const viewport = { width: 400, height: 300 };
    const narrowRow = 52;
    const scrolls: number[] = [];
    const restorers: (() => void)[] = [];
    // jsdom does no layout: the list alone gets a box; every other element keeps jsdom's.
    const stub = (key: string, descriptor: PropertyDescriptor) => {
      const own = Object.getOwnPropertyDescriptor(HTMLElement.prototype, key);
      Object.defineProperty(HTMLElement.prototype, key, { configurable: true, ...descriptor });
      restorers.push(() => {
        if (own) Object.defineProperty(HTMLElement.prototype, key, own);
        else Reflect.deleteProperty(HTMLElement.prototype, key);
      });
    };
    const listBox: Record<string, number> = {
      clientWidth: viewport.width,
      offsetWidth: viewport.width,
      clientHeight: viewport.height,
      offsetHeight: viewport.height,
      scrollHeight: 100_000,
    };
    for (const [key, size] of Object.entries(listBox)) {
      stub(key, {
        get(this: HTMLElement) {
          return this.classList.contains("pr-list-list") ? size : 0;
        },
      });
    }
    stub("scrollTo", {
      writable: true,
      value(this: HTMLElement, options: ScrollToOptions) {
        if (options.top === undefined) return;
        scrolls.push(options.top);
        this.scrollTop = options.top;
      },
    });
    fixture.rows = Array.from({ length: 40 }, (_, index) => row({ number: index + 1 }));
    try {
      render(
        <QueryClientProvider client={new QueryClient()}>
          <PullRequestListPage
            conversationId="conv_1"
            active
            associations={[]}
            hostOffline={false}
            sandboxAsleep={false}
            onSelect={vi.fn()}
            unlinkPending={false}
            selectedUrl={fixture.rows[34]!.url}
          />
        </QueryClientProvider>,
      );
      // One group draws no header, so #35's top is 34 stacked rows down.
      const rowTop = 34 * narrowRow;
      // Every scroll away from the top lands the row in view, never at a wide row's offset first.
      const reveals = () => scrolls.filter((top) => top !== 0);
      await waitFor(() => expect(reveals().length).toBeGreaterThan(0));
      for (const scrollTop of reveals()) {
        expect(scrollTop).toBeLessThanOrEqual(rowTop);
        expect(scrollTop + viewport.height).toBeGreaterThanOrEqual(rowTop + narrowRow);
      }
      expect(rowFor(fixture.rows[34]!.url)).toHaveAttribute("aria-current", "true");
    } finally {
      for (const restore of restorers) restore();
      vi.mocked(useVirtualizer).mockImplementation(passThrough);
    }
  });

  it("reads only the rows in view and the active row, however many rows are selected", async () => {
    const { useVirtualizer } = await import("@tanstack/react-virtual");
    // Six screens of rows; the viewport holds the first 20, and every row is drawn under jsdom.
    fixture.rows = Array.from({ length: 120 }, (_, index) => row({ number: index + 1 }));
    const passThrough = vi.mocked(useVirtualizer).getMockImplementation()!;
    vi.mocked(useVirtualizer).mockImplementation(
      (options) =>
        ({
          ...passThrough(options),
          range: { startIndex: 0, endIndex: 19 },
        }) as unknown as ReturnType<typeof useVirtualizer>,
    );
    try {
      const selectionActions = () => [
        {
          id: "approve",
          label: "Approve…",
          paletteLabel: "Approve",
          icon: CheckIcon,
          run: () => {},
        },
      ];
      renderList({ managesSessionLinks: false, selectionActions });
      const user = userEvent.setup();
      const grid = screen.getByRole("grid", { name: "Pull requests" });
      act(() => grid.focus());
      await user.keyboard("{Meta>}a{/Meta}");
      expect(document.querySelectorAll(".pr-list-row.selected")).toHaveLength(120);
      // Cleared, then a Shift-range from the first row to the 100th.
      await user.keyboard("{Escape}");
      expect(document.querySelectorAll(".pr-list-row.selected")).toHaveLength(0);
      fireEvent.click(rowFor(fixture.rows[0]!.url).querySelector(".pr-list-cell-updated")!);
      fireEvent.click(rowFor(fixture.rows[99]!.url).querySelector(".pr-list-cell-updated")!, {
        shiftKey: true,
      });
      expect(document.querySelectorAll(".pr-list-row.selected")).toHaveLength(100);
      const firstScreen = fixture.rows.slice(0, 20).map((each) => each.url);
      for (const window of fixture.readWindows.filter(Boolean)) {
        expect(window!.visible.every((url) => firstScreen.includes(url))).toBe(true);
        // The active row reads wherever it is; the selection never does.
        expect(window!.exceptions.length).toBeLessThanOrEqual(1);
      }
      expect(fixture.readWindows.at(-1)?.exceptions).toEqual([fixture.rows[99]!.url]);
    } finally {
      vi.mocked(useVirtualizer).mockImplementation(passThrough);
    }
  }, 30_000);

  it("keeps the keyboard's active row mounted when a scroll leaves it far behind", async () => {
    const { useVirtualizer } = await import("@tanstack/react-virtual");
    fixture.rows = Array.from({ length: 120 }, (_, index) => row({ number: index + 1 }));
    const passThrough = vi.mocked(useVirtualizer).getMockImplementation()!;
    // A real window: only the items the range extractor names are drawn.
    let range = { startIndex: 0, endIndex: 31 };
    vi.mocked(useVirtualizer).mockImplementation((options) => {
      const base = passThrough(options);
      const indices = new Set(
        options.rangeExtractor!({ ...range, overscan: 0, count: options.count }),
      );
      return {
        ...base,
        range,
        getVirtualItems: () => base.getVirtualItems().filter((item) => indices.has(item.index)),
      } as unknown as ReturnType<typeof useVirtualizer>;
    });
    try {
      const view = renderList({ managesSessionLinks: false });
      const grid = screen.getByRole("grid", { name: "Pull requests" });
      act(() => grid.focus());
      const activeId = grid.getAttribute("aria-activedescendant");
      expect(activeId).toBeTruthy();
      expect(document.getElementById(activeId!)).not.toBeNull();
      // A wheel scroll to the end, far past the overscan.
      range = { startIndex: 88, endIndex: 119 };
      view.rerender();
      expect(grid).toHaveFocus();
      expect(document.getElementById(activeId!)).not.toBeNull();
      expect(grid).toHaveAttribute("aria-activedescendant", activeId);
      // Only the viewport's rows and the active one are drawn; the rest is space.
      expect(document.querySelectorAll(".pr-list-row")).toHaveLength(32 + 1);
      expect(
        document.querySelector(`[data-pull-request-row="${fixture.rows[40]!.url}"]`),
      ).toBeNull();
      // The active row reads only as the window's exception, not by being drawn.
      const window = fixture.readWindows.at(-1)!;
      expect(window.visible).not.toContain(fixture.rows[0]!.url);
      expect(window.exceptions).toEqual([fixture.rows[0]!.url]);
    } finally {
      vi.mocked(useVirtualizer).mockImplementation(passThrough);
    }
  });

  it("keeps the pull request shown beside a long list in view, on restore and each step", () => {
    fixture.windowFlag = false;
    fixture.rows = Array.from({ length: 40 }, (_, index) => row({ number: index + 1 }));
    const revealed: string[] = [];
    const reveal = vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(function (
      this: Element,
    ) {
      revealed.push(this.getAttribute("data-pull-request-row") ?? "");
    });
    try {
      let selectedUrl = fixture.rows[34]!.url;
      const client = new QueryClient();
      const tree = () => (
        <QueryClientProvider client={client}>
          <PullRequestListPage
            conversationId="conv_1"
            active
            associations={[]}
            hostOffline={false}
            sandboxAsleep={false}
            onSelect={vi.fn()}
            unlinkPending={false}
            selectedUrl={selectedUrl}
          />
        </QueryClientProvider>
      );
      const view = render(tree());
      // A restored tab beside #35 shows its row, current and active.
      expect(revealed).toEqual([fixture.rows[34]!.url]);
      const shown = rowFor(fixture.rows[34]!.url);
      expect(shown).toHaveAttribute("aria-current", "true");
      expect(screen.getByRole("grid")).toHaveAttribute("aria-activedescendant", shown.id);
      // Back to #1 from far down the list brings its row back into view.
      selectedUrl = fixture.rows[0]!.url;
      view.rerender(tree());
      expect(revealed).toEqual([fixture.rows[34]!.url, fixture.rows[0]!.url]);
      expect(rowFor(fixture.rows[0]!.url)).toHaveAttribute("aria-current", "true");
    } finally {
      reveal.mockRestore();
    }
  });

  it("runs a row menu action that moves focus once the menu has closed", async () => {
    const user = userEvent.setup();
    const { onUnlink } = renderList();

    fireEvent.contextMenu(rowNamed("Retry uploads"));
    await user.click(await screen.findByRole("menuitem", { name: "Show details" }));
    await vi.waitFor(() => expect(fixture.detailsShown).toEqual([{ number: 1, menuOpen: false }]));

    // An action that leaves focus alone runs right away, as before.
    fireEvent.contextMenu(rowNamed("Ship docs"));
    await user.click(await screen.findByRole("menuitem", { name: "Remove from this session" }));
    expect(onUnlink).toHaveBeenCalledWith([fixture.rows[1]!.url]);
  });

  it("shows the title and count, the status chips, and lifecycle groups", () => {
    renderList();

    expect(screen.getByRole("heading", { level: 1, name: "Pull Requests" })).toBeVisible();
    const chips = within(screen.getByRole("navigation", { name: "Status" }));
    expect(chips.getAllByRole("button").map((chip) => chip.getAttribute("aria-label"))).toEqual([
      "All (4)",
      "Needs You (1)",
      "Open (2)",
      "Merged (1)",
      "Closed (1)",
    ]);
    expect(screen.getByRole("button", { name: "Open group (2)" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    // Only finished groups offer Remove All.
    const removeAll = screen.getAllByRole("button", { name: "Remove All" });
    expect(removeAll).toHaveLength(2);
    expect(
      within(rowNamed("Retry uploads")).getByRole("img", { name: "Review Requested" }),
    ).toBeVisible();
  });

  it("leaves the title to a page that names itself, and lists its notices above the table", () => {
    renderList({ showHeading: false, notices: <p>Some links didn’t load.</p> });

    expect(screen.queryByRole("heading", { level: 1 })).toBeNull();
    const notice = screen.getByText("Some links didn’t load.");
    const chips = screen.getByRole("navigation", { name: "Status" });
    const grid = screen.getByRole("grid", { name: "Linked pull requests" });
    // Below the status filters, so the header never moves for a notice.
    expect(chips.compareDocumentPosition(notice) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(notice.compareDocumentPosition(grid) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("keeps the header around a placeholder, and offers nothing for the rows it hides", async () => {
    const user = userEvent.setup();
    // A cached list can outlive what the page shows, e.g. once GitHub reads are turned off.
    renderList({ placeholder: <p>Loading pull requests…</p> });

    expect(screen.getByText("Loading pull requests…")).toBeVisible();
    expect(screen.queryByRole("grid")).toBeNull();
    expect(screen.getByRole("searchbox", { name: "Filter pull requests" })).toBeDisabled();
    // Nothing behind a placeholder is known, so the chip names no count rather than 0.
    const status = screen.getByRole("navigation", { name: "Status" });
    const all = within(status).getByRole("button", { name: "All" });
    expect(all).toBeVisible();
    expect(all).not.toHaveTextContent(/\d/);
    // Neither / nor ⌘K offers a field that can't take a filter.
    await user.click(all);
    await user.keyboard("/");
    expect(screen.getByRole("searchbox", { name: "Filter pull requests" })).not.toHaveFocus();
    const commands = Object.values(usePaletteCommandsStore.getState().groups).flatMap(
      (group) => group.commands,
    );
    expect(commands.map((command) => command.label)).toEqual(["Filter pull requests"]);
    expect(commands[0]?.availability).toEqual({
      available: false,
      reason: "There are no pull requests to filter yet.",
    });
  });

  it("shows only the empty state for a list with no pull requests, without its controls", () => {
    renderList({
      showHeading: false,
      notices: <p>Some links didn’t load.</p>,
      emptyState: <p>No reviews waiting on you</p>,
    });

    expect(screen.getByText("No reviews waiting on you")).toBeVisible();
    expect(screen.getByText("Some links didn’t load.")).toBeVisible();
    expect(screen.queryByRole("grid")).toBeNull();
    expect(screen.queryByRole("searchbox", { name: "Filter pull requests" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Display Options" })).toBeNull();
    expect(screen.queryByRole("navigation", { name: "Status" })).toBeNull();
    const commands = Object.values(usePaletteCommandsStore.getState().groups).flatMap(
      (group) => group.commands,
    );
    expect(commands.map((command) => command.label)).toEqual(["Filter pull requests"]);
    expect(commands[0]?.availability).toEqual({
      available: false,
      reason: "There are no pull requests to filter yet.",
    });
  });

  it("keeps a loading or error placeholder in its own layout over an empty state", () => {
    renderList({
      placeholder: <p>Loading pull requests…</p>,
      emptyState: <p>No reviews waiting on you</p>,
    });

    expect(screen.getByText("Loading pull requests…")).toBeVisible();
    expect(screen.queryByText("No reviews waiting on you")).toBeNull();
    expect(screen.getByRole("searchbox", { name: "Filter pull requests" })).toBeDisabled();
    expect(screen.getByRole("navigation", { name: "Status" })).toBeVisible();
  });

  it.each([
    ["alone", undefined],
    ["beside an empty state", <p key="empty">No reviews waiting on you</p>],
  ])("holds the table's place for a null placeholder %s", (_case, emptyState) => {
    // The page passes null while a saved list restores or an error notice explains.
    renderList({ placeholder: null, emptyState });

    expect(screen.queryByRole("grid")).toBeNull();
    expect(screen.queryByText("No pull requests match")).toBeNull();
    expect(screen.queryByText("No reviews waiting on you")).toBeNull();
    expect(screen.getByRole("searchbox", { name: "Filter pull requests" })).toBeDisabled();
  });

  describe("focusRow", () => {
    const activeTitle = (grid: HTMLElement) =>
      document.getElementById(grid.getAttribute("aria-activedescendant") ?? "")?.textContent ?? "";

    it("focuses the list on a row from no active row, ahead of its first-row fallback", () => {
      const handle = createRef<PullRequestListFocus>();
      const { grid } = renderList({ focusHandle: handle });
      expect(grid).not.toHaveAttribute("aria-activedescendant");
      act(() => void expect(handle.current!.focusRow(fixture.rows[2]!.url)).toBe(true));
      expect(grid).toHaveFocus();
      expect(activeTitle(grid)).toContain("Try a new layout");
      // Rows are grouped by state, so the merged one follows the open ones.
      fireEvent.keyDown(grid, { key: "ArrowDown" });
      expect(activeTitle(grid)).toContain("Ship docs");
    });

    it("keeps the active row for a row that's gone, and says when there's no list", () => {
      const handle = createRef<PullRequestListFocus>();
      const view = renderList({
        focusHandle: handle,
        // As the page does, an empty list shows its empty state in the table's place.
        get emptyState() {
          return fixture.rows.length > 0 ? undefined : <p>No pull requests</p>;
        },
      });
      act(() => void handle.current!.focusRow(fixture.rows[1]!.url));
      act(() => void handle.current!.focusRow("https://github.com/acme/app/pull/99"));
      expect(activeTitle(view.grid)).toContain("Ship docs");
      fixture.rows = [];
      view.rerender();
      expect(screen.queryByRole("grid")).toBeNull();
      expect(handle.current!.focusRow(PR_ONE)).toBe(false);
    });
  });

  it("opens a pull request from its number or title, and only selects on a row click", async () => {
    const user = userEvent.setup();
    const { onSelect } = renderList();

    await user.click(within(rowNamed("Retry uploads")).getByText("Retry uploads"));
    expect(onSelect).toHaveBeenLastCalledWith("https://github.com/acme/app/pull/1", "here");

    fireEvent.click(within(rowNamed("Ship docs")).getByText("#2"), {
      metaKey: true,
      ctrlKey: true,
    });
    expect(onSelect).toHaveBeenLastCalledWith("https://github.com/acme/app/pull/2", "background");

    onSelect.mockClear();
    await user.click(rowNamed("Try a new layout").querySelector(".pr-list-cell-updated")!);
    expect(rowNamed("Try a new layout")).toHaveClass("active");
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("names a Example Host row's host in its title's tooltip, not in a label beside it", () => {
    const deviceHostUrl = (shard: number, number: number) =>
      `https://${shard}.host.example.com/example-org/universe/pull/${number}`;
    const repository = "example-org/universe";
    fixture.rows = [
      row({ number: 21, title: "DeviceHost one change", repository, url: deviceHostUrl(1, 21) }),
      row({ number: 30, title: "DeviceHost two change", repository, url: deviceHostUrl(2, 30) }),
      row({ number: 5, title: "GitHub change" }),
    ];
    renderList();
    for (const [shard, number] of [
      [1, 21],
      [2, 30],
    ] as const) {
      const deviceHostRow = rowFor(deviceHostUrl(shard, number));
      expect(deviceHostRow.textContent).not.toMatch(/DeviceHost \d/);
      expect(deviceHostRow.querySelector("a.pr-list-title")).toHaveAttribute(
        "title",
        `${number === 21 ? "DeviceHost one" : "DeviceHost two"} change\n${repository}\nada/change-${number}\nOn ${shard}.host.example.com`,
      );
    }
    expect(
      rowFor("https://github.com/acme/app/pull/5").querySelector("a.pr-list-title"),
    ).toHaveAttribute("title", "GitHub change\nacme/app\nada/change-5");
  });

  it("selects with the checkbox, Shift-click and the keyboard, then removes the selection", async () => {
    const user = userEvent.setup();
    const { onUnlink, grid } = renderList();

    await user.click(within(rowNamed("Retry uploads")).getByRole("checkbox"));
    fireEvent.click(within(rowNamed("Try a new layout")).getByRole("checkbox"), {
      shiftKey: true,
    });
    const bar = screen.getByRole("toolbar", { name: "Selected pull requests" });
    expect(within(bar).getByText("2")).toBeVisible();

    await user.click(within(bar).getByRole("button", { name: /^Remove/ }));
    expect(onUnlink).toHaveBeenCalledWith([
      "https://github.com/acme/app/pull/1",
      "https://github.com/acme/app/pull/3",
    ]);

    // The last checkbox made its row active; j moves on from it to the merged row.
    act(() => grid.focus());
    await user.keyboard("j");
    await user.keyboard("x");
    expect(screen.getByRole("toolbar", { name: "Selected pull requests" })).toBeVisible();
    await user.keyboard("{Meta>}{Backspace}{/Meta}");
    expect(onUnlink).toHaveBeenLastCalledWith(["https://github.com/acme/app/pull/2"]);
  });

  it.each([
    ["macOS", "MacIntel", "⌘+⌫"],
    ["Linux", "Linux x86_64", "Ctrl+⌫"],
  ])(
    "labels Remove with %s's key, and removes the selection with ⌘ or Control",
    async (_os, platform, label) => {
      vi.spyOn(navigator, "platform", "get").mockReturnValue(platform);
      const user = userEvent.setup();
      const { onUnlink, grid } = renderList();
      act(() => grid.focus());
      await user.keyboard("x");

      const bar = screen.getByRole("toolbar", { name: "Selected pull requests" });
      const remove = within(bar).getByRole("button", { name: /^Remove/ });
      expect(remove).toHaveAttribute("title", `Remove from this session (${label})`);
      expect(remove).toHaveTextContent(label);
      // The ⌘K row shows the same key.
      expect(
        registeredPaletteCommands().find(
          (command) => command.shortcut === "pullRequests.removeSelected",
        )?.id,
      ).toBe("pull-requests.remove-selected");

      // Backspace alone doesn't remove; ⌘ and Control both do, on every platform.
      await user.keyboard("{Backspace}");
      expect(onUnlink).not.toHaveBeenCalled();
      await user.keyboard("{Meta>}{Backspace}{/Meta}");
      expect(onUnlink).toHaveBeenCalledTimes(1);
      // Removing cleared the selection; select the row again.
      act(() => grid.focus());
      await user.keyboard("x");
      await user.keyboard("{Control>}{Backspace}{/Control}");
      expect(onUnlink).toHaveBeenCalledTimes(2);
      expect(onUnlink).toHaveBeenLastCalledWith(["https://github.com/acme/app/pull/1"]);
      vi.restoreAllMocks();
    },
  );

  it("offers no selection or Remove where the list doesn't manage a session's links", async () => {
    fixture.rows[2] = row({ number: 3, title: "Try a new layout", inferred: true });
    const user = userEvent.setup();
    const { onSelect, onUnlink } = renderList({ managesSessionLinks: false });
    const grid = screen.getByRole("grid", { name: "Pull requests" });
    expect(grid).toHaveAttribute("aria-multiselectable", "false");
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
    expect(rowNamed("Retry uploads")).not.toHaveAttribute("aria-selected");
    expect(screen.queryByRole("button", { name: "Remove All" })).toBeNull();
    expect(within(rowNamed("Try a new layout")).getByText("From branch")).toHaveAttribute(
      "title",
      "Found from a session’s branch",
    );

    // Clicks that select in a session's list only make a row active, or open its link here.
    fireEvent.click(rowNamed("Ship docs").querySelector(".pr-list-cell-updated")!, {
      metaKey: true,
      ctrlKey: true,
    });
    fireEvent.click(rowNamed("Try a new layout").querySelector(".pr-list-cell-updated")!, {
      shiftKey: true,
    });
    expect(rowNamed("Try a new layout")).toHaveClass("active");
    fireEvent.click(within(rowNamed("Retry uploads")).getByText("Retry uploads"), {
      shiftKey: true,
    });
    expect(onSelect).toHaveBeenLastCalledWith("https://github.com/acme/app/pull/1", "here");

    // Selection keys do nothing, and Shift with j only moves the active row.
    act(() => grid.focus());
    await user.keyboard("x");
    await user.keyboard(" ");
    await user.keyboard("{Meta>}a{/Meta}");
    await user.keyboard("{Shift>}j{/Shift}");
    expect(rowNamed("Try a new layout")).toHaveClass("active");
    await user.keyboard("{Meta>}{Backspace}{/Meta}");
    expect(screen.queryByRole("toolbar", { name: "Selected pull requests" })).toBeNull();
    expect(document.querySelector(".pr-list-row.selected")).toBeNull();
    expect(onUnlink).not.toHaveBeenCalled();

    const labels = Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) =>
      group.commands.map((command) => command.label),
    );
    expect(labels).toEqual(
      expect.arrayContaining(["Filter pull requests", "Order pull requests ascending"]),
    );
    expect(labels.filter((label) => /select|remove|unlink/i.test(label))).toEqual([]);
  });

  it("selects rows to act on together where the list offers selection actions, never Remove", async () => {
    const user = userEvent.setup();
    const approve = vi.fn();
    const remove = vi.fn();
    const seen: (readonly string[])[] = [];
    const selectionActions = (selected: readonly PullRequestListEntry[]): ListSelectionAction[] => {
      const urls = selected.map((entry) => entry.row.url);
      seen.push(urls);
      return [
        {
          id: "approve",
          label: "Approve…",
          paletteLabel: `Approve ${urls.length} Pull Requests…`,
          icon: CheckIcon,
          movesFocus: true,
          run: () => approve(urls),
        },
        {
          id: "remove-me",
          label: "Remove Me as Reviewer",
          paletteLabel: `Remove Me as Reviewer on ${urls.length} Pull Requests`,
          icon: XIcon,
          unavailableReason: "A team asked for these reviews.",
          run: remove,
        },
      ];
    };
    const { onUnlink } = renderList({ managesSessionLinks: false, selectionActions });
    const grid = screen.getByRole("grid", { name: "Pull requests" });
    expect(grid).toHaveAttribute("aria-multiselectable", "true");
    expect(screen.queryByRole("button", { name: "Remove All" })).toBeNull();

    // Click one row's checkbox, then Shift-click another: the range between them, as shown
    // (open ones first), is selected.
    fireEvent.click(within(rowNamed("Retry uploads")).getByRole("checkbox"));
    fireEvent.click(within(rowNamed("Try a new layout")).getByRole("checkbox"), { shiftKey: true });
    const bar = screen.getByRole("toolbar", { name: "Selected pull requests" });
    expect(within(bar).getByText("2")).toBeInTheDocument();
    expect(within(bar).queryByRole("button", { name: /^Remove$/ })).toBeNull();
    expect(seen.at(-1)).toHaveLength(2);

    // An action it can't take now says why and does nothing; one it can acts on the selection.
    const removeMe = within(bar).getByRole("button", { name: "Remove Me as Reviewer" });
    expect(removeMe).toHaveAttribute("aria-disabled", "true");
    expect(removeMe).toHaveAttribute("title", "A team asked for these reviews.");
    await user.click(removeMe);
    expect(remove).not.toHaveBeenCalled();
    await user.click(within(bar).getByRole("button", { name: "Approve…" }));
    expect(approve).toHaveBeenCalledWith([
      "https://github.com/acme/app/pull/1",
      "https://github.com/acme/app/pull/3",
    ]);

    // ⌘K offers the same two for the selection, and no Remove from this session.
    const commands = Object.values(usePaletteCommandsStore.getState().groups).flatMap(
      (group) => group.commands,
    );
    const offered = commands.map((command) => command.label);
    expect(offered).toEqual(
      expect.arrayContaining([
        "Approve 2 Pull Requests…",
        "Remove Me as Reviewer on 2 Pull Requests",
      ]),
    );
    expect(offered.filter((label) => /from this session/.test(label))).toEqual([]);
    expect(commands.find((command) => command.label.startsWith("Remove Me"))?.availability).toEqual(
      expect.objectContaining({ reason: "A team asked for these reviews." }),
    );

    // ⌘⌫ removes nothing from a list that doesn't manage a session's links.
    act(() => grid.focus());
    await user.keyboard("{Meta>}{Backspace}{/Meta}");
    expect(onUnlink).not.toHaveBeenCalled();
  });

  it("offers the selection's actions in a checked row's context menu, as the bar and ⌘K do", async () => {
    const user = userEvent.setup();
    const close = vi.fn();
    const selectionActions = (selected: readonly PullRequestListEntry[]): ListSelectionAction[] => {
      const urls = selected.map((entry) => entry.row.url);
      return [
        {
          id: "state:close",
          label: "Close",
          paletteLabel: `Close ${urls.length} Pull Requests`,
          icon: XIcon,
          run: () => close(urls),
        },
        {
          id: "state:reopen",
          label: "Reopen",
          paletteLabel: `Reopen ${urls.length} Pull Requests`,
          icon: XIcon,
          unavailableReason: "None of these is closed.",
          run: close,
        },
      ];
    };
    renderList({ managesSessionLinks: false, selectionActions });
    fireEvent.click(within(rowNamed("Retry uploads")).getByRole("checkbox"));
    fireEvent.click(within(rowNamed("Try a new layout")).getByRole("checkbox"));

    // A row outside the selection keeps its own menu.
    fireEvent.contextMenu(rowNamed("Old idea"));
    let menu = await screen.findByRole("menu");
    expect(within(menu).queryByRole("menuitem", { name: "Close 2 Pull Requests" })).toBeNull();
    await user.keyboard("{Escape}");

    fireEvent.contextMenu(rowNamed("Try a new layout"));
    menu = await screen.findByRole("menu");
    expect(within(menu).getByRole("menuitem", { name: /Reopen 2 Pull Requests/ })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    await user.click(within(menu).getByRole("menuitem", { name: "Close 2 Pull Requests" }));
    expect(close).toHaveBeenCalledWith([
      "https://github.com/acme/app/pull/1",
      "https://github.com/acme/app/pull/3",
    ]);
  });

  it("acts only on selected rows still shown: a search, a status chip or a collapsed group drops the rest", async () => {
    const user = userEvent.setup();
    const seen: (readonly number[])[] = [];
    const selectionActions = (selected: readonly PullRequestListEntry[]): ListSelectionAction[] => {
      seen.push(selected.map((entry) => entry.row.number));
      return [
        { id: "approve", label: "Approve…", paletteLabel: "Approve", icon: CheckIcon, run() {} },
      ];
    };
    renderList({ managesSessionLinks: false, selectionActions });
    const select = (title: string) =>
      fireEvent.click(within(rowNamed(title)).getByRole("checkbox"));
    const bar = () => screen.getByRole("toolbar", { name: "Selected pull requests" });
    select("Retry uploads");
    select("Try a new layout");
    select("Ship docs");
    select("Old idea");
    expect(seen.at(-1)).toEqual([1, 3, 2, 4]);

    // A search hiding a selected row takes it out, and clearing the search doesn't bring it back.
    const search = screen.getByRole("searchbox", { name: "Filter pull requests" });
    fireEvent.change(search, { target: { value: "Retry uploads" } });
    expect(seen.at(-1)).toEqual([1]);
    expect(within(bar()).getByText("1")).toBeInTheDocument();
    fireEvent.change(search, { target: { value: "" } });
    expect(seen.at(-1)).toEqual([1]);

    // So does a status chip, and collapsing a group.
    select("Ship docs");
    select("Old idea");
    expect(seen.at(-1)).toEqual([1, 2, 4]);
    await user.click(screen.getByRole("button", { name: "Merged group (1)" }));
    expect(seen.at(-1)).toEqual([1, 4]);
    await user.click(
      within(screen.getByRole("navigation", { name: "Status" })).getByRole("button", {
        name: "Open (2)",
      }),
    );
    expect(seen.at(-1)).toEqual([1]);
    expect(within(bar()).getByText("1")).toBeInTheDocument();
  });

  it("clears the selection when its scope changes, as the page reads as another account", () => {
    let scope = "alice";
    const selectionActions = () => [];
    const client = new QueryClient();
    const tree = () => (
      <QueryClientProvider client={client}>
        <PullRequestListPage
          conversationId="conv_1"
          active
          associations={[]}
          hostOffline={false}
          sandboxAsleep={false}
          onSelect={vi.fn()}
          unlinkPending={false}
          managesSessionLinks={false}
          selectionActions={selectionActions}
          selectionScope={scope}
        />
      </QueryClientProvider>
    );
    const view = render(tree());
    fireEvent.click(within(rowNamed("Retry uploads")).getByRole("checkbox"));
    expect(screen.getByRole("toolbar", { name: "Selected pull requests" })).toBeInTheDocument();
    scope = "bob";
    view.rerender(tree());
    expect(screen.queryByRole("toolbar", { name: "Selected pull requests" })).toBeNull();
  });

  it("selects and names its rows as the session's in a session's list", () => {
    fixture.rows[2] = row({ number: 3, title: "Try a new layout", inferred: true });
    const { grid } = renderList();
    expect(grid).toHaveAttribute("aria-multiselectable", "true");
    expect(rowNamed("Retry uploads")).toHaveAttribute("aria-selected", "false");
    expect(screen.getByRole("checkbox", { name: "Select All" })).toBeInTheDocument();
    expect(within(rowNamed("Try a new layout")).getByText("From branch")).toHaveAttribute(
      "title",
      "Found from the session’s branch",
    );
  });

  it("opens the active row with Enter and walks rows with j and k", async () => {
    const user = userEvent.setup();
    const { onSelect, grid } = renderList();
    // Focusing the list makes its first row active.
    act(() => grid.focus());
    expect(rowNamed("Retry uploads")).toHaveClass("active");
    await user.keyboard("j");
    expect(rowNamed("Try a new layout")).toHaveClass("active");
    await user.keyboard("k{Enter}");
    expect(onSelect).toHaveBeenLastCalledWith("https://github.com/acme/app/pull/1", "here");
  });

  it("filters from / and clears the filter, then leaves the field, with Escape", async () => {
    const user = userEvent.setup();
    const { grid } = renderList();
    act(() => grid.focus());
    await user.keyboard("/");
    const filter = screen.getByRole("searchbox", { name: "Filter pull requests" });
    expect(filter).toHaveFocus();
    await user.keyboard("docs");
    expect(screen.queryByText("Retry uploads")).toBeNull();
    expect(screen.getByText("Ship docs")).toBeVisible();

    await user.keyboard("{Escape}");
    expect(filter).toHaveValue("");
    await user.keyboard("{Escape}");
    expect(filter).not.toHaveFocus();

    await user.type(filter, "nothing-matches");
    expect(screen.getByText("No pull requests match")).toBeVisible();
    // The controls stay, so the filter that hides every row can be changed or cleared.
    expect(filter).toBeEnabled();
    expect(screen.getByRole("navigation", { name: "Status" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Display Options" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Clear Filters" }));
    expect(screen.getByText("Retry uploads")).toBeVisible();
  });

  it("keeps a collapsed group's header when filtering down to that group alone", async () => {
    const user = userEvent.setup();
    renderList();
    await user.click(screen.getByRole("button", { name: "Open group (2)" }));
    expect(screen.queryByText("Retry uploads")).toBeNull();

    await user.click(
      within(screen.getByRole("navigation", { name: "Status" })).getByRole("button", {
        name: "Open (2)",
      }),
    );
    const header = screen.getByRole("button", { name: "Open group (2)" });
    expect(header).toHaveAttribute("aria-expanded", "false");
    await user.click(header);
    expect(screen.getByText("Retry uploads")).toBeVisible();
  });

  it("removes a finished group together", async () => {
    const user = userEvent.setup();
    const { onUnlink } = renderList();
    const merged = screen.getByRole("button", { name: "Merged group (1)" }).parentElement!;
    await user.click(within(merged).getByRole("button", { name: "Remove All" }));
    expect(onUnlink).toHaveBeenCalledWith(["https://github.com/acme/app/pull/2"]);
  });

  it("can't remove without edit access, and says why", async () => {
    fixture.canUnlink = false;
    const user = userEvent.setup();
    renderList({ onUnlink: undefined });
    expect(screen.queryByRole("button", { name: "Remove All" })).toBeNull();
    await user.click(within(rowNamed("Retry uploads")).getByRole("checkbox"));
    const remove = within(
      screen.getByRole("toolbar", { name: "Selected pull requests" }),
    ).getByRole("button", { name: /^Remove/ });
    // It stays reachable from the keyboard, which reads why it can't remove.
    expect(remove).toHaveAttribute("aria-disabled", "true");
    expect(remove).toHaveAccessibleDescription(
      "You need edit access to remove pull requests from this session.",
    );
    expect(remove).toHaveAttribute(
      "title",
      "You need edit access to remove pull requests from this session.",
    );
  });

  it("hides Diff and Updated where no desktop shell reads them, but still shows reviewers", () => {
    fixture.stats = "unsupported";
    fixture.statsColumns = false;
    fixture.rows = [
      row({ number: 1, stats: undefined, reviewers: [reviewer("grace", "APPROVED")] }),
    ];
    const { grid } = renderList();
    expect(grid).toHaveClass("no-stats");
    expect(within(grid).getByRole("img", { name: "grace · Approved" })).toBeInTheDocument();
  });

  it("keeps Diff and Updated on desktop and says why a row has none", async () => {
    fixture.stats = "failed";
    const failed = row({ number: 1, title: "Signed out", stats: undefined });
    fixture.rows = [failed, row({ number: 2, title: "Still loading", stats: undefined })];
    fixture.statsGaps = new Map([
      [
        failed.url,
        {
          kind: "read-failed",
          service: "github",
          code: "account_missing",
          message: "That GitHub account isn't signed in on this Mac. Run gh auth login.",
        },
      ],
    ]);
    const { grid } = renderList();
    expect(grid).not.toHaveClass("no-stats");
    const diffGap = within(rowNamed("Signed out")).getByRole("img", {
      name: "Diff size unavailable. That GitHub account isn't signed in on this Mac. Run gh auth login.",
    });
    expect(diffGap).toHaveTextContent("—");
    expect(
      within(rowNamed("Signed out")).getByRole("img", { name: /^Update time unavailable\./ }),
    ).toBeInTheDocument();
    // A row whose read may still answer holds its place without a dash.
    expect(
      within(rowNamed("Still loading")).queryByRole("img", { name: /unavailable/ }),
    ).toBeNull();
    // The reason is reachable from the keyboard, not only on hover.
    act(() => diffGap.focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent(
      "Diff size unavailable. That GitHub account isn't signed in on this Mac.",
    );
  });

  it("keeps the chosen order when Diff or Updated can't sort yet, and sorts once stats arrive", async () => {
    const user = userEvent.setup();
    fixture.stats = "failed";
    const view = renderList();
    const sortedBy = () =>
      within(view.grid)
        .getAllByRole("columnheader")
        .find((header) => header.getAttribute("aria-sort"))?.textContent;
    await user.click(screen.getByRole("button", { name: "Title" }));
    expect(sortedBy()).toBe("Title");
    const tryToSort = async (name: string) => {
      const header = screen.getByRole("button", { name });
      expect(header).toHaveAttribute("aria-disabled", "true");
      expect(header).toHaveAccessibleDescription(
        "Sorting needs diff sizes and update times, which aren't available yet.",
      );
      await user.click(header);
      expect(sortedBy()).toBe("Title");
    };
    await tryToSort("Updated");
    await tryToSort("Diff");
    fixture.stats = "ready";
    view.rerender();
    const updated = screen.getByRole("button", { name: "Updated" });
    expect(updated).not.toHaveAttribute("aria-disabled");
    await user.click(updated);
    expect(sortedBy()).toBe("Updated");
  });

  it("shows a partial answer's value beside the field it lacks, and zero as a value", () => {
    fixture.rows = [
      row({
        number: 1,
        title: "No time",
        stats: { ...row().stats!, additions: 0, deletions: 0, updatedAt: undefined },
      }),
    ];
    renderList();
    const partial = rowNamed("No time");
    expect(within(partial).getByText("+0")).toBeInTheDocument();
    expect(
      within(partial).getByRole("img", {
        name: "Update time unavailable. GitHub didn't report it.",
      }),
    ).toBeInTheDocument();
  });

  it("shows initials where the host has no public avatars", () => {
    fixture.host = "github.example.com";
    fixture.rows = [row({ number: 1, author: "ada-lovelace" })];
    renderList();
    expect(within(rowNamed("Change 1")).getByText("AL")).toBeInTheDocument();
  });

  it("sorts from a column header and flips the direction on a second click", async () => {
    const user = userEvent.setup();
    renderList({});
    const titleHeader = screen.getByRole("columnheader", { name: "Title" });
    await user.click(within(titleHeader).getByRole("button"));
    expect(titleHeader).toHaveAttribute("aria-sort", "ascending");
    await user.click(within(titleHeader).getByRole("button"));
    expect(titleHeader).toHaveAttribute("aria-sort", "descending");
  });

  it("sorts by Status from its glyph header, by name and keyboard, in both directions", async () => {
    const user = userEvent.setup();
    renderList({});
    const header = screen.getByRole("columnheader", { name: "Status" });
    const button = within(header).getByRole("button", { name: "Status" });
    expect(button).not.toHaveTextContent("Status");
    // Activating closes the tooltip; focusing again tells the new order.
    const tooltipOnFocus = async () => {
      act(() => button.blur());
      act(() => button.focus());
      return screen.findByRole("tooltip");
    };
    await user.click(screen.getByRole("button", { name: "Title" }));
    expect(await tooltipOnFocus()).toHaveTextContent("Sort by status");
    await user.keyboard("{Enter}");
    expect(header).toHaveAttribute("aria-sort", "ascending");
    expect(await tooltipOnFocus()).toHaveTextContent("Sorted by status, ascending");
    await user.keyboard("{Enter}");
    expect(header).toHaveAttribute("aria-sort", "descending");
    expect(await tooltipOnFocus()).toHaveTextContent("Sorted by status, descending");
  });

  it("shows each status as one named glyph, and leaves the state glyph to say the rest", async () => {
    const user = userEvent.setup();
    fixture.rows = [
      row({ number: 1, title: "Asks for review", review: "review_requested" }),
      row({
        number: 2,
        title: "Unreadable",
        mergeReadinessView: { kind: "unknown", failure: "service" },
      }),
      row({ number: 3, title: "Plain open" }),
      row({ number: 4, title: "Sketch only", state: "draft" }),
    ];
    renderList({});
    const asks = within(rowNamed("Asks for review")).getByRole("img", {
      name: "Review Requested",
    });
    expect(asks).toHaveAttribute("data-status-meaning", "review-requested");
    expect(asks.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    expect(
      within(rowNamed("Unreadable")).getByRole("img", {
        name: "Merge status unknown. Couldn't reach the merge status service. The app will try again.",
      }),
    ).toBeVisible();

    // Focus shows the same words as the name, and Escape puts the tooltip away.
    asks.focus();
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Review Requested");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).toBeNull();

    // The state glyph already says Open and Draft; their cells add nothing, not even a tab stop.
    for (const quiet of [fixture.rows[2]!, fixture.rows[3]!]) {
      expect(rowFor(quiet.url).querySelector(".pr-list-cell-status")).toBeEmptyDOMElement();
    }
    expect(within(rowFor(fixture.rows[3]!.url)).getByRole("img", { name: "Draft" })).toBeVisible();
  });

  it("still filters by state once statuses are glyphs", async () => {
    const user = userEvent.setup();
    fixture.rows = [
      row({ number: 1, title: "Waiting", review: "review_requested" }),
      row({ number: 2, title: "Landed", state: "merged" }),
    ];
    const view = renderList({});
    const chips = within(screen.getByRole("navigation", { name: "Status" }));
    await user.click(chips.getByRole("button", { name: "Merged (1)" }));
    const titles = within(view.grid)
      .getAllByRole("row")
      .flatMap((element) => element.getAttribute("aria-label") ?? []);
    expect(titles.some((title) => title.includes("Landed"))).toBe(true);
    expect(titles.some((title) => title.includes("Waiting"))).toBe(false);
  });

  it("shows and reverses the order it applies while stats are unavailable, then follows them", async () => {
    const user = userEvent.setup();
    fixture.stats = "unsupported";
    const view = renderList();
    const titles = () =>
      within(view.grid)
        .getAllByRole("row")
        .flatMap((element) => element.getAttribute("aria-label") ?? []);
    // Last updated waits for stats, so the list keeps status order and says so.
    expect(screen.getByRole("columnheader", { name: "Status" })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
    await user.click(screen.getByRole("button", { name: "Display Options" }));
    expect(screen.getByRole("combobox", { name: "Order by" })).toHaveTextContent("Status");
    const direction = screen.getByRole("button", { name: "Ascending" });
    const before = titles();
    await user.click(direction);
    expect(screen.getByRole("button", { name: "Descending" })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "Status" })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
    expect(titles()).not.toEqual(before);

    // The reversed status order is a choice now, kept once stats arrive.
    fixture.stats = "ready";
    view.rerender();
    expect(screen.getByRole("columnheader", { name: "Status" })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
  });

  it("keeps a search's own order, ungrouped, until the user picks another display", async () => {
    const user = userEvent.setup();
    // The search lists the least recently updated first: the opposite of Last updated.
    fixture.rows = [
      row({ number: 3, title: "Third" }),
      row({ number: 1, title: "First" }),
      row({ number: 2, title: "Second", state: "merged" }),
    ];
    const view = renderList({ searchOrdered: true });
    const numbers = () =>
      within(view.grid)
        .getAllByRole("row")
        .flatMap((element) => element.getAttribute("data-pull-request-row") ?? [])
        .map((url) => Number(url.split("/").pop()));
    expect(numbers()).toEqual([3, 1, 2]);
    // No status groups split the search's order.
    expect(within(view.grid).queryByRole("button", { name: /^(Open|Merged) group/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Display Options" }));
    expect(screen.getByRole("combobox", { name: "Order by" })).toHaveTextContent("Search order");
    // Nothing to reset: the search's order is this list's default.
    expect(screen.queryByRole("button", { name: "Reset to Default" })).toBeNull();
    await user.keyboard("{Escape}");
    // Picking Last updated orders by it, still ungrouped as the list showed.
    await user.click(screen.getByRole("button", { name: "Updated" }));
    expect(numbers()).toEqual([1, 2, 3]);
    expect(paletteCommand("Reset pull request display options")).toBeDefined();
  });

  it("keeps Group by Status then Order by Last updated on a search-ordered list", () => {
    fixture.rows = [
      row({ number: 3, title: "Third" }),
      row({ number: 1, title: "First" }),
      row({ number: 2, title: "Second", state: "merged" }),
    ];
    const view = renderList({ searchOrdered: true });
    const numbers = () =>
      within(view.grid)
        .getAllByRole("row")
        .flatMap((element) => element.getAttribute("data-pull-request-row") ?? [])
        .map((url) => Number(url.split("/").pop()));
    const choose = (label: string, value: string) => {
      const setting = paletteCommand(label)?.setting;
      act(() => {
        void setting?.choose(value);
      });
    };
    expect(numbers()).toEqual([3, 1, 2]);
    // Together the two picks are the default's values; they stay picked all the same.
    choose("Group pull requests by", "status");
    choose("Order pull requests by", "updated");
    expect(numbers()).toEqual([1, 3, 2]);
    expect(paletteCommand("Reset pull request display options")).toBeDefined();
    // Reset goes back to the search's own order.
    act(() => paletteCommand("Reset pull request display options")?.run());
    expect(numbers()).toEqual([3, 1, 2]);
    expect(paletteCommand("Reset pull request display options")).toBeUndefined();
  });

  it("never shows a search's order on a list whose rows come in none", () => {
    renderList();
    const choices = paletteCommand("Order pull requests by")?.setting?.choices ?? [];
    expect(choices.map((choice) => choice.label)).toContain("Last updated");
    expect(choices.map((choice) => choice.label)).not.toContain("Search order");
  });

  it("follows Last updated once stats arrive, when the order was never changed", () => {
    fixture.stats = "unsupported";
    const view = renderList();
    expect(screen.getByRole("columnheader", { name: "Status" })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
    fixture.stats = "ready";
    view.rerender();
    expect(screen.getByRole("columnheader", { name: "Updated" })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
  });

  it("names the active row for assistive tech as it moves, and drops it once it's hidden", async () => {
    const user = userEvent.setup();
    const { grid } = renderList();
    act(() => grid.focus());
    const activeName = () => {
      const id = grid.getAttribute("aria-activedescendant");
      return id ? document.getElementById(id)?.getAttribute("aria-label") : undefined;
    };
    expect(activeName()).toMatch(/Retry uploads$/);
    await user.keyboard("j");
    expect(activeName()).toMatch(/Try a new layout$/);

    // Filtering the active row away leaves no active row; the next key starts from the top.
    await user.click(screen.getByRole("button", { name: "Merged (1)" }));
    expect(grid).not.toHaveAttribute("aria-activedescendant");
    act(() => grid.focus());
    await user.keyboard("j");
    expect(activeName()).toMatch(/Ship docs$/);

    // Collapsing its group hides it the same way.
    await user.click(screen.getByRole("button", { name: "Merged group (1)" }));
    expect(grid).not.toHaveAttribute("aria-activedescendant");
  });

  it("drops the active row when it's removed from the list", async () => {
    const user = userEvent.setup();
    const view = renderList();
    act(() => view.grid.focus());
    expect(view.grid).toHaveAttribute("aria-activedescendant");
    fixture.rows = fixture.rows.filter((value) => value.number !== 1);
    view.rerender();
    expect(view.grid).not.toHaveAttribute("aria-activedescendant");
    await user.keyboard("j");
    expect(view.grid).toHaveAttribute("aria-activedescendant");
  });

  it("leaves a selection with its session, while each session keeps its own filter", async () => {
    const user = userEvent.setup();
    const view = renderList();
    const chips = () => within(screen.getByRole("navigation", { name: "Status" }));
    await user.click(chips().getByRole("button", { name: "Open (2)" }));
    await user.click(within(rowNamed("Retry uploads")).getByRole("checkbox"));
    expect(screen.getByRole("toolbar", { name: "Selected pull requests" })).toBeVisible();

    // Another session listing the same pull request starts with nothing selected or active.
    view.switchTo("conv_2");
    expect(screen.queryByRole("toolbar", { name: "Selected pull requests" })).toBeNull();
    expect(view.grid).not.toHaveAttribute("aria-activedescendant");
    expect(chips().getByRole("button", { name: "All (4)" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    act(() => view.grid.focus());
    await user.keyboard("{Meta>}{Backspace}{/Meta}");
    expect(view.onUnlink).not.toHaveBeenCalled();

    // Back in the first session, its chip is as it was.
    view.switchTo("conv_1");
    expect(chips().getByRole("button", { name: "Open (2)" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.queryByRole("toolbar", { name: "Selected pull requests" })).toBeNull();
  });

  it("offers the list's actions in ⌘K", async () => {
    const user = userEvent.setup();
    renderList();
    await user.click(within(rowNamed("Retry uploads")).getByRole("checkbox"));
    const labels = Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) =>
      group.commands.map((command) => command.label),
    );
    expect(labels).toEqual(
      expect.arrayContaining([
        "Filter pull requests",
        "Remove 1 selected pull request from this session",
        "Clear pull request selection",
        "Select all pull requests (4)",
        "Order pull requests ascending",
        "Collapse the Open group",
        "Remove 1 merged pull request from this session",
        "Remove 1 closed pull request from this session",
      ]),
    );
  });

  it("runs the same group actions from ⌘K as from the headers", () => {
    const { onUnlink } = renderList();
    const command = (label: string) =>
      Object.values(usePaletteCommandsStore.getState().groups)
        .flatMap((group) => group.commands)
        .find((candidate) => candidate.label === label)!;
    act(() => command("Collapse the Open group").run());
    expect(screen.getByRole("button", { name: "Open group (2)" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(command("Expand the Open group")).toBeDefined();
    act(() => command("Remove 1 merged pull request from this session").run());
    expect(onUnlink).toHaveBeenCalledWith(["https://github.com/acme/app/pull/2"]);
    act(() => command("Order pull requests ascending").run());
    expect(screen.getByRole("columnheader", { name: "Updated" })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
  });
});

describe("PullRequestListPage's PRs for the page's other menus", () => {
  it("offers its rows' menus while it's mounted and shown, and none once it's gone", async () => {
    const owners = createPullRequestEntryOwners();
    const url = fixture.rows[0]!.url;
    const view = render(
      <QueryClientProvider client={new QueryClient()}>
        <PullRequestListPage
          conversationId="conv_1"
          active
          associations={[]}
          hostOffline={false}
          sandboxAsleep={false}
          onSelect={vi.fn()}
          unlinkPending={false}
          entryOwners={owners}
        />
      </QueryClientProvider>,
    );
    fireEvent.contextMenu(rowNamed("Retry uploads"));
    const shown = within(await screen.findByRole("menu"))
      .getAllByRole("menuitem")
      .map((item) => item.textContent ?? "");
    await userEvent.keyboard("{Escape}");
    // The same items its own right-click shows, for the PR however its URL is written.
    const labels = owners.menuFor(url.replace("acme/app", "ACME/App"))?.map((a) => a.label) ?? [];
    expect(labels).toHaveLength(shown.length);
    shown.forEach((text, index) => expect(text).toContain(labels[index]));
    view.unmount();
    expect(owners.menuFor(url)).toBeNull();
    expect(owners.tableKeys()).toBe("");
  });

  it("offers none while the list isn't confirmed", () => {
    const owners = createPullRequestEntryOwners();
    renderList({ active: false, entryOwners: owners });
    expect(owners.menuFor(fixture.rows[0]!.url)).toBeNull();
  });
});

describe("PullRequestListPage's repository filters", () => {
  const exclusion = (hidden: string[] = []) => ({
    hidden,
    toggle: vi.fn(),
    clear: vi.fn(),
  });

  beforeEach(() => {
    fixture.rows = [
      row({ number: 1, title: "Retry uploads" }),
      row({ number: 2, title: "Ship docs", repository: "acme/docs" }),
      row({ number: 3, title: "Try a new layout" }),
    ];
  });

  it("narrows to a repository picked in the Filter menu, shown as a chip until it's removed", async () => {
    const user = userEvent.setup();
    renderList({ filtersByRepository: true });
    await user.click(screen.getByRole("button", { name: "Add Filter" }));
    await user.type(await screen.findByRole("searchbox", { name: "Filter by" }), "docs");
    await user.click(screen.getByRole("menuitemcheckbox", { name: /acme\/docs/ }));
    await user.keyboard("{Escape}");
    expect(screen.queryByText("Retry uploads")).toBeNull();
    expect(screen.getByText("Ship docs")).toBeVisible();
    const chips = screen.getByRole("group", { name: "Active filters" });
    expect(within(chips).getByRole("button", { name: "Repository is acme/docs" })).toBeVisible();
    // The chips share the state tabs' row, right after them.
    expect(screen.getByRole("navigation", { name: "Status" }).nextElementSibling).toBe(chips);
    await user.click(within(chips).getByRole("button", { name: "Remove Repository Filter" }));
    expect(screen.getByText("Retry uploads")).toBeVisible();
    expect(screen.queryByRole("group", { name: "Active filters" })).toBeNull();
  });

  it("keeps a picked repository's chip when its rows go, and clears it from the empty view", async () => {
    const user = userEvent.setup();
    const list = renderList({ filtersByRepository: true });
    await user.click(screen.getByRole("button", { name: "Add Filter" }));
    await user.type(await screen.findByRole("searchbox", { name: "Filter by" }), "docs");
    await user.click(screen.getByRole("menuitemcheckbox", { name: /acme\/docs/ }));
    await user.keyboard("{Escape}");
    // Its only PR leaves the list, say after a refresh: the pick stays in reach.
    fixture.rows = fixture.rows.filter((candidate) => candidate.repository !== "acme/docs");
    list.rerender();
    expect(screen.getByRole("button", { name: "Repository is acme/docs" })).toBeVisible();
    expect(screen.getByText("No pull requests match")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Clear Filters" }));
    expect(screen.getByText("Retry uploads")).toBeVisible();
    expect(screen.queryByRole("group", { name: "Active filters" })).toBeNull();
  });

  it("clears a picked repository with Escape, after the status chip", async () => {
    const user = userEvent.setup();
    const list = renderList({ filtersByRepository: true });
    await user.click(screen.getByRole("button", { name: "Add Filter" }));
    await user.type(await screen.findByRole("searchbox", { name: "Filter by" }), "docs");
    await user.click(screen.getByRole("menuitemcheckbox", { name: /acme\/docs/ }));
    await user.keyboard("{Escape}");
    list.grid.focus();
    await user.keyboard("{Escape}");
    expect(screen.getByText("Retry uploads")).toBeVisible();
  });

  it("opens the Filter menu from F in the list", async () => {
    const user = userEvent.setup();
    const list = renderList({ filtersByRepository: true });
    list.grid.focus();
    await user.keyboard("f");
    expect(await screen.findByRole("searchbox", { name: "Filter by" })).toBeVisible();
  });

  it("hides and shows repositories through the owner, listing the hidden ones checked first", async () => {
    const user = userEvent.setup();
    const hiding = exclusion(["acme/noise"]);
    renderList({ repositoryExclusion: hiding });
    await user.click(screen.getByRole("button", { name: "Add Filter" }));
    await screen.findByRole("searchbox", { name: "Filter by" });
    // Without the Repository filter, Exclude Repository is the only category.
    expect(screen.queryByRole("menuitem", { name: /^Repository/ })).toBeNull();
    await user.keyboard("{ArrowDown}{ArrowRight}");
    const options = await screen.findAllByRole("menuitemcheckbox");
    expect(
      options.map((option) => [option.textContent, option.getAttribute("aria-checked")]),
    ).toEqual([
      ["acme/noise", "true"],
      ["acme/app2", "false"],
      ["acme/docs1", "false"],
    ]);
    await user.click(options[2]!);
    expect(hiding.toggle).toHaveBeenCalledWith("acme/docs");
  });

  it("keeps the filters in reach over an empty state while repositories are hidden", async () => {
    const user = userEvent.setup();
    const hiding = exclusion(["acme/noise"]);
    renderList({ repositoryExclusion: hiding, emptyState: <p>No reviews waiting on you</p> });
    expect(screen.getByText("No reviews waiting on you")).toBeVisible();
    const chip = screen.getByRole("button", { name: "Repository is not acme/noise" });
    await user.click(screen.getByRole("button", { name: "Show All Hidden Repositories" }));
    expect(hiding.clear).toHaveBeenCalled();
    await user.click(chip);
    expect(await screen.findByRole("menuitemcheckbox", { name: /acme\/noise/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("opens nothing when the owner's hides arrive after the list", async () => {
    const props: Partial<PullRequestListProps> = {};
    const list = renderList(props);
    props.repositoryExclusion = { ...exclusion(["acme/noise"]), menuRequest: 0 };
    list.rerender();
    expect(screen.getByRole("button", { name: "Repository is not acme/noise" })).toBeVisible();
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("opens the hidden repositories' chip when its owner asks", async () => {
    const hiding = exclusion(["acme/noise", "acme/old"]);
    const props: Partial<PullRequestListProps> = {
      repositoryExclusion: { ...hiding, menuRequest: 0 },
    };
    const list = renderList(props);
    expect(
      screen.getByRole("button", { name: "Repository is none of 2 repositories" }),
    ).toBeVisible();
    props.repositoryExclusion = { ...hiding, menuRequest: 1 };
    list.rerender();
    expect(await screen.findByRole("menuitemcheckbox", { name: /acme\/old/ })).toBeVisible();
  });

  it("offers a group header's actions in its ellipsis and context menus", async () => {
    const user = userEvent.setup();
    const run = vi.fn();
    const groupActions = vi.fn(() => [
      { id: "page:hide-repository", label: "Hide Repository", icon: XIcon, run },
    ]);
    fixture.rows = [...fixture.rows, row({ number: 4, title: "Old idea", state: "closed" })];
    renderList({ groupActions });
    await user.click(screen.getByRole("button", { name: "More actions for Open" }));
    await user.click(await screen.findByRole("menuitem", { name: "Hide Repository" }));
    expect(run).toHaveBeenCalledTimes(1);
    expect(groupActions).toHaveBeenCalledWith(
      expect.objectContaining({ grouping: "status", id: "open", title: "Open", state: "open" }),
    );
    fireEvent.contextMenu(screen.getByRole("button", { name: /^Open group/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Hide Repository" }));
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("selects a group's shown rows from its ⋯, its context menu and ⌘K, keeping rows checked elsewhere", async () => {
    const user = userEvent.setup();
    // A second group, so the list shows its headers.
    fixture.rows = [
      ...fixture.rows,
      row({ number: 5, title: "Sketch the importer", state: "draft" }),
    ];
    renderList({ managesSessionLinks: false, selectionActions: () => [] });
    fireEvent.click(within(rowNamed("Sketch the importer")).getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "More actions for Open" }));
    await user.click(await screen.findByRole("menuitem", { name: "Select All in Group" }));
    expect(selectedCount()).toBe("4 selected");
    fireEvent.contextMenu(screen.getByRole("button", { name: /^Open group/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Deselect All in Group" }));
    expect(selectedCount()).toBe("1 selected");
    act(() => paletteCommand("Select All Pull Requests in Open (3)")!.run());
    expect(selectedCount()).toBe("4 selected");
  });

  it("selects only what a search leaves shown, even from a ⌘K row listed before it", () => {
    // A second group, so the list shows its headers.
    fixture.rows = [
      ...fixture.rows,
      row({ number: 5, title: "Sketch the importer", state: "draft" }),
    ];
    renderList({ managesSessionLinks: false, selectionActions: () => [] });
    const listed = paletteCommand("Select All Pull Requests in Open (3)")!;
    fireEvent.change(screen.getByPlaceholderText("Filter by title, number, author, or branch"), {
      target: { value: "layout" },
    });
    act(() => listed.run());
    expect(selectedCount()).toBe("1 selected");
    expect(within(rowNamed("Try a new layout")).getByRole("checkbox")).toBeChecked();
  });

  it("never lets a header action listed under one grouping act on a same-named group of another", async () => {
    const user = userEvent.setup();
    // An author named "open" has a group id the Open status group had.
    fixture.rows = [
      row({ number: 1, title: "Retry uploads", author: "alice" }),
      row({ number: 5, title: "Sketch the importer", state: "draft", author: "open" }),
    ];
    const reached: (readonly PullRequestListEntry[] | null)[] = [];
    renderList({
      managesSessionLinks: false,
      selectionActions: () => [],
      groupActions: (target) =>
        target.id === "open"
          ? [
              {
                id: "probe",
                label: "Probe",
                paletteLabel: `Probe ${target.grouping}`,
                icon: XIcon,
                onRows: true,
                run: () => reached.push(target.shownEntries()),
              },
            ]
          : [],
    });
    const select = paletteCommand("Select All Pull Requests in Open (1)")!;
    const probe = paletteCommand("Probe status")!;
    await user.click(screen.getByRole("button", { name: "Display Options" }));
    await user.click(screen.getByRole("combobox", { name: "Group by" }));
    await user.click(await screen.findByRole("option", { name: /^Author$/ }));
    expect(screen.getByRole("button", { name: "open group (1)" })).toBeVisible();
    act(() => select.run());
    act(() => probe.run());
    expect(selectedCount()).toBeNull();
    expect(reached).toEqual([null]);
  });

  it("holds Select All in a collapsed group, saying why where the keyboard reaches it", async () => {
    const user = userEvent.setup();
    // A second group, so the list shows its headers.
    fixture.rows = [
      ...fixture.rows,
      row({ number: 5, title: "Sketch the importer", state: "draft" }),
    ];
    renderList({ managesSessionLinks: false, selectionActions: () => [] });
    await user.click(screen.getByRole("button", { name: /^Open group/ }));
    await user.click(screen.getByRole("button", { name: "More actions for Open" }));
    const item = await screen.findByRole("menuitem", { name: "Select All in Group" });
    expect(item).toHaveAttribute("aria-disabled", "true");
    expect(item).toHaveAccessibleDescription("Expand the group first.");
    expect(paletteCommand("Select All Pull Requests in Open (3)")?.availability).toMatchObject({
      reason: "Expand the group first.",
    });
  });

  it("offers a repository header's links on its right-click, ⋯ and ⌘K, naming each host where one name sits on two", async () => {
    const user = userEvent.setup();
    fixture.hostsFromUrls = true;
    fixture.rows = [
      row({ number: 1, title: "Retry uploads" }),
      row({
        number: 3,
        title: "Try a new layout",
        url: "https://github.example.com/acme/app/pull/3",
      }),
      row({ number: 5, title: "Write docs", repository: "acme/docs" }),
    ];
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    renderList(
      { managesSessionLinks: false, selectionActions: () => [] },
      { grouping: "repository" },
    );
    const linkLabels = [
      "Open acme/app on github.com",
      "Open acme/app on github.example.com",
      "Copy Repository Name",
      "Copy Link to acme/app on github.com",
      "Copy Link to acme/app on github.example.com",
    ];
    await user.click(screen.getByRole("button", { name: "More actions for acme/app" }));
    const more = await screen.findByRole("menu");
    expect(
      within(more)
        .getAllByRole("menuitem")
        .map((item) => item.textContent),
    ).toEqual(["Select All in Group", ...linkLabels]);
    await user.click(
      within(more).getByRole("menuitem", { name: "Open acme/app on github.example.com" }),
    );
    expect(open).toHaveBeenLastCalledWith(
      "https://github.example.com/acme/app",
      "_blank",
      "noopener,noreferrer",
    );
    fireEvent.contextMenu(screen.getByRole("button", { name: /^acme\/app group/ }));
    const menu = await screen.findByRole("menu");
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((item) => item.textContent),
    ).toEqual(["Select All in Group", ...linkLabels]);
    await user.click(
      within(menu).getByRole("menuitem", { name: "Copy Link to acme/app on github.example.com" }),
    );
    await vi.waitFor(() =>
      expect(clipboard.copied).toEqual(["https://github.example.com/acme/app"]),
    );
    // One host: the header says GitHub, in ⌘K too.
    expect(paletteCommand("Open acme/docs on GitHub")).toBeDefined();
    expect(paletteCommand("Copy Link to acme/docs on GitHub")).toBeDefined();
    expect(paletteCommand("Copy Repository Name of acme/docs")).toBeDefined();
    expect(paletteCommand("Open acme/app on github.example.com")).toBeDefined();
  });

  it("offers no repository items on Status or Author headers, the unknown author's included", async () => {
    const user = userEvent.setup();
    fixture.rows = [
      row({ number: 1, title: "Retry uploads", author: "alice" }),
      row({ number: 3, title: "Try a new layout", state: "draft", author: null }),
    ];
    const headerItems = async (title: string) => {
      await user.click(screen.getByRole("button", { name: `More actions for ${title}` }));
      const items = within(await screen.findByRole("menu"))
        .getAllByRole("menuitem")
        .map((item) => item.textContent);
      await user.keyboard("{Escape}");
      return items;
    };
    const repositoryCommands = () =>
      Object.values(usePaletteCommandsStore.getState().groups)
        .flatMap((group) => group.commands)
        .filter((command) => /Repository|Link to/.test(command.label));
    renderList({ managesSessionLinks: false, selectionActions: () => [] });
    expect(await headerItems("Open")).toEqual(["Select All in Group"]);
    expect(await headerItems("Draft")).toEqual(["Select All in Group"]);
    expect(repositoryCommands()).toEqual([]);
    cleanup();

    renderList({ managesSessionLinks: false, selectionActions: () => [] }, { grouping: "author" });
    expect(await headerItems("alice")).toEqual(["Select All in Group"]);
    expect(await headerItems("Unknown author")).toEqual(["Select All in Group"]);
    expect(repositoryCommands()).toEqual([]);
  });

  it("filters to an author from their header, apart from the same login on another host", async () => {
    const user = userEvent.setup();
    fixture.hostsFromUrls = true;
    fixture.rows = [
      row({ number: 1, title: "Retry uploads", author: "alice" }),
      row({
        number: 3,
        title: "Try a new layout",
        author: "alice",
        url: "https://github.example.com/acme/app/pull/3",
      }),
      row({ number: 5, title: "Write docs", author: "github-actions" }),
    ];
    renderList(
      { filtersByRepository: true, managesSessionLinks: false, selectionActions: () => [] },
      { grouping: "author" },
    );
    const items = [
      "Select All in Group",
      "Filter to @alice on github.com",
      "Filter to @alice on github.example.com",
    ];
    await user.click(screen.getByRole("button", { name: "More actions for alice" }));
    const more = await screen.findByRole("menu");
    expect(
      within(more)
        .getAllByRole("menuitem")
        .map((item) => item.textContent),
    ).toEqual(items);
    await user.keyboard("{Escape}");
    expect(paletteCommand("Filter Pull Requests to @alice on github.com")).toBeDefined();
    expect(paletteCommand("Filter Pull Requests to @github-actions")).toBeDefined();
    // A login can't tell a person from an app, so nothing links to a guessed profile.
    const openProfile = /Open @/;
    expect(
      Object.values(usePaletteCommandsStore.getState().groups)
        .flatMap((group) => group.commands)
        .filter((command) => openProfile.test(command.label)),
    ).toEqual([]);
    await user.click(screen.getByRole("button", { name: "More actions for github-actions" }));
    expect(
      within(await screen.findByRole("menu"))
        .getAllByRole("menuitem")
        .map((item) => item.textContent),
    ).toEqual(["Select All in Group", "Filter to @github-actions"]);
    await user.keyboard("{Escape}");
    fireEvent.contextMenu(rowNamed("Write docs"));
    expect(
      within(await screen.findByRole("menu"))
        .getAllByRole("menuitem")
        .filter((item) => openProfile.test(item.textContent ?? "")),
    ).toEqual([]);
    await user.keyboard("{Escape}");

    fireEvent.contextMenu(screen.getByRole("button", { name: "alice group (2)" }));
    const menu = await screen.findByRole("menu");
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((item) => item.textContent),
    ).toEqual(items);
    await user.click(
      within(menu).getByRole("menuitem", { name: "Filter to @alice on github.example.com" }),
    );
    expect(rowFor("https://github.example.com/acme/app/pull/3")).toBeTruthy();
    expect(rowFor("https://github.com/acme/app/pull/1")).toBeNull();
    expect(rowFor("https://github.com/acme/app/pull/5")).toBeNull();
    expect(screen.getByRole("group", { name: "Active filters" })).toHaveTextContent(
      "@alice (github.example.com)",
    );

    // Escape takes the filter off, as it does a repository's.
    fireEvent.keyDown(screen.getByRole("grid", { name: "Pull requests" }), { key: "Escape" });
    expect(rowFor("https://github.com/acme/app/pull/1")).toBeTruthy();
    expect(screen.queryByRole("group", { name: "Active filters" })).toBeNull();
  });

  it("offers no Open of an author's page from their header or ⌘K, only their Filter", async () => {
    const user = userEvent.setup();
    fixture.rows = [
      row({ number: 1, title: "Retry uploads", author: "alice" }),
      row({ number: 2, title: "Write docs", author: "github-actions" }),
    ];
    renderList(
      { filtersByRepository: true, managesSessionLinks: false, selectionActions: () => [] },
      { grouping: "author" },
    );
    await user.click(screen.getByRole("button", { name: "More actions for alice" }));
    expect(
      within(await screen.findByRole("menu"))
        .getAllByRole("menuitem")
        .map((item) => item.textContent),
    ).toEqual(["Select All in Group", "Filter to @alice"]);
    await user.keyboard("{Escape}");
    const openAuthor = /^Open @|^Open github-actions/;
    expect(
      Object.values(usePaletteCommandsStore.getState().groups)
        .flatMap((group) => group.commands)
        .filter((command) => openAuthor.test(command.label)),
    ).toEqual([]);
  });

  it("shows no header for a lone author without filters, having nothing to offer", async () => {
    fixture.rows = [row({ number: 1, title: "Retry uploads", author: "alice" })];
    renderList({ managesSessionLinks: false, selectionActions: () => [] }, { grouping: "author" });
    expect(screen.queryByRole("button", { name: "More actions for alice" })).toBeNull();
  });

  it("keeps a collapsed author header's Filter, and offers none for the unknown author", async () => {
    const user = userEvent.setup();
    fixture.rows = [
      row({ number: 1, title: "Retry uploads", author: "alice" }),
      row({ number: 5, title: "Write docs", author: null }),
    ];
    renderList(
      { filtersByRepository: true, managesSessionLinks: false, selectionActions: () => [] },
      { grouping: "author" },
    );
    await user.click(screen.getByRole("button", { name: "alice group (1)" }));
    await user.click(screen.getByRole("button", { name: "More actions for alice" }));
    expect(await screen.findByRole("menuitem", { name: "Select All in Group" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(screen.getByRole("menuitem", { name: "Filter to @alice" })).not.toHaveAttribute(
      "aria-disabled",
    );
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "More actions for Unknown author" }));
    const unknown = await screen.findByRole("menu");
    expect(
      within(unknown)
        .getAllByRole("menuitem")
        .map((item) => item.textContent),
    ).toEqual(["Select All in Group"]);
  });

  it("keeps a collapsed repository header's links while it holds what acts on its rows", async () => {
    const user = userEvent.setup();
    fixture.rows = [
      row({ number: 1, title: "Retry uploads" }),
      row({ number: 5, title: "Write docs", repository: "acme/docs" }),
    ];
    renderList(
      { managesSessionLinks: false, selectionActions: () => [] },
      { grouping: "repository" },
    );
    await user.click(screen.getByRole("button", { name: /^acme\/docs group/ }));
    await user.click(screen.getByRole("button", { name: "More actions for acme/docs" }));
    expect(await screen.findByRole("menuitem", { name: "Select All in Group" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    for (const name of [
      "Open Repository on GitHub",
      "Copy Repository Name",
      "Copy Repository Link",
    ]) {
      expect(screen.getByRole("menuitem", { name })).not.toHaveAttribute("aria-disabled");
    }
    expect(isCommandAvailable(paletteCommand("Open acme/docs on GitHub")?.availability)).toBe(true);
  });

  it("keeps a lone repository's header while it offers Hide Repository", async () => {
    const user = userEvent.setup();
    fixture.rows = [row({ number: 1, title: "Retry uploads" })];
    const run = vi.fn();
    renderList(
      {
        managesSessionLinks: false,
        groupActions: ({ grouping }) =>
          grouping === "repository"
            ? [{ id: "page:hide-repository", label: "Hide Repository", icon: XIcon, run }]
            : [],
      },
      { grouping: "repository" },
    );
    await user.click(screen.getByRole("button", { name: "More actions for acme/app" }));
    await user.click(await screen.findByRole("menuitem", { name: "Hide Repository" }));
    expect(run).toHaveBeenCalled();
  });

  it("offers Add Filter and a repository picker in ⌘K", () => {
    renderList({ filtersByRepository: true });
    const commands = Object.values(usePaletteCommandsStore.getState().groups).flatMap(
      (group) => group.commands,
    );
    const labels = commands.map((command) => command.label);
    expect(labels).toEqual(
      expect.arrayContaining(["Add pull request filter", "Show pull requests in repository"]),
    );
  });
});

describe("PullRequestListPage's field filters", () => {
  const read = (number: number, fields: Record<string, unknown>) =>
    fixture.prs.set(`https://github.com/acme/app/pull/${number}`, fields);
  const label = (name: string) => ({ name, color: null, description: null });

  beforeEach(() => {
    fixture.rows = [
      row({ number: 1, title: "Retry uploads" }),
      row({ number: 2, title: "Ship docs" }),
      row({ number: 3, title: "Try a new layout" }),
    ];
    read(1, { labels: [label("bug")], base_ref: "main", reviewers: [] });
    read(2, { labels: [label("bug"), label("docs")], base_ref: "release", reviewers: [] });
    read(3, { labels: [label("docs")], base_ref: "main", reviewers: [] });
  });

  it("narrows by a label and a base branch together, each a chip that changes in place", async () => {
    const user = userEvent.setup();
    renderList({ filtersByRepository: true });
    await user.click(screen.getByRole("button", { name: "Add Filter" }));
    await user.type(await screen.findByRole("searchbox", { name: "Filter by" }), "bug");
    // Typing lists the label to require once: Exclude Label keeps to its own submenu.
    const bug = await screen.findByRole("menuitemcheckbox", { name: "Label bug" });
    expect(screen.getAllByRole("menuitemcheckbox")).toHaveLength(1);
    expect(bug).toHaveTextContent("2");
    await user.click(bug);
    await user.keyboard("{Escape}");
    expect(screen.queryByText("Try a new layout")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Add Filter" }));
    await user.type(await screen.findByRole("searchbox", { name: "Filter by" }), "main");
    // Among the rows labelled bug, one is on main.
    const main = await screen.findByRole("menuitemcheckbox", { name: "Base Branch main" });
    expect(main).toHaveTextContent("1");
    await user.click(main);
    await user.keyboard("{Escape}");
    expect(screen.getByText("Retry uploads")).toBeVisible();
    expect(screen.queryByText("Ship docs")).toBeNull();
    const chips = screen.getByRole("group", { name: "Active filters" });
    expect(within(chips).getByRole("button", { name: "Label is bug" })).toBeVisible();
    expect(within(chips).getByRole("button", { name: "Base Branch is main" })).toBeVisible();
    // Label is not bug: the rows without it, still on main.
    await user.click(
      within(chips).getByRole("button", { name: "Label is, change how it matches" }),
    );
    await user.click(await screen.findByRole("menuitemradio", { name: "is not" }));
    expect(screen.getByText("Try a new layout")).toBeVisible();
    expect(screen.queryByText("Retry uploads")).toBeNull();
    // ⌘K clears every chip at once.
    act(() => paletteCommand("Clear Pull Request Filters")?.run());
    expect(screen.queryByRole("group", { name: "Active filters" })).toBeNull();
    expect(screen.getByText("Retry uploads")).toBeVisible();
  });

  it("opens a field from ⌘K for the keyboard, small or searchable, and gives focus back", async () => {
    const user = userEvent.setup();
    renderList({ filtersByRepository: true });
    const filterButton = screen.getByRole("button", { name: "Add Filter" });
    // Two labels: the search takes focus, the arrows pick one, Enter checks it.
    act(() => paletteCommand("Filter by Label…")?.run());
    const search = await screen.findByRole("searchbox", { name: "Search Label" });
    await waitFor(() => expect(search).toHaveFocus());
    await user.keyboard("{ArrowDown}{Enter}");
    expect(
      within(screen.getByRole("group", { name: "Active filters" })).getByRole("button", {
        name: /^Label is /,
      }),
    ).toBeVisible();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(filterButton).toHaveFocus());
    // Many branches: typing narrows them, and the same keys pick one.
    fixture.rows = Array.from({ length: 10 }, (_, index) =>
      row({ number: index + 10, title: `Change ${index + 10}` }),
    );
    fixture.rows.forEach((listRow, index) =>
      fixture.prs.set(listRow.url, { labels: [], base_ref: `release/${index}`, reviewers: [] }),
    );
    act(() => paletteCommand("Clear Pull Request Filters")?.run());
    act(() => paletteCommand("Filter by Base Branch…")?.run());
    const branches = await screen.findByRole("searchbox", { name: "Search Base Branch" });
    await waitFor(() => expect(branches).toHaveFocus());
    await user.keyboard("release/7{ArrowDown}{Enter}");
    expect(screen.getByRole("button", { name: "Base Branch is release/7" })).toBeVisible();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(filterButton).toHaveFocus());
  });

  it("keeps a chip, and the focus on its operator, as the keyboard changes how it matches", async () => {
    const user = userEvent.setup();
    renderList({ filtersByRepository: true });
    act(() => paletteCommand("Filter by Base Branch…")?.run());
    await user.keyboard("main{ArrowDown}{Enter}{Escape}");
    const chips = screen.getByRole("group", { name: "Active filters" });
    const operator = within(chips).getByRole("button", {
      name: "Base Branch is, change how it matches",
    });
    operator.focus();
    await user.keyboard("{Enter}");
    // One value of a one-valued field: is, or is not.
    expect((await screen.findAllByRole("menuitemradio")).map((item) => item.textContent)).toEqual([
      "is",
      "is not",
    ]);
    await user.keyboard("{ArrowDown}{Enter}");
    expect(operator).toBeInTheDocument();
    expect(operator).toHaveFocus();
    expect(operator).toHaveAccessibleName("Base Branch is not, change how it matches");
    expect(screen.getByText("Ship docs")).toBeVisible();
    // Base Branch's menu shows main checked, and unchecking it takes the chip away: no
    // second chip asks for main beside the one leaving it out.
    act(() => paletteCommand("Filter by Base Branch…")?.run());
    const main = await screen.findByRole("menuitemcheckbox", { name: /main/ });
    expect(main).toHaveAttribute("aria-checked", "true");
    await user.keyboard("main{ArrowDown}{Enter}{Escape}");
    expect(screen.queryByRole("group", { name: "Active filters" })).toBeNull();
    expect(screen.getByText("Retry uploads")).toBeVisible();
  });

  it("offers one is for a single label, keeping all of where the chip uses it", async () => {
    const user = userEvent.setup();
    renderList({ filtersByRepository: true });
    act(() => paletteCommand("Filter by Label…")?.run());
    await user.keyboard("bug{ArrowDown}{Enter}{Escape}");
    await user.click(screen.getByRole("button", { name: "Label is, change how it matches" }));
    expect((await screen.findAllByRole("menuitemradio")).map((item) => item.textContent)).toEqual([
      "is",
      "is not",
    ]);
  });

  it("opens one field's values from ⌘K, and offers none that no row reports", async () => {
    renderList({ filtersByRepository: true });
    expect(paletteCommand("Filter by Label…")).toBeDefined();
    expect(paletteCommand("Exclude Label…")).toBeDefined();
    expect(paletteCommand("Filter by Base Branch…")).toBeDefined();
    // These reads report no assignees, as an older desktop app's don't.
    expect(paletteCommand("Filter by Assignee…")).toBeUndefined();
    act(() => paletteCommand("Filter by Label…")?.run());
    expect(await screen.findByRole("menuitemcheckbox", { name: /docs/ })).toBeVisible();
    const user = userEvent.setup();
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Add Filter" }));
    const assignee = await screen.findByRole("menuitem", { name: /Assignee/ });
    expect(assignee).toHaveAttribute("aria-disabled", "true");
  });
});

describe("PullRequestListPage with stacks", () => {
  const prUrl = (number: number) => `https://github.com/acme/app/pull/${number}`;
  function stackBody(self: number, stack: number[], footer = "---------") {
    const lines = stack.map((number, level) =>
      number === self
        ? `${"  ".repeat(level)}- [**stack/${number}**](${prUrl(number)}) ← _this PR_`
        : `${"  ".repeat(level)}- [stack/${number}](${prUrl(number)})`,
    );
    return ["## Stacked changes", ...lines, "", footer, "", "The change.", ""].join("\n");
  }
  /** Open rows #100, #101 and #102 stacked in that order, and an unstacked #7. */
  function stackedRows(stack = [100, 101, 102], footer?: string) {
    fixture.stacksFlag = true;
    fixture.rows = [...stack, 7].map((number) => row({ number }));
    for (const number of stack) fixture.bodies.set(prUrl(number), stackBody(number, stack, footer));
    fixture.bodies.set(prUrl(7), "Not stacked.");
  }
  const treegrid = () => screen.getByRole("treegrid", { name: "Linked pull requests" });
  const levels = () =>
    [100, 101, 102, 7]
      .map((number) => rowFor(prUrl(number)))
      .map((element) => element && element.getAttribute("aria-level"));
  const activeNumber = () => {
    const id = treegrid().getAttribute("aria-activedescendant");
    return (
      id &&
      Number(document.getElementById(id)?.getAttribute("data-pull-request-row")?.split("/").pop())
    );
  };
  const commands = () =>
    Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) => group.commands);

  it("nests a stack in a tree whose levels count the group headers shown", () => {
    stackedRows();
    renderList({ stacks: { foldScope: "user-1" } });
    // One group shows no header, so the stack's rows start at level 1.
    expect(levels()).toEqual(["1", "2", "3", "1"]);
    expect(rowFor(prUrl(100))).toHaveAttribute("aria-expanded", "true");
    expect(rowFor(prUrl(101))).toHaveAttribute("aria-expanded", "true");
    expect(rowFor(prUrl(102))).not.toHaveAttribute("aria-expanded");
    expect(rowFor(prUrl(7))).not.toHaveAttribute("aria-expanded");

    // A merged PR adds a second group: headers are level 1, and rows sit below them.
    fixture.rows = [...fixture.rows, row({ number: 9, state: "merged" })];
    renderList({ stacks: { foldScope: "user-1" } });
    const [, second] = screen.getAllByRole("treegrid");
    const header = within(second!)
      .getByRole("button", { name: "Open group (4)" })
      .closest('[role="row"]');
    expect(header).toHaveAttribute("aria-level", "1");
    expect(header).toHaveAttribute("aria-expanded", "true");
    const nested = within(second!)
      .getAllByRole("row")
      .filter((element) => element.hasAttribute("data-pull-request-row"))
      .map((element) => element.getAttribute("aria-level"));
    // #7 was updated last, so it comes first; the stack keeps its own order.
    expect(nested).toEqual(["2", "2", "3", "4", "2"]);
  });

  it("keeps the grid flat where the list doesn't show stacks", () => {
    stackedRows();
    // The session panel's list passes no stacks; with the flag off the page's doesn't nest.
    renderList();
    expect(screen.getByRole("grid", { name: "Linked pull requests" })).toBeInTheDocument();
    expect(rowFor(prUrl(101))).not.toHaveAttribute("aria-level");
    fixture.stacksFlag = false;
    renderList({ stacks: { foldScope: "user-1" } });
    expect(screen.queryByRole("treegrid")).toBeNull();
  });

  it("folds a stack from its button, keeping the fold on this device", async () => {
    const user = userEvent.setup();
    stackedRows();
    renderList({ stacks: { foldScope: "user-1" } });
    await user.click(screen.getByRole("button", { name: "Collapse the stack under #100" }));
    expect(rowFor(prUrl(101))).toBeNull();
    expect(rowFor(prUrl(100))).toHaveAttribute("aria-expanded", "false");
    expect(within(rowFor(prUrl(100))).getByText("+2 in stack")).toBeVisible();
    expect(localStorage.getItem("omnigent:pull-requests-collapsed-stacks")).toContain(prUrl(100));
  });

  it("leaves a folded stack's hidden rows out of its group's Select All", async () => {
    const user = userEvent.setup();
    stackedRows();
    fixture.rows = [...fixture.rows, row({ number: 9, state: "draft" })];
    renderList({ stacks: { foldScope: "user-1" }, selectionActions: () => [] });
    await user.click(screen.getByRole("button", { name: "Collapse the stack under #100" }));
    const select = commands().find((command) => command.id.endsWith(".select-group"));
    expect(select?.label).toBe("Select All Pull Requests in Open (2)");
    act(() => select!.run());
    expect(
      document.querySelector('[aria-label="Selected pull requests"] .pr-list-selection-count'),
    ).toHaveTextContent("2 selected");
    expect(within(rowFor(prUrl(100))).getByRole("checkbox")).toBeChecked();
  });

  it("steps in and out of a stack with Right and Left, folding and opening it", async () => {
    const user = userEvent.setup();
    stackedRows();
    renderList({ stacks: { foldScope: "user-1" } });
    // #7 was updated last, so it's first; the stack follows it.
    act(() => treegrid().focus());
    expect(activeNumber()).toBe(7);
    await user.keyboard("j");
    expect(activeNumber()).toBe(100);
    await user.keyboard("{ArrowRight}");
    expect(activeNumber()).toBe(101);
    await user.keyboard("{ArrowRight}");
    expect(activeNumber()).toBe(102);
    // A row with nothing under it steps out to its parent, which then folds.
    await user.keyboard("{ArrowLeft}");
    expect(activeNumber()).toBe(101);
    await user.keyboard("{ArrowLeft}");
    expect(rowFor(prUrl(102))).toBeNull();
    expect(rowFor(prUrl(101))).toHaveAttribute("aria-expanded", "false");
    await user.keyboard("{ArrowRight}");
    expect(rowFor(prUrl(102))).not.toBeNull();
    // j and k still walk every shown row.
    await user.keyboard("kk");
    expect(activeNumber()).toBe(7);
  });

  it("moves the keyboard to the stack's top row when a fold hides the active row", async () => {
    const user = userEvent.setup();
    stackedRows();
    renderList({ stacks: { foldScope: "user-1" } });
    treegrid().focus();
    // #102 is active and selected, then its stack folds from the button.
    await user.keyboard("jjj");
    expect(activeNumber()).toBe(102);
    await user.keyboard("x");
    expect(screen.getByRole("toolbar", { name: "Selected pull requests" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Collapse the stack under #100" }));
    expect(activeNumber()).toBe(100);
    // A row folded away leaves the selection, so nothing acts on it.
    expect(screen.queryByRole("toolbar", { name: "Selected pull requests" })).toBeNull();
    // A Shift range starts at the active row, not at the folded-away anchor.
    await user.keyboard("{Shift>}k{/Shift}");
    expect(rowFor(prUrl(100))).toHaveAttribute("aria-selected", "true");
    expect(rowFor(prUrl(7))).toHaveAttribute("aria-selected", "true");
  });

  it("keeps the keyboard on the stack's top row after a fold from another window", async () => {
    const user = userEvent.setup();
    stackedRows();
    renderList({ stacks: { foldScope: "user-1" } });
    act(() => treegrid().focus());
    await user.keyboard("jjj");
    expect(activeNumber()).toBe(102);
    const foldElsewhere = (urls: string[]) =>
      act(() => {
        const key = "omnigent:pull-requests-collapsed-stacks";
        if (urls.length > 0) {
          localStorage.setItem(
            key,
            JSON.stringify({ version: 1, stacks: urls.map((url) => ({ scope: "user-1", url })) }),
          );
        } else {
          localStorage.removeItem(key);
        }
        window.dispatchEvent(new StorageEvent("storage", { key }));
      });
    foldElsewhere([prUrl(100)]);
    expect(activeNumber()).toBe(100);
    // Opened again, the stack's rows come back, but the keyboard stays where it went.
    foldElsewhere([]);
    expect(rowFor(prUrl(102))).not.toBeNull();
    expect(activeNumber()).toBe(100);
    await user.keyboard("{Shift>}j{/Shift}");
    expect(rowFor(prUrl(100))).toHaveAttribute("aria-selected", "true");
    expect(rowFor(prUrl(101))).toHaveAttribute("aria-selected", "true");
    expect(rowFor(prUrl(102))).toHaveAttribute("aria-selected", "false");
  });

  it("counts every row the tree draws, headers included", async () => {
    const user = userEvent.setup();
    const counted = () => {
      const tree = screen.getAllByRole("treegrid").at(-1)!;
      return [Number(tree.getAttribute("aria-rowcount")), within(tree).getAllByRole("row").length];
    };
    stackedRows();
    renderList({ stacks: { foldScope: "user-1" } }, { grouping: "none" });
    expect(counted()).toEqual([5, 5]);
    fixture.rows = [...fixture.rows, row({ number: 9, state: "merged" })];
    renderList({ stacks: { foldScope: "user-1" } });
    expect(counted()).toEqual([8, 8]);
    const tree = screen.getAllByRole("treegrid").at(-1)!;
    await user.click(within(tree).getByRole("button", { name: "Open group (4)" }));
    expect(counted()).toEqual([4, 4]);
  });

  it("folds from the row menu and ⌘K alike, giving focus back to the list", async () => {
    const user = userEvent.setup();
    stackedRows();
    renderList({ stacks: { foldScope: "user-1" } });
    fireEvent.contextMenu(rowFor(prUrl(100)));
    await user.click(await screen.findByRole("menuitem", { name: "Collapse Stack" }));
    await vi.waitFor(() => expect(rowFor(prUrl(101))).toBeNull());
    expect(treegrid()).toHaveFocus();
    expect(activeNumber()).toBe(100);

    const expand = commands().find((command) => command.label === "Expand the stack under #100");
    expect(expand).toBeDefined();
    act(() => expand!.run());
    expect(rowFor(prUrl(101))).not.toBeNull();
    expect(commands().some((command) => command.label === "Collapse the stack under #100")).toBe(
      true,
    );
    // Only rows with a stack under them offer it.
    expect(commands().some((command) => command.label.endsWith("under #7"))).toBe(false);
  });

  it("indents a deep stack four levels and names the levels past them", () => {
    stackedRows([100, 101, 102, 103, 104, 105]);
    renderList({ stacks: { foldScope: "user-1" } });
    const level = within(rowFor(prUrl(105))).getByRole("img", { name: "Level 6" });
    expect(level).toHaveTextContent("6");
    expect(rowFor(prUrl(105))).toHaveAttribute("aria-level", "6");
    expect(within(rowFor(prUrl(104))).queryByRole("img", { name: /^Level/ })).toBeNull();
    expect(screen.queryByText(/↳ level/)).toBeNull();
    // The PR number stays in its own cell, ahead of the title.
    expect(within(rowFor(prUrl(105))).getByRole("link", { name: "#105" })).toBeInTheDocument();
  });

  it("notes stack info that may be out of date, on the parent and its nested rows", () => {
    stackedRows([100, 101], "---");
    renderList({ stacks: { foldScope: "user-1" } });
    for (const number of [100, 101]) {
      expect(
        within(rowFor(prUrl(number))).getByRole("img", { name: "Stack info may be out of date" }),
      ).toBeInTheDocument();
    }
    expect(
      within(rowFor(prUrl(7))).queryByRole("img", { name: "Stack info may be out of date" }),
    ).toBeNull();
  });

  it("says a stacked row's parent isn't shown here", () => {
    stackedRows();
    // #100 isn't in this list, so #101 is a top-level row.
    fixture.rows = fixture.rows.filter((listRow) => listRow.number !== 100);
    renderList({ stacks: { foldScope: "user-1" } });
    const hint = within(rowFor(prUrl(101))).getByText("Part of a stack");
    expect(hint).toHaveAttribute("title", "Stacked on #100, which isn’t shown here.");
    expect(rowFor(prUrl(101))).toHaveAttribute("aria-level", "1");
  });
});

describe("PullRequestListPage's open and copy actions on a selection", () => {
  // The Pull Requests page selects rows for its own actions, here none.
  const urlOf = (number: number) => `https://github.com/acme/app/pull/${number}`;
  const bar = () => screen.getByRole("toolbar", { name: "Selected pull requests" });
  const select = (title: string) => fireEvent.click(within(rowNamed(title)).getByRole("checkbox"));
  /** Choose `name` from the bar's More menu, as a click does. */
  async function fromMoreMenu(name: string) {
    const user = userEvent.setup();
    await user.click(within(bar()).getByRole("button", { name: "More Actions" }));
    await user.click(await screen.findByRole("menuitem", { name }));
  }
  const paletteLabels = () =>
    Object.values(usePaletteCommandsStore.getState().groups)
      .flatMap((group) => group.commands)
      .map((command) => command.label);

  /** A plain browser that opens a blank tab per call while `allowed` says so, else refuses. */
  function browserTabs(allowed: (call: number) => boolean = () => true) {
    const opened: string[] = [];
    let calls = 0;
    vi.spyOn(window, "open").mockImplementation(() => {
      if (!allowed(calls++)) return null;
      return { opener: window, document } as unknown as Window;
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      opened.push(this.href);
    });
    return opened;
  }

  beforeEach(() => {
    clipboard.copied = [];
    clipboard.refuse = false;
    vi.spyOn(toast, "success");
    vi.spyOn(toast, "error");
  });
  afterEach(() => vi.restoreAllMocks());

  it("offers them in the bar's More menu, a checked row's menu and ⌘K of every list that selects rows", async () => {
    const user = userEvent.setup();
    // A session's own list selects rows to remove them, with no actions of its caller's.
    renderList({ managesSessionLinks: true });
    select("Retry uploads");
    select("Try a new layout");
    // The bar keeps its room for what changes the PRs: these sit in its More menu.
    expect(within(bar()).queryByRole("button", { name: "Copy Links" })).toBeNull();
    await user.click(within(bar()).getByRole("button", { name: "More Actions" }));
    const more = await screen.findByRole("menu");
    expect(
      within(more)
        .getAllByRole("menuitem")
        .map((item) => item.textContent),
    ).toEqual(["Open on GitHub", "Copy Links", "Copy Branch Names"]);
    await user.keyboard("{Escape}");
    expect(paletteLabels()).toEqual(
      expect.arrayContaining([
        "Open 2 Pull Requests on GitHub",
        "Copy Links to 2 Pull Requests",
        "Copy Branch Names of 2 Pull Requests",
      ]),
    );
    // A checked row's menu nests them as a single row's are: its one open alone, then Copy ›.
    fireEvent.contextMenu(rowNamed("Try a new layout"));
    const menu = await screen.findByRole("menu");
    expect(topLevelItems(menu)).toEqual(["Open 2 Pull Requests on GitHub", "Copy"]);
    within(menu).getByRole("menuitem", { name: "Copy" }).focus();
    await user.keyboard("{ArrowRight}");
    const copies = (await screen.findAllByRole("menu")).at(-1)!;
    expect(topLevelItems(copies)).toEqual(["Links", "Branch Names"]);
    within(copies).getByRole("menuitem", { name: "Branch Names" }).focus();
    await user.keyboard("{Enter}");
    await vi.waitFor(() => expect(clipboard.copied).toEqual(["ada/change-1\nada/change-3"]));
    expect(toast.success).toHaveBeenCalledWith("Copied 2 branch names");
  });

  it("gives a checked row's menu only the selection's actions while several are checked", async () => {
    renderList({
      managesSessionLinks: false,
      selectionActions: (selected) => [
        {
          id: "close",
          label: "Close",
          paletteLabel: `Close ${selected.length} Pull Requests`,
          icon: XIcon,
          run: () => {},
        },
      ],
    });
    // One checked row keeps its own menu.
    select("Try a new layout");
    fireEvent.contextMenu(rowNamed("Try a new layout"));
    expect(topLevelItems(await screen.findByRole("menu"))).toContain("Open on GitHub");
    await userEvent.keyboard("{Escape}");
    select("Retry uploads");
    fireEvent.contextMenu(rowNamed("Try a new layout"));
    expect(topLevelItems(await screen.findByRole("menu"))).toEqual([
      "Close 2 Pull Requests",
      "Open 2 Pull Requests on GitHub",
      "Copy",
    ]);
  });

  it("opens every checked PR in the workspace sidebar from a checked row's Open ›, the bar and ⌘K", async () => {
    const user = userEvent.setup();
    const { onSelect } = renderList({
      managesSessionLinks: false,
      selectionActions: () => [],
      opensInWorkspace: true,
    });
    select("Try a new layout");
    select("Retry uploads");
    expect(paletteCommand("Open 2 Pull Requests in Workspace Sidebar")).toBeDefined();
    fireEvent.contextMenu(rowNamed("Try a new layout"));
    const menu = await screen.findByRole("menu");
    expect(topLevelItems(menu)).toEqual(["Open", "Copy"]);
    within(menu).getByRole("menuitem", { name: "Open" }).focus();
    await user.keyboard("{ArrowRight}");
    const opens = (await screen.findAllByRole("menu")).at(-1)!;
    expect(topLevelItems(opens)).toEqual(["In Workspace Sidebar", "On GitHub"]);
    within(opens).getByRole("menuitem", { name: "In Workspace Sidebar" }).focus();
    await user.keyboard("{Enter}");
    await vi.waitFor(() =>
      expect(onSelect.mock.calls).toEqual([
        [urlOf(1), "foreground"],
        [urlOf(3), "foreground"],
      ]),
    );
  });

  it("opens no more than 20 in the sidebar, saying why in the bar, ⌘K and a row's menu alike", async () => {
    const user = userEvent.setup();
    fixture.rows = Array.from({ length: 21 }, (_, index) =>
      row({ number: index + 1, title: `Change ${index + 1}` }),
    );
    const { onSelect } = renderList({
      managesSessionLinks: false,
      selectionActions: () => [],
      opensInWorkspace: true,
    });
    act(() => paletteCommand("Select all pull requests (21)")!.run());
    const command = paletteCommand("Open 21 Pull Requests in Workspace Sidebar")!;
    expect(command.availability).toEqual(
      expect.objectContaining({
        available: false,
        reason: "Select 20 or fewer to open them in the sidebar.",
      }),
    );
    await user.click(within(bar()).getByRole("button", { name: "More Actions" }));
    expect(
      await screen.findByRole("menuitem", { name: /^Open in Workspace Sidebar/ }),
    ).toHaveAttribute("aria-disabled", "true");
    await user.keyboard("{Escape}");
    fireEvent.contextMenu(rowNamed("Change 2"));
    const menu = await screen.findByRole("menu");
    within(menu).getByRole("menuitem", { name: "Open" }).focus();
    await user.keyboard("{ArrowRight}");
    const opens = (await screen.findAllByRole("menu")).at(-1)!;
    const inSidebar = within(opens).getByRole("menuitem", { name: /^In Workspace Sidebar/ });
    expect(inSidebar).toHaveAttribute("aria-disabled", "true");
    // The row's menu says why, as the bar's More menu and ⌘K do.
    expect(inSidebar).toHaveAccessibleDescription(
      "Select 20 or fewer to open them in the sidebar.",
    );
    expect(
      within(opens).getByText("Select 20 or fewer to open them in the sidebar."),
    ).toBeVisible();
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("copies the links in list order, and says so when the clipboard refuses them", async () => {
    renderList({ managesSessionLinks: false, selectionActions: () => [] });
    select("Try a new layout");
    select("Retry uploads");
    await fromMoreMenu("Copy Links");
    await vi.waitFor(() => expect(clipboard.copied).toEqual([`${urlOf(1)}\n${urlOf(3)}`]));
    expect(toast.success).toHaveBeenCalledWith("Copied 2 links");
    clipboard.refuse = true;
    await fromMoreMenu("Copy Links");
    await vi.waitFor(() => expect(toast.error).toHaveBeenCalledWith("Couldn’t copy the links"));
    expect(toast.success).toHaveBeenCalledTimes(1);
  });

  it("says under Copy Branch Names why it can't run while no branch is known, and copies nothing", async () => {
    for (const listed of fixture.rows) listed.headRef = null;
    const user = userEvent.setup();
    renderList({ managesSessionLinks: false, selectionActions: () => [] });
    select("Try a new layout");
    select("Retry uploads");
    await user.click(within(bar()).getByRole("button", { name: "More Actions" }));
    const branches = await screen.findByRole("menuitem", { name: "Copy Branch Names (0 of 2)" });
    // Disabled items take neither focus nor hover, so the reason shows as their description.
    expect(branches).toHaveAttribute("aria-disabled", "true");
    expect(branches).toHaveAccessibleDescription("Their branches aren’t known yet.");
    expect(within(branches).getByText("Their branches aren’t known yet.")).toBeVisible();
    await user.click(branches);
    expect(clipboard.copied).toEqual([]);
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("opens each one within the click, in list order", async () => {
    const opened = browserTabs();
    renderList({ managesSessionLinks: false, selectionActions: () => [] });
    select("Retry uploads");
    select("Try a new layout");
    const user = userEvent.setup();
    await user.click(within(bar()).getByRole("button", { name: "More Actions" }));
    const item = await screen.findByRole("menuitem", { name: "Open on GitHub" });
    fireEvent.click(item);
    // Nothing waits for later: a browser allows pop-ups only during the click.
    expect(opened).toEqual([urlOf(1), urlOf(3)]);
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("asks before opening more than ten, and opens what it asked about though the selection changed", async () => {
    const opened = browserTabs();
    fixture.rows = Array.from({ length: 12 }, (_, index) =>
      row({ number: index + 1, title: `Change ${index + 1}` }),
    );
    renderList({ managesSessionLinks: false, selectionActions: () => [] });
    act(() => screen.getByRole("grid", { name: "Pull requests" }).focus());
    fireEvent.keyDown(screen.getByRole("grid", { name: "Pull requests" }), {
      key: "a",
      metaKey: true,
    });
    await fromMoreMenu("Open on GitHub…");
    let dialog = await screen.findByRole("dialog", { name: "Open 12 pull requests on GitHub?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(opened).toEqual([]);

    await fromMoreMenu("Open on GitHub…");
    dialog = await screen.findByRole("dialog", { name: "Open 12 pull requests on GitHub?" });
    // The selection changes behind the dialog, hidden from it; what it asked about is what opens.
    fireEvent.click(
      rowFor(urlOf(1)).querySelector<HTMLElement>('[role="checkbox"], input[type="checkbox"]')!,
    );
    expect(
      document.querySelector('[aria-label="Selected pull requests"] .pr-list-selection-count'),
    ).toHaveTextContent("11 selected");
    fireEvent.click(within(dialog).getByRole("button", { name: "Open 12" }));
    expect(opened).toHaveLength(12);
    expect(new Set(opened)).toEqual(new Set(fixture.rows.map((listed) => listed.url)));
  });

  it("drops its question when the list moves to another session, which never opens them", async () => {
    const opened = browserTabs();
    fixture.rows = Array.from({ length: 12 }, (_, index) =>
      row({ number: index + 1, title: `Change ${index + 1}` }),
    );
    const view = renderList({ managesSessionLinks: false, selectionActions: () => [] });
    act(() => screen.getByRole("grid", { name: "Pull requests" }).focus());
    fireEvent.keyDown(screen.getByRole("grid", { name: "Pull requests" }), {
      key: "a",
      metaKey: true,
    });
    await fromMoreMenu("Open on GitHub…");
    await screen.findByRole("dialog", { name: "Open 12 pull requests on GitHub?" });
    view.switchTo("conv_2");
    await vi.waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    view.switchTo("conv_1");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(opened).toEqual([]);
  });

  it("names what a browser blocked, and opens or copies only those again", async () => {
    // The browser opens the first and refuses the rest.
    let opened = browserTabs((call) => call === 0);
    renderList({ managesSessionLinks: false, selectionActions: () => [] });
    select("Retry uploads");
    select("Try a new layout");
    select("Old idea");
    await fromMoreMenu("Open on GitHub");
    expect(opened).toEqual([urlOf(1)]);
    const [message, options] = vi.mocked(toast.error).mock.calls[0]!;
    expect(message).toBe(
      "Your browser blocked 2 of 3 pull requests from opening. Allow pop-ups for this site, then open the rest.",
    );
    const { action, cancel } = options as {
      action: { label: string; onClick: (event: { preventDefault: () => void }) => void };
      cancel: { label: string; onClick: () => void };
    };
    expect([action.label, cancel.label]).toEqual(["Open the Rest", "Copy Their Links"]);

    vi.restoreAllMocks();
    vi.spyOn(toast, "success");
    vi.spyOn(toast, "error");
    opened = browserTabs();
    // It settles its own notice, so the Toaster mustn't dismiss it on the click.
    const click = { preventDefault: vi.fn() };
    act(() => action.onClick(click));
    expect(click.preventDefault).toHaveBeenCalled();
    expect(opened).toEqual([urlOf(3), urlOf(4)]);
    act(() => cancel.onClick());
    await vi.waitFor(() => expect(clipboard.copied).toEqual([`${urlOf(3)}\n${urlOf(4)}`]));
  });
});

describe("a list with rows its search found that it hasn't loaded yet", () => {
  /** Shows the trailing block to the list as if scrolled into view, and back out. */
  const observed: { callback: IntersectionObserverCallback; disconnected: boolean }[] = [];
  const original = globalThis.IntersectionObserver;
  beforeEach(() => {
    observed.length = 0;
    globalThis.IntersectionObserver = class {
      private readonly entry: (typeof observed)[number];
      constructor(callback: IntersectionObserverCallback) {
        this.entry = { callback, disconnected: false };
        observed.push(this.entry);
      }
      observe() {}
      unobserve() {}
      disconnect() {
        this.entry.disconnected = true;
      }
      takeRecords() {
        return [];
      }
      root = null;
      rootMargin = "";
      thresholds = [];
    } as unknown as typeof IntersectionObserver;
  });
  afterEach(() => {
    globalThis.IntersectionObserver = original;
  });
  const scrollBlock = (shown: boolean) =>
    act(() => {
      const live = observed.filter((each) => !each.disconnected).at(-1)!;
      live.callback(
        [{ isIntersecting: shown } as IntersectionObserverEntry],
        {} as IntersectionObserver,
      );
    });

  function more(over: Partial<PullRequestListMore> = {}): PullRequestListMore {
    return {
      remainder: 370,
      loading: null,
      failure: null,
      end: null,
      countLabel: "420",
      inView: vi.fn(),
      request: vi.fn(),
      loadMore: vi.fn(),
      refresh: vi.fn(),
      ...over,
    };
  }

  it("sizes the list to every row its search found, and reads on while the end is in view", () => {
    // Open and merged rows, so the list draws its group headers.
    fixture.rows = Array.from({ length: 50 }, (_, index) =>
      row({ number: index + 1, state: index === 49 ? "merged" : "open" }),
    );
    const pages = more();
    renderList({ managesSessionLinks: false, more: pages });
    const grid = screen.getByRole("grid", { name: "Pull requests" });
    expect(grid).toHaveAttribute("aria-rowcount", String(50 + 370));
    const block = screen.getByTestId("pull-request-list-more");
    expect(block).toHaveTextContent("Not loaded yet");
    // The placeholders hold the rest of the list's height, and nothing in them is focusable.
    const placeholders = block.querySelector<HTMLElement>(".pr-list-more-rows")!;
    expect(placeholders.style.height).toBe(`${369 * 36}px`);
    expect(placeholders).toHaveAttribute("aria-hidden");
    expect(block.querySelectorAll("button, [tabindex]")).toHaveLength(0);
    scrollBlock(true);
    expect(pages.inView).toHaveBeenLastCalledWith(true);
    scrollBlock(false);
    expect(pages.inView).toHaveBeenLastCalledWith(false);
    // Group headers count what's loaded while the rest isn't.
    expect(screen.getByRole("button", { name: "Open group (49 loaded)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Merged group (1 loaded)" })).toHaveTextContent(
      "1 loaded",
    );
  });

  it("says which rows a page reads, and offers Retry when one failed", () => {
    fixture.rows = Array.from({ length: 50 }, (_, index) => row({ number: index + 1 }));
    const view = renderList({
      managesSessionLinks: false,
      more: more({ loading: { from: 51, to: 100, of: 420 } }),
    });
    expect(screen.getByRole("status")).toHaveTextContent("Loading 51–100 of 420…");
    cleanup();
    void view;
    const failed = more({ failure: "GitHub didn’t answer." });
    renderList({ managesSessionLinks: false, more: failed });
    expect(screen.getByRole("status")).toHaveTextContent(
      "Couldn’t load more pull requests. GitHub didn’t answer.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(failed.loadMore).toHaveBeenCalledTimes(1);
  });

  it("loads nothing on its own while filtered, and loads 50 more to search when asked", async () => {
    fixture.rows = Array.from({ length: 50 }, (_, index) => row({ number: index + 1 }));
    const pages = more({ countLabel: "at least 60" });
    renderList({ managesSessionLinks: false, more: pages });
    const user = userEvent.setup();
    await user.type(screen.getByRole("searchbox"), "nothing like this");
    // The placeholders give way to one line, and nothing watches for them to show.
    expect(screen.queryByTestId("pull-request-list-more")).toBeNull();
    expect(screen.getByText("Searching 50 of at least 60 loaded")).toBeInTheDocument();
    expect(pages.inView).not.toHaveBeenCalledWith(true);
    await user.click(screen.getByRole("button", { name: "Load 50 More to Search" }));
    expect(pages.loadMore).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("grid", { name: "Pull requests" })).toHaveAttribute(
      "aria-rowcount",
      "0",
    );
  });

  it("says why it ends short of GitHub's count, or at GitHub's 1,000", () => {
    fixture.rows = Array.from({ length: 5 }, (_, index) => row({ number: index + 1 }));
    const short = more({ remainder: 0, end: "incomplete" });
    renderList({ managesSessionLinks: false, more: short });
    expect(
      screen.getByText("Some pull requests may have moved while this list loaded."),
    ).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(short.refresh).toHaveBeenCalledTimes(1);
    cleanup();
    renderList({ managesSessionLinks: false, more: more({ remainder: 0, end: "capped" }) });
    expect(screen.getByText(/GitHub lists only the first 1,000 pull requests/)).toBeVisible();
    expect(screen.queryByTestId("pull-request-list-more")).toBeNull();
  });

  it("reads the next page when the keyboard moves past the last loaded row, then moves on", async () => {
    fixture.rows = Array.from({ length: 3 }, (_, index) => row({ number: index + 1 }));
    const pages = more();
    const view = renderList({ managesSessionLinks: false, more: pages });
    const grid = screen.getByRole("grid", { name: "Pull requests" });
    act(() => grid.focus());
    const user = userEvent.setup();
    await user.keyboard("jjj");
    expect(pages.request).toHaveBeenCalledTimes(1);
    expect(grid.getAttribute("aria-activedescendant")).toMatch(/row-2$/);
    // The page lands: the row after the last one is active.
    fixture.rows = Array.from({ length: 5 }, (_, index) => row({ number: index + 1 }));
    act(() => view.rerender());
    await waitFor(() => expect(grid.getAttribute("aria-activedescendant")).toMatch(/row-3$/));
  });

  it("selects only the loaded rows, and says so", async () => {
    fixture.rows = Array.from({ length: 3 }, (_, index) => row({ number: index + 1 }));
    const selectionActions = () => [
      { id: "approve", label: "Approve…", paletteLabel: "Approve", icon: CheckIcon, run: () => {} },
    ];
    renderList({ managesSessionLinks: false, selectionActions, more: more() });
    const grid = screen.getByRole("grid", { name: "Pull requests" });
    act(() => grid.focus());
    const user = userEvent.setup();
    await user.keyboard("x");
    expect(screen.getByRole("button", { name: "Select All 3 Loaded" })).toBeInTheDocument();
  });

  it("keeps its first page without windowing, the rollback", () => {
    fixture.windowFlag = false;
    fixture.rows = Array.from({ length: 5 }, (_, index) => row({ number: index + 1 }));
    renderList({ managesSessionLinks: false, more: more() });
    expect(screen.queryByTestId("pull-request-list-more")).toBeNull();
    expect(screen.getByRole("grid", { name: "Pull requests" })).toHaveAttribute(
      "aria-rowcount",
      "5",
    );
  });
});
