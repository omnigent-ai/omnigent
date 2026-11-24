import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FileIcon, GlobeIcon, TerminalIcon } from "lucide-react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";

import {
  TAB_ACTIONS_GROUP,
  type TabSearchChoice,
  type TabSearchClosing,
  type TabSearchEntry,
  tabSearchItems,
  WorkspaceTabSearch,
} from "./WorkspaceTabSearch";

const ENTRIES: TabSearchEntry[] = [
  {
    tabId: "t1",
    label: "main.go",
    tooltip: "cmd/main.go",
    icon: FileIcon,
    selected: false,
    group: "files",
    unsaved: true,
    closable: true,
  },
  {
    tabId: "t2",
    label: "zsh",
    tooltip: "zsh",
    icon: TerminalIcon,
    selected: true,
    group: "shells",
    closable: true,
  },
  {
    tabId: "t3",
    label: "Pull request #12",
    tooltip: "github.example.com/acme/repo/pull/12",
    icon: GlobeIcon,
    selected: false,
    group: "pull-requests",
    pullRequest: {
      number: "#12",
      title: "Fix the login form",
      description: "octocat · acme/repo",
      status: "merged",
    },
    closable: true,
  },
  {
    tabId: "t4",
    label: "domain.go",
    tooltip: "internal/domain.go",
    icon: FileIcon,
    selected: false,
    group: "files",
    closable: false,
  },
];

afterEach(cleanup);

describe("tabSearchItems", () => {
  it("lists each kind of tab in strip order, the one on screen in its own, then Reopen", () => {
    const items = tabSearchItems(ENTRIES, { label: "Reopen closed tab" });
    expect(items.map((item) => [item.label, item.group])).toEqual([
      ["#12 Fix the login form", "Pull requests"],
      ["main.go", "Files"],
      ["domain.go", "Files"],
      ["zsh", "Shells"],
      ["Reopen closed tab", TAB_ACTIONS_GROUP],
    ]);
  });

  it("names a PR by number and title, with who wrote it and where, still found by its address", () => {
    const pr = tabSearchItems(ENTRIES, null).find((item) => item.id === "tab:t3")!;
    expect(pr.description).toBe("octocat · acme/repo");
    expect(pr.keywords).toEqual(["Pull request #12", "github.example.com/acme/repo/pull/12"]);
  });

  it("shows a tab's place when it says more than its name", () => {
    const items = tabSearchItems(ENTRIES, null);
    const byId = (id: string) => items.find((item) => item.id === id)!;
    expect(byId("tab:t1").description).toBe("cmd/main.go");
    expect(byId("tab:t2").description).toBeUndefined();
  });

  it("offers Close on each tab this user can close, by its chord", () => {
    const onClose = vi.fn();
    const items = tabSearchItems(ENTRIES, null, onClose);
    const close = items.find((item) => item.id === "tab:t1")!.actions!;
    expect(close.map((action) => action.label)).toEqual(["Close tab"]);
    close[0]!.run();
    expect(onClose).toHaveBeenCalledWith("t1");
    expect(items.find((item) => item.id === "tab:t4")!.actions).toBeUndefined();
    expect(tabSearchItems(ENTRIES, null).some((item) => item.actions)).toBe(false);
  });

  it("shows Reopen with its chord when a closed tab can come back", () => {
    const reopen = tabSearchItems([], { label: "Reopen closed tab", shortcut: ["⇧", "⌘", "T"] })[0];
    expect(reopen).toMatchObject({ label: "Reopen closed tab", shortcut: ["⇧", "⌘", "T"] });
    expect(reopen.disabledReason).toBeUndefined();
  });

  it("offers no Actions group while no closed tab can come back", () => {
    const items = tabSearchItems(ENTRIES, null);
    expect(items.some((item) => item.group === TAB_ACTIONS_GROUP)).toBe(false);
  });
});

function Harness({
  onChoose,
  closing,
}: {
  onChoose: (choice: TabSearchChoice) => void;
  closing?: TabSearchClosing;
}) {
  const [open, setOpen] = useState(false);
  const [entries, setEntries] = useState(ENTRIES);
  return (
    <TooltipProvider>
      {/* ⌘K's "Search tabs…" opens the list without the layers button. */}
      <button type="button" onClick={() => setOpen(true)}>
        Run Search tabs
      </button>
      <WorkspaceTabSearch
        open={open}
        onOpenChange={setOpen}
        entries={entries}
        reopen={{ label: "Reopen closed tab" }}
        closing={
          closing && {
            ...closing,
            closeTab: (tabId) => {
              closing.closeTab(tabId);
              setEntries((current) => current.filter((entry) => entry.tabId !== tabId));
            },
          }
        }
        onChoose={onChoose}
      />
    </TooltipProvider>
  );
}

const options = () =>
  within(screen.getByRole("listbox", { name: "Search tabs" }))
    .getAllByRole("option")
    .map((option) => option.textContent ?? "");

describe("WorkspaceTabSearch", () => {
  it("opens from the layers button on the tab on screen, with every tab listed", async () => {
    render(<Harness onChoose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "All tabs" }));
    const field = screen.getByRole("combobox", { name: "Search tabs" });
    expect(field).toHaveFocus();
    expect(options()).toHaveLength(5);
    const active = document.getElementById(field.getAttribute("aria-activedescendant")!);
    expect(active).toHaveTextContent("zsh");
  });

  it("filters fuzzily by name and place, best match first, and switches to the pick", async () => {
    const onChoose = vi.fn();
    render(<Harness onChoose={onChoose} />);
    await userEvent.click(screen.getByRole("button", { name: "All tabs" }));
    await userEvent.keyboard("main");
    expect(options()[0]).toContain("main.go");
    expect(options()).not.toContainEqual(expect.stringContaining("zsh"));

    await userEvent.clear(screen.getByRole("combobox", { name: "Search tabs" }));
    await userEvent.keyboard("acme");
    expect(options()[0]).toContain("#12 Fix the login form");

    await userEvent.keyboard("{Enter}");
    expect(onChoose).toHaveBeenCalledWith({ kind: "tab", tabId: "t3" });
    expect(screen.queryByRole("combobox", { name: "Search tabs" })).toBeNull();
  });

  it("reopens a closed tab from its action row", async () => {
    const onChoose = vi.fn();
    render(<Harness onChoose={onChoose} />);
    await userEvent.click(screen.getByRole("button", { name: "All tabs" }));
    await userEvent.keyboard("reopen{Enter}");
    expect(onChoose).toHaveBeenCalledWith({ kind: "reopen" });
  });

  it("closes on Escape, and opens again with the whole list", async () => {
    render(<Harness onChoose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "All tabs" }));
    await userEvent.keyboard("zzz");
    expect(screen.getByRole("status")).toHaveTextContent("No open tab matches.");
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("combobox", { name: "Search tabs" })).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "All tabs" }));
    expect(screen.getByRole("combobox", { name: "Search tabs" })).toHaveValue("");
    expect(options()).toHaveLength(5);
  });

  it("opens from ⌘K with the whole list too, after a search from the layers button", async () => {
    render(<Harness onChoose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "All tabs" }));
    await userEvent.keyboard("main{Escape}");
    await userEvent.click(screen.getByRole("button", { name: "Run Search tabs" }));
    const field = screen.getByRole("combobox", { name: "Search tabs" });
    expect(field).toHaveValue("");
    expect(options()).toHaveLength(5);
    expect(document.getElementById(field.getAttribute("aria-activedescendant")!)).toHaveTextContent(
      "zsh",
    );
  });

  it("groups the tabs under headings that count them, with the PR's status and unsaved edits", async () => {
    render(<Harness onChoose={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "All tabs" }));
    expect(screen.getByRole("group", { name: "Pull requests 1" })).toHaveTextContent("Merged");
    const files = screen.getByRole("group", { name: "Files 2" });
    expect(within(files).getByRole("img", { name: "Unsaved changes" })).toBeTruthy();
    // The tab on screen counts in its own group, which the list opens on.
    const shells = screen.getByRole("group", { name: "Shells 1" });
    expect(within(shells).getByRole("option", { name: /zsh/ })).toHaveTextContent("Current");
    const field = screen.getByRole("combobox", { name: "Search tabs" });
    expect(document.getElementById(field.getAttribute("aria-activedescendant")!)).toHaveTextContent(
      "zsh",
    );
  });

  it("closes a tab from its row by click or chord, keeping the list open", async () => {
    const closeTab = vi.fn();
    render(<Harness onChoose={vi.fn()} closing={{ closeTab, groupActions: () => [] }} />);
    await userEvent.click(screen.getByRole("button", { name: "All tabs" }));
    const field = screen.getByRole("combobox", { name: "Search tabs" });
    await userEvent.keyboard("main");
    // With text before the caret, the chord edits the query, as it does in any text field.
    await userEvent.keyboard("{Control>}{Backspace}{/Control}");
    expect(closeTab).not.toHaveBeenCalled();
    await userEvent.clear(field);
    await userEvent.keyboard("main");
    // With the caret at the start there's nothing for it to delete, so it closes the tab.
    act(() => (field as HTMLInputElement).setSelectionRange(0, 0));
    await userEvent.keyboard("{Control>}{Backspace}{/Control}");
    expect(closeTab).toHaveBeenCalledWith("t1");
    expect(field).toHaveFocus();
    expect(field).toHaveValue("main");
    await userEvent.clear(field);
    expect(options()).toHaveLength(4);

    const pr = screen.getByRole("option", { name: /Fix the login form/ });
    const row = pr.closest<HTMLElement>("[data-slot=quick-pick-row]")!;
    await userEvent.click(within(row).getByRole("button", { name: "Close tab", hidden: true }));
    expect(closeTab).toHaveBeenLastCalledWith("t3");
    expect(screen.getByRole("combobox", { name: "Search tabs" })).toBeTruthy();
  });

  it("moves to the next tab after closing the highlighted one", async () => {
    render(<Harness onChoose={vi.fn()} closing={{ closeTab: vi.fn(), groupActions: () => [] }} />);
    await userEvent.click(screen.getByRole("button", { name: "All tabs" }));
    const field = screen.getByRole("combobox", { name: "Search tabs" });
    const active = () => document.getElementById(field.getAttribute("aria-activedescendant")!);
    expect(active()).toHaveTextContent("zsh");
    await userEvent.keyboard("{ArrowUp}{ArrowUp}{ArrowUp}");
    expect(active()).toHaveTextContent("Fix the login form");
    await userEvent.keyboard("{Control>}{Backspace}{/Control}");
    expect(active()).toHaveTextContent("main.go");
  });

  it("runs a group's buttons from its heading", async () => {
    const closeMerged = vi.fn();
    render(
      <Harness
        onChoose={vi.fn()}
        closing={{
          closeTab: vi.fn(),
          groupActions: (group) =>
            group === "pull-requests"
              ? [
                  {
                    id: "close-merged-pull-requests",
                    label: "Close merged (1)",
                    ariaLabel: "Close merged pull request tabs",
                    run: closeMerged,
                  },
                ]
              : [],
        }}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "All tabs" }));
    expect(screen.getByRole("group", { name: "Pull requests 1" })).toBeTruthy();
    const button = screen.getByRole("button", { name: "Close merged pull request tabs" });
    expect(button.textContent).toBe("Close merged (1)");
    await userEvent.click(button);
    expect(closeMerged).toHaveBeenCalledOnce();
  });
});
