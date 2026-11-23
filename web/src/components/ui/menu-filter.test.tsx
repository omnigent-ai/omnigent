import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useCommandPaletteHotkey } from "@/hooks/useCommandPaletteHotkey";
import { useNewSessionHotkey } from "@/hooks/useNewSessionHotkey";

import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuTrigger,
} from "./context-menu";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "./dropdown-menu";
import { MenuFilterField, menuFilterMatches, useMenuFilter } from "./menu-filter";

const navigate = vi.hoisted(() => vi.fn());
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigate }));

afterEach(() => {
  cleanup();
  navigate.mockReset();
});

interface Entry {
  label: string;
  disabled?: boolean;
}

const ENTRIES: Entry[] = [
  { label: "Merge…", disabled: true },
  { label: "Copy GitHub link" },
  { label: "Copy branch name" },
  { label: "Close pull request" },
];

function FilteredContextMenu({ onSelect }: { onSelect: (label: string) => void }) {
  const filter = useMenuFilter();
  const shown = ENTRIES.filter((entry) => menuFilterMatches(entry.label, filter.query));
  return (
    <ContextMenu>
      <ContextMenuTrigger>Pull request</ContextMenuTrigger>
      <ContextMenuContent {...filter.contentProps}>
        <MenuFilterField filter={filter} />
        {shown.map((entry) => (
          <ContextMenuItem
            key={entry.label}
            disabled={entry.disabled}
            onSelect={() => onSelect(entry.label)}
          >
            {entry.label}
          </ContextMenuItem>
        ))}
      </ContextMenuContent>
    </ContextMenu>
  );
}

function FilteredDropdownMenu({ onSelect }: { onSelect: (label: string) => void }) {
  const filter = useMenuFilter();
  const shown = ENTRIES.filter((entry) => menuFilterMatches(entry.label, filter.query));
  return (
    <DropdownMenu>
      <DropdownMenuTrigger>Actions</DropdownMenuTrigger>
      <DropdownMenuContent {...filter.contentProps}>
        <MenuFilterField filter={filter} />
        {shown.map((entry) => (
          <DropdownMenuItem
            key={entry.label}
            disabled={entry.disabled}
            onSelect={() => onSelect(entry.label)}
          >
            {entry.label}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

const field = () => screen.getByRole("textbox", { name: "Filter actions" });
const items = () => screen.queryAllByRole("menuitem").map((item) => item.textContent);

function openContextMenu() {
  const onSelect = vi.fn();
  render(<FilteredContextMenu onSelect={onSelect} />);
  fireEvent.contextMenu(screen.getByText("Pull request"));
  return onSelect;
}

function type(text: string) {
  fireEvent.change(field(), { target: { value: (field() as HTMLInputElement).value + text } });
}

describe("menuFilterMatches", () => {
  it("keeps text where each word starts a word, in any order, ignoring case and accents", () => {
    expect(menuFilterMatches("Copy branch name", "branch copy")).toBe(true);
    expect(menuFilterMatches("Copy branch name", "COPY  b")).toBe(true);
    expect(menuFilterMatches("Résumé review", "resume")).toBe(true);
    expect(menuFilterMatches("Open on GitHub (#42)", "42")).toBe(true);
    expect(menuFilterMatches("Copy branch name", "copy link")).toBe(false);
    // A word only matches from its start: "b" isn't "GitHub"'s.
    expect(menuFilterMatches("Copy GitHub link", "copy b")).toBe(false);
  });

  it("keeps everything for an empty or blank query", () => {
    expect(menuFilterMatches("Merge…", "")).toBe(true);
    expect(menuFilterMatches("Merge…", "   ")).toBe(true);
  });
});

describe("MenuFilterField in a context menu", () => {
  it("takes focus as the menu opens", async () => {
    openContextMenu();
    await waitFor(() => expect(field()).toHaveFocus());
  });

  it("narrows the items as the query is typed", async () => {
    openContextMenu();
    type("copy b");
    expect(items()).toEqual(["Copy branch name"]);
  });

  it("runs the first enabled match once on Enter, skipping a disabled one, and closes", async () => {
    const onSelect = openContextMenu();
    fireEvent.keyDown(field(), { key: "Enter" });
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("Copy GitHub link");
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });

  it("ignores an Enter that confirms composed text, then runs the next plain Enter once", async () => {
    const onSelect = openContextMenu();
    type("close");
    fireEvent.keyDown(field(), { key: "Enter", isComposing: true });
    fireEvent.keyDown(field(), { key: "Enter", keyCode: 229 });
    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.getByRole("menu")).toBeInTheDocument();
    expect(field()).toHaveValue("close");

    fireEvent.keyDown(field(), { key: "Enter" });
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("Close pull request");
  });

  it("leaves composing arrows and Escape to the input method", async () => {
    openContextMenu();
    type("copy");
    fireEvent.keyDown(field(), { key: "ArrowDown", isComposing: true });
    expect(field()).toHaveFocus();
    fireEvent.keyDown(field(), { key: "Escape", keyCode: 229 });
    expect(field()).toHaveValue("copy");
    expect(screen.getByRole("menu")).toBeInTheDocument();
  });

  it("clears a query on the first Escape and closes on the second", async () => {
    openContextMenu();
    type("copy");
    fireEvent.keyDown(field(), { key: "Escape" });
    expect(field()).toHaveValue("");
    expect(screen.getByRole("menu")).toBeInTheDocument();
    fireEvent.keyDown(field(), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });

  it("moves down to the first enabled item and back up to the field", async () => {
    openContextMenu();
    fireEvent.keyDown(field(), { key: "ArrowDown" });
    const first = screen.getByRole("menuitem", { name: "Copy GitHub link" });
    expect(first).toHaveFocus();
    fireEvent.keyDown(first, { key: "ArrowUp" });
    expect(field()).toHaveFocus();
  });

  it("sends a character typed on an item to the field instead of the typeahead", async () => {
    openContextMenu();
    fireEvent.keyDown(field(), { key: "ArrowDown" });
    fireEvent.keyDown(screen.getByRole("menuitem", { name: "Copy GitHub link" }), { key: "c" });
    expect(field()).toHaveFocus();
    expect(field()).toHaveValue("c");
    expect(items()).toEqual(["Copy GitHub link", "Copy branch name", "Close pull request"]);
  });

  it("lets Space select the focused item and close the menu", async () => {
    const onSelect = openContextMenu();
    fireEvent.keyDown(field(), { key: "ArrowDown" });
    fireEvent.keyDown(screen.getByRole("menuitem", { name: "Copy GitHub link" }), { key: " " });
    expect(onSelect).toHaveBeenCalledExactlyOnceWith("Copy GitHub link");
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });

  it("starts empty each time the menu opens", async () => {
    openContextMenu();
    type("copy");
    fireEvent.keyDown(field(), { key: "Escape" });
    fireEvent.keyDown(field(), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    fireEvent.contextMenu(screen.getByText("Pull request"));
    expect(field()).toHaveValue("");
    expect(items()).toHaveLength(ENTRIES.length);
  });
});

describe("MenuFilterField in a dropdown menu", () => {
  it("filters and runs the first match from the keyboard", async () => {
    const onSelect = vi.fn();
    render(<FilteredDropdownMenu onSelect={onSelect} />);
    const trigger = screen.getByRole("button", { name: "Actions" });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Enter" });
    await waitFor(() => expect(field()).toHaveFocus());
    type("branch");
    expect(items()).toEqual(["Copy branch name"]);
    fireEvent.keyDown(field(), { key: "Enter" });
    expect(onSelect).toHaveBeenCalledExactlyOnceWith("Copy branch name");
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });
});

/** The app's own chords on Ctrl-P (Sessions, off macOS) and Ctrl-N (New Session where bound), as mounted. */
function WithAppHotkeys({ onSessionSearch }: { onSessionSearch: () => void }) {
  useCommandPaletteHotkey(vi.fn(), true, false, onSessionSearch);
  useNewSessionHotkey(true, false);
  return <FilteredContextMenu onSelect={() => {}} />;
}

describe("MenuFilterField with the app's Ctrl-N and Ctrl-P chords", () => {
  function openWithHotkeys() {
    const onSessionSearch = vi.fn();
    render(<WithAppHotkeys onSessionSearch={onSessionSearch} />);
    fireEvent.contextMenu(screen.getByText("Pull request"));
    return onSessionSearch;
  }

  it("moves from the field to the first enabled item on Ctrl-N, without starting a session", () => {
    openWithHotkeys();
    const fromField = fireEvent.keyDown(field(), { key: "n", code: "KeyN", ctrlKey: true });
    expect(fromField).toBe(false);
    expect(screen.getByRole("menuitem", { name: "Copy GitHub link" })).toHaveFocus();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("keeps Ctrl-P in the menu instead of opening Sessions", () => {
    const onSessionSearch = openWithHotkeys();
    expect(fireEvent.keyDown(field(), { key: "p", code: "KeyP", ctrlKey: true })).toBe(false);
    expect(field()).toHaveFocus();
    expect(onSessionSearch).not.toHaveBeenCalled();
    expect(screen.getByRole("menu")).toBeInTheDocument();
  });

  it("leaves a composing Ctrl-N to the input method", () => {
    openWithHotkeys();
    fireEvent.keyDown(field(), { key: "n", code: "KeyN", ctrlKey: true, isComposing: true });
    expect(field()).toHaveFocus();
  });
});
