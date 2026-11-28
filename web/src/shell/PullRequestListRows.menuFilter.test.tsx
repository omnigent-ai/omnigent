import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { CopyIcon } from "lucide-react";
import { afterEach, describe, expect, it } from "vitest";

import { ContextMenu, ContextMenuContent, ContextMenuTrigger } from "@/components/ui/context-menu";
import { useMenuFilter } from "@/components/ui/menu-filter";
import { useMenuCloseHandOff } from "@/hooks/useMenuCloseHandOff";

import {
  CONTEXT_ROW_MENU,
  filterRowActions,
  type PullRequestListEntry,
  type RowAction,
  rowActionCommand,
  RowActionMenuBody,
} from "./PullRequestListRows";

afterEach(cleanup);

function action(
  id: string,
  section: RowAction["section"],
  label: string,
  extra: Partial<RowAction> = {},
): RowAction {
  return {
    id,
    section,
    label,
    paletteLabel: `${label} of #42 Bump the build`,
    icon: CopyIcon,
    run: () => {},
    ...extra,
  } as RowAction;
}

describe("rowActionCommand", () => {
  it("carries the action's description to its ⌘K command", () => {
    const entry = {
      association: { url: "https://github.com/acme/app/pull/42" },
      row: { repository: "acme/app" },
    } as unknown as PullRequestListEntry;
    const described = action("merge", "merge", "Merge on GitHub", {
      description: "GitHub merges a stack together.",
    });
    expect(rowActionCommand(entry, described)).toMatchObject({
      label: "Merge on GitHub of #42 Bump the build",
      description: "GitHub merges a stack together.",
    });
  });
});

describe("filterRowActions", () => {
  const actions = [
    action("open-on-provider", "open", "Open on GitHub"),
    action("review", "review", "Review…", { keywords: ["approve", "comment"] }),
    action("copy-branch", "copy", "Copy branch name"),
  ];

  it("keeps every action for an empty query, in order", () => {
    expect(filterRowActions(actions, " ").map((kept) => kept.id)).toEqual([
      "open-on-provider",
      "review",
      "copy-branch",
    ]);
  });

  it("matches a label or a keyword", () => {
    expect(filterRowActions(actions, "copy b").map((kept) => kept.id)).toEqual(["copy-branch"]);
    expect(filterRowActions(actions, "approve").map((kept) => kept.id)).toEqual(["review"]);
    expect(filterRowActions(actions, "merge")).toEqual([]);
  });

  it("ignores the palette label, which names the PR for every action alike", () => {
    expect(filterRowActions(actions, "bump")).toEqual([]);
    expect(filterRowActions(actions, "42")).toEqual([]);
  });
});

/** A row's context menu as the PR surfaces draw it, logging what ran and where focus was. */
function RowMenu({ actions }: { actions: RowAction[] }) {
  const menu = useMenuCloseHandOff();
  const filter = useMenuFilter();
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <div role="row" tabIndex={-1}>
          #42 Fix the parser
        </div>
      </ContextMenuTrigger>
      <ContextMenuContent {...filter.contentProps} onCloseAutoFocus={menu.onCloseAutoFocus}>
        <RowActionMenuBody actions={actions} parts={CONTEXT_ROW_MENU} menu={menu} filter={filter} />
      </ContextMenuContent>
    </ContextMenu>
  );
}

function openMenu(actions: RowAction[]) {
  render(<RowMenu actions={actions} />);
  const row = screen.getByRole("row");
  row.focus();
  fireEvent.contextMenu(row);
  return { row, field: screen.getByRole("textbox", { name: "Filter actions" }) };
}

const layout = () =>
  [...screen.getByRole("menu").querySelectorAll('[role="menuitem"], [role="separator"]')]
    .map((element) =>
      element.getAttribute("role") === "separator" ? "|" : element.textContent?.trim(),
    )
    .join(" / ");

describe("RowActionMenuBody", () => {
  it("shows an action's description under its label, as the item's accessible description", () => {
    openMenu([
      action("merge", "merge", "Merge on GitHub", {
        description: "GitHub merges a stack together.",
      }),
    ]);
    const item = screen.getByRole("menuitem", { name: /Merge on GitHub/ });
    expect(item).toHaveAccessibleDescription("GitHub merges a stack together.");
  });

  it("draws separators only between the sections a query keeps", () => {
    const { field } = openMenu([
      action("open", "open", "Open"),
      action("copy-link", "copy", "Copy GitHub link"),
      action("draft", "lifecycle", "Convert to draft"),
      action("copy-branch", "copy", "Copy branch name"),
    ]);
    fireEvent.change(field, { target: { value: "co" } });
    // The copies sit under Open, in its group, wherever they came in the actions.
    expect(layout()).toBe("Copy GitHub link / Copy branch name / | / Convert to draft");
    fireEvent.change(field, { target: { value: "copy" } });
    expect(layout()).toBe("Copy GitHub link / Copy branch name");
    fireEvent.change(field, { target: { value: "open" } });
    expect(layout()).toBe("Open");
  });

  it("gathers a section's Open and Copy actions into submenus, but leaves one alone", () => {
    openMenu([
      action("open", "open", "Open", { paletteOnly: true }),
      action("open-in-workspace", "open", "Open in workspace sidebar", {
        nestedLabel: "In workspace sidebar",
      }),
      action("open-on-provider", "open", "Open on GitHub", { nestedLabel: "On GitHub" }),
      action("review", "review", "Review…"),
      action("copy-link", "copy", "Copy GitHub link", { nestedLabel: "GitHub link" }),
      action("copy-branch", "copy", "Copy branch name", { nestedLabel: "Branch name" }),
      action("hide", "page", "Hide Repository", { paletteOnly: true }),
      action("remove", "remove", "Remove from this session"),
    ]);
    expect(layout()).toBe("Open / Copy / | / Review… / | / Remove from this session");
    expect(screen.getByRole("menuitem", { name: "Copy" })).toHaveAttribute("aria-haspopup", "menu");
  });

  it("puts Open in new tab at the top, then Open › and Copy › in the same group", () => {
    openMenu([
      action("open", "open", "Open", { paletteOnly: true }),
      action("open-in-new-tab", "open", "Open in new tab"),
      action("open-in-workspace", "open", "Open in workspace sidebar", {
        nestedLabel: "In workspace sidebar",
      }),
      action("open-on-provider", "open", "Open on GitHub", { nestedLabel: "On GitHub" }),
      action("request-review", "review", "Request Review…"),
      action("edit-labels", "review", "Edit Labels…"),
      action("copy-link", "copy", "Copy GitHub link", { nestedLabel: "GitHub link" }),
      action("copy-branch", "copy", "Copy branch name", { nestedLabel: "Branch name" }),
      action("remove", "remove", "Remove from this session"),
    ]);
    expect(layout()).toBe(
      "Open in new tab / Open / Copy / | / Request Review… / Edit Labels… / | / Remove from this session",
    );
    expect(screen.getByRole("menuitem", { name: "Open in new tab" })).not.toHaveAttribute(
      "aria-haspopup",
    );
  });

  it("shows a lone nested action as itself, with its full label", () => {
    openMenu([
      action("open-on-provider", "open", "Open on GitHub", { nestedLabel: "On GitHub" }),
      action("copy-link", "copy", "Copy GitHub link", { nestedLabel: "GitHub link" }),
    ]);
    expect(layout()).toBe("Open on GitHub / Copy GitHub link");
  });

  it("lists every nested match inline for a query, and runs the first on Enter", async () => {
    const ran: string[] = [];
    const { field } = openMenu([
      action("open-in-new-tab", "open", "Open in new tab"),
      action("open-on-provider", "open", "Open on GitHub", { nestedLabel: "On GitHub" }),
      action("copy-link", "copy", "Copy GitHub link", { nestedLabel: "GitHub link" }),
      action("copy-branch", "copy", "Copy branch name", {
        nestedLabel: "Branch name",
        run: () => ran.push("branch"),
      }),
      action("open", "open", "Open", { paletteOnly: true, keywords: ["branch"] }),
    ]);
    await waitFor(() => expect(field).toHaveFocus());
    fireEvent.change(field, { target: { value: "branch" } });
    expect(layout()).toBe("Copy branch name");
    fireEvent.change(field, { target: { value: "github" } });
    expect(layout()).toBe("Open on GitHub / Copy GitHub link");
    fireEvent.change(field, { target: { value: "branch" } });
    fireEvent.keyDown(field, { key: "Enter" });
    expect(ran).toEqual(["branch"]);
  });

  it("says nothing matches when a query keeps no action", () => {
    const { field } = openMenu([action("open", "open", "Open")]);
    fireEvent.change(field, { target: { value: "zebra" } });
    expect(screen.queryAllByRole("menuitem")).toEqual([]);
    expect(screen.getByRole("status")).toHaveTextContent("No actions match “zebra”");
  });

  it("marks the first available match while the field has focus, and runs it on Enter", async () => {
    const ran: string[] = [];
    const { field } = openMenu([
      action("merge", "merge", "Merge…", { availability: { available: false, reason: "Blocked" } }),
      action("copy-number", "copy", "Copy pull request number", { run: () => ran.push("number") }),
      action("copy-branch", "copy", "Copy branch name", { run: () => ran.push("branch") }),
    ]);
    await waitFor(() => expect(field).toHaveFocus());
    const marked = () =>
      [...document.querySelectorAll("[data-menu-filter-target]")].map((item) =>
        item.textContent?.trim(),
      );
    expect(marked()).toEqual([]);
    fireEvent.change(field, { target: { value: "c" } });
    expect(marked()).toEqual(["Copy pull request number"]);
    // On the items, Radix's own highlight takes over.
    fireEvent.keyDown(field, { key: "ArrowDown" });
    expect(marked()).toEqual([]);
    field.focus();
    fireEvent.keyDown(field, { key: "Enter" });
    expect(ran).toEqual(["number"]);
  });

  it("runs an action that moves focus once, after focus is back on the row", async () => {
    const ran: string[] = [];
    let focusedAsItRan: Element | null = null;
    const { row, field } = openMenu([
      action("review", "review", "Review…", {
        movesFocus: true,
        afterMenuFocusReturns: true,
        run: () => {
          ran.push("review");
          focusedAsItRan = document.activeElement;
        },
      }),
    ]);
    fireEvent.change(field, { target: { value: "rev" } });
    fireEvent.keyDown(field, { key: "Enter", isComposing: true });
    expect(screen.getByRole("menu")).toBeInTheDocument();
    expect(ran).toEqual([]);
    fireEvent.keyDown(field, { key: "Enter" });
    await waitFor(() => expect(ran).toEqual(["review"]));
    expect(focusedAsItRan).toBe(row);
    await new Promise((resolve) => {
      setTimeout(resolve, 20);
    });
    expect(ran).toEqual(["review"]);
  });
});
