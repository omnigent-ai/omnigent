import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FileIcon, GlobeIcon, TerminalIcon } from "lucide-react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";

import {
  OPEN_TABS_GROUP,
  TAB_ACTIONS_GROUP,
  type TabSearchChoice,
  type TabSearchEntry,
  tabSearchItems,
  WorkspaceTabSearch,
} from "./WorkspaceTabSearch";

const ENTRIES: TabSearchEntry[] = [
  { tabId: "t1", label: "main.go", tooltip: "cmd/main.go", icon: FileIcon, selected: false },
  { tabId: "t2", label: "zsh", tooltip: "zsh", icon: TerminalIcon, selected: true },
  {
    tabId: "t3",
    label: "Pull request #12",
    tooltip: "github.com/acme/repo/pull/12",
    icon: GlobeIcon,
    selected: false,
  },
  {
    tabId: "t4",
    label: "domain.go",
    tooltip: "internal/domain.go",
    icon: FileIcon,
    selected: false,
  },
];

afterEach(cleanup);

describe("tabSearchItems", () => {
  it("lists every tab in strip order, then Reopen closed tab", () => {
    const items = tabSearchItems(ENTRIES, { label: "Reopen closed tab" });
    expect(items.map((item) => [item.label, item.group])).toEqual([
      ["main.go", OPEN_TABS_GROUP],
      ["zsh", OPEN_TABS_GROUP],
      ["Pull request #12", OPEN_TABS_GROUP],
      ["domain.go", OPEN_TABS_GROUP],
      ["Reopen closed tab", TAB_ACTIONS_GROUP],
    ]);
  });

  it("shows a tab's place when it says more than its name, and marks the tab on screen", () => {
    const [main, shell] = tabSearchItems(ENTRIES, { label: "Reopen closed tab" });
    expect(main.description).toBe("cmd/main.go");
    expect(shell.description).toBeUndefined();
    expect(shell.accessory).toBeDefined();
    expect(main.accessory).toBeUndefined();
  });

  it("shows Reopen with its chord when a closed tab can come back", () => {
    const reopen = tabSearchItems([], { label: "Reopen closed tab", shortcut: ["⇧", "⌘", "T"] })[0];
    expect(reopen).toMatchObject({ label: "Reopen closed tab", shortcut: ["⇧", "⌘", "T"] });
    expect(reopen.disabledReason).toBeUndefined();
  });

  it("offers no Actions group while no closed tab can come back", () => {
    const items = tabSearchItems(ENTRIES, null);
    expect(items.map((item) => item.label)).toEqual([
      "main.go",
      "zsh",
      "Pull request #12",
      "domain.go",
    ]);
    expect(items.some((item) => item.group === TAB_ACTIONS_GROUP)).toBe(false);
  });
});

function Harness({ onChoose }: { onChoose: (choice: TabSearchChoice) => void }) {
  const [open, setOpen] = useState(false);
  return (
    <TooltipProvider>
      {/* ⌘K's "Search tabs…" opens the list without the layers button. */}
      <button type="button" onClick={() => setOpen(true)}>
        Run Search tabs
      </button>
      <WorkspaceTabSearch
        open={open}
        onOpenChange={setOpen}
        entries={ENTRIES}
        reopen={{ label: "Reopen closed tab" }}
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
    expect(options()[0]).toContain("Pull request #12");

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
});
