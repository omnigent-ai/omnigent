// A list on the Pull Requests page offers each row's "Open in workspace sidebar" in its
// menus and ⌘K, with the chord rows already open behind with, and its Ready for review and
// Convert to draft before the row's copies; other lists offer neither.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { SidebarDataProvider } from "@/hooks/useSidebarData";
import type { PullRequestAssociation } from "@/newPullRequestPage/hooks/usePullRequests";

import { GitPullRequestArrowIcon } from "lucide-react";

import type { LifecycleRow } from "@/pullRequestPage/useRowLifecycleActions";

import { type PullRequestListProps, usePullRequestListEntries } from "./usePullRequestListEntries";

const lifecycleRows = vi.hoisted(() => ({ asked: [] as { enabled: boolean; name: string }[] }));
vi.mock("@/pullRequestPage/useRowLifecycleActions", () => ({
  useRowLifecycleActions: (_conversationId: string, enabled: boolean) => ({
    dialog: null,
    actionsFor: (row: LifecycleRow) => {
      lifecycleRows.asked.push({ enabled, name: row.paletteName });
      return enabled
        ? [
            {
              id: "lifecycle:ready_for_review",
              section: "lifecycle",
              label: "Ready for review",
              paletteLabel: `Mark ${row.paletteName} Ready for Review`,
              icon: GitPullRequestArrowIcon,
              run: () => {},
            },
          ]
        : [];
    },
  }),
}));

const PR = "https://github.com/acme/app/pull/7";

function entriesFor(
  over: Partial<PullRequestListProps>,
  options: Parameters<typeof usePullRequestListEntries>[1] = {},
) {
  const onSelect = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <SidebarDataProvider>{children}</SidebarDataProvider>
    </QueryClientProvider>
  );
  const association = {
    url: PR,
    host: "github.com",
    repository: "acme/app",
    number: 7,
    relationship: "created",
  } as unknown as PullRequestAssociation;
  const { result } = renderHook(
    () =>
      usePullRequestListEntries(
        {
          conversationId: "pull-requests-page",
          active: false,
          associations: [association],
          hostOffline: true,
          sandboxAsleep: false,
          onSelect,
          unlinkPending: false,
          managesSessionLinks: false,
          ...over,
        },
        options,
      ),
    { wrapper },
  );
  const entry = result.current.entries[0]!;
  return {
    onSelect,
    actions: result.current.actionsFor(entry),
    commands: result.current.rowCommands,
  };
}

describe("a row's Open in workspace sidebar", () => {
  it("is offered with ⌘Enter's chord, opens the row as a foreground open, and lets the menu return focus", () => {
    const { onSelect, actions, commands } = entriesFor({ opensInWorkspace: true });
    const action = actions.find((candidate) => candidate.id === "open-in-workspace");
    expect(action).toMatchObject({
      label: "Open in workspace sidebar",
      shortcut: "rows.openInNewTab",
      movesFocus: true,
      afterMenuFocusReturns: true,
    });
    action!.run();
    expect(onSelect).toHaveBeenCalledWith(PR, "foreground");
    const command = commands.find((candidate) => candidate.id === `open-in-workspace:${PR}`);
    expect(command?.label).toBe("Open #7 in the workspace sidebar");
    expect(command?.shortcut).toBe("rows.openInNewTab");
  });

  it("isn't offered by other lists, whose actions carry no chord", () => {
    const { actions } = entriesFor({});
    expect(actions.map((action) => action.id)).not.toContain("open-in-workspace");
    expect(actions.filter((action) => action.shortcut !== undefined)).toEqual([]);
  });
});

describe("a row's Ready for review and Convert to draft", () => {
  it("follow the row's own actions, before its copies, with ⌘K rows named for the PR", () => {
    const { actions, commands } = entriesFor({}, { lifecycleOnRows: true });
    const ids = actions.map((action) => action.id);
    const ready = ids.indexOf("lifecycle:ready_for_review");
    expect(ready).toBeGreaterThan(ids.indexOf("open-on-provider"));
    expect(ready).toBeLessThan(ids.indexOf("copy-link"));
    expect(
      commands.find((command) => command.id === `lifecycle:ready_for_review:${PR}`)?.label,
    ).toBe("Mark #7 Ready for Review");
  });

  it("aren't offered by lists that don't ask for them", () => {
    lifecycleRows.asked = [];
    const { actions } = entriesFor({});
    expect(actions.map((action) => action.section)).not.toContain("lifecycle");
    expect(lifecycleRows.asked.every((asked) => !asked.enabled)).toBe(true);
  });
});

describe("a row's Copy branch name", () => {
  it("waits for the PR's branch, which a row nobody has read yet doesn't know", () => {
    const { actions, commands } = entriesFor({});
    expect(actions.map((action) => action.id)).not.toContain("copy-branch");
    expect(commands.map((command) => command.id)).not.toContain(`copy-branch:${PR}`);
  });
});
