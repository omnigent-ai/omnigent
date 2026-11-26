// The "Add filter…" menu: its search finds values in every category, Enter and the arrow keys
// check them without closing it, and its submenus and chips change the same values in place.

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  FilterChip,
  FilterMenu,
  FilterMenuButton,
  type FilterMenuCategory,
  filterMenuMatches,
  type FilterMenuOption,
} from "./filter-menu";

const CATEGORIES: FilterMenuCategory[] = [
  {
    id: "repository",
    label: "Repository",
    options: [
      { id: "example/app", label: "example/app", count: 4, checked: false },
      { id: "example/docs", label: "example/docs", count: 1, checked: false },
    ],
  },
  {
    id: "exclude",
    label: "Exclude Repository",
    options: [{ id: "example/noise", label: "example/noise", count: 9, checked: true }],
  },
];

/** `count` repositories, one past the search threshold at 9. */
function repositories(count: number): FilterMenuOption[] {
  return Array.from({ length: count }, (_, index) => ({
    id: `example/repo-${index + 1}`,
    label: `example/repo-${index + 1}`,
    checked: false,
  }));
}

const MANY: FilterMenuCategory[] = [
  { id: "repository", label: "Repository", options: repositories(9) },
  { id: "label", label: "Label", options: [{ id: "bug", label: "bug", checked: false }] },
];

function Harness({
  onToggle = vi.fn(),
  initial = CATEGORIES,
  only,
}: {
  onToggle?: (category: string, option: string) => void;
  initial?: FilterMenuCategory[];
  only?: string;
}) {
  const [categories, setCategories] = useState(initial);
  const toggle = (categoryId: string, optionId: string) => {
    onToggle(categoryId, optionId);
    setCategories((current) =>
      current.map((category) =>
        category.id !== categoryId
          ? category
          : {
              ...category,
              options: category.options.map((option) =>
                option.id === optionId ? { ...option, checked: !option.checked } : option,
              ),
            },
      ),
    );
  };
  return (
    <FilterMenu
      categories={categories}
      onToggle={toggle}
      shortcut="F"
      category={only}
      // As a command opens one category, the caller holds the menu open.
      open={only ? true : undefined}
      trigger={<FilterMenuButton />}
    />
  );
}

const openMenu = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.click(screen.getByRole("button", { name: "Add Filter" }));
  const search = await screen.findByRole("searchbox", { name: "Filter by" });
  await waitFor(() => expect(search).toHaveFocus());
  return search;
};

describe("FilterMenu", () => {
  it("opens with its search focused and lists a submenu per category", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Add Filter" }));
    const search = await screen.findByRole("searchbox", { name: "Filter by" });
    await waitFor(() => expect(search).toHaveFocus());
    expect(screen.getByRole("menuitem", { name: /^Repository/ })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /^Exclude Repository/ })).toBeInTheDocument();
  });

  it("finds values in every category as it's typed in, and checks one with the keyboard", async () => {
    const user = userEvent.setup();
    const onToggle = vi.fn();
    render(<Harness onToggle={onToggle} />);
    await user.click(screen.getByRole("button", { name: "Add Filter" }));
    const search = await screen.findByRole("searchbox", { name: "Filter by" });
    await user.type(search, "docs");
    expect(search).toHaveValue("docs");
    // The letters went to the search, not the menu's typeahead.
    expect(screen.getAllByRole("menuitemcheckbox").map((item) => item.textContent)).toEqual([
      "Repositoryexample/docs1",
    ]);
    await user.keyboard("{Enter}");
    expect(onToggle).toHaveBeenCalledWith("repository", "example/docs");
    // Checking keeps the menu open, so several can be picked.
    expect(screen.getByRole("menuitemcheckbox", { name: /example\/docs/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(search).toHaveFocus();
  });

  it("matches a category's name too, and says when nothing does", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Add Filter" }));
    const search = await screen.findByRole("searchbox", { name: "Filter by" });
    await user.type(search, "exclude");
    expect(screen.getAllByRole("menuitemcheckbox")).toHaveLength(1);
    await user.clear(search);
    await user.type(search, "zzz");
    expect(screen.queryAllByRole("menuitemcheckbox")).toHaveLength(0);
    expect(screen.getByText("No filters match “zzz”")).toBeInTheDocument();
  });

  it("opens a category's submenu from the keyboard and toggles its values in place", async () => {
    const user = userEvent.setup();
    const onToggle = vi.fn();
    render(<Harness onToggle={onToggle} />);
    await user.click(screen.getByRole("button", { name: "Add Filter" }));
    await screen.findByRole("searchbox", { name: "Filter by" });
    await user.keyboard("{ArrowDown}{ArrowDown}{ArrowRight}");
    const noise = await screen.findByRole("menuitemcheckbox", { name: /example\/noise/ });
    expect(noise).toHaveAttribute("aria-checked", "true");
    await user.click(noise);
    expect(onToggle).toHaveBeenCalledWith("exclude", "example/noise");
    expect(noise).toHaveAttribute("aria-checked", "false");
  });
});

describe("FilterMenu's keyboard", () => {
  it("opens from the keyboard with its search focused", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    screen.getByRole("button", { name: "Add Filter" }).focus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(screen.getByRole("searchbox", { name: "Filter by" })).toHaveFocus());
  });

  it("moves from its search down to the matches and back up, and checks twice without closing", async () => {
    const user = userEvent.setup();
    const onToggle = vi.fn();
    render(<Harness onToggle={onToggle} />);
    const search = await openMenu(user);
    await user.type(search, "example");
    await user.keyboard("{ArrowDown}");
    const first = screen.getByRole("menuitemcheckbox", { name: "Repository example/app" });
    expect(first).toHaveFocus();
    await user.keyboard("{ArrowUp}");
    expect(search).toHaveFocus();
    await user.keyboard("{Enter}{Enter}");
    expect(onToggle).toHaveBeenCalledTimes(2);
    expect(first).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("menu")).toBeInTheDocument();
  });

  it("goes back to its search when a letter is typed on a match", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const search = await openMenu(user);
    await user.type(search, "ex");
    await user.keyboard("{ArrowDown}a");
    expect(search).toHaveFocus();
    expect(search).toHaveValue("exa");
  });

  it("closes on one Escape, query and all, and gives focus back to its button", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const search = await openMenu(user);
    await user.type(search, "docs");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(screen.getByRole("button", { name: "Add Filter" })).toHaveFocus();
    // The next opening starts from an empty search.
    expect(await openMenu(user)).toHaveValue("");
  });

  it("leaves composing Enter, arrows and Escape to the input method", async () => {
    const user = userEvent.setup();
    const onToggle = vi.fn();
    render(<Harness onToggle={onToggle} />);
    const search = await openMenu(user);
    await user.type(search, "docs");
    fireEvent.keyDown(search, { key: "Enter", isComposing: true });
    fireEvent.keyDown(search, { key: "Enter", keyCode: 229 });
    fireEvent.keyDown(search, { key: "ArrowDown", isComposing: true });
    fireEvent.keyDown(search, { key: "Escape", keyCode: 229 });
    expect(onToggle).not.toHaveBeenCalled();
    expect(search).toHaveFocus();
    expect(search).toHaveValue("docs");
    expect(screen.getByRole("menu")).toBeInTheDocument();
    fireEvent.keyDown(search, { key: "Enter" });
    expect(onToggle).toHaveBeenCalledTimes(1);
    expect(onToggle).toHaveBeenCalledWith("repository", "example/docs");
  });

  it("opens one category alone with its search focused and checks its first match with Enter", async () => {
    const user = userEvent.setup();
    const onToggle = vi.fn();
    render(<Harness onToggle={onToggle} initial={MANY} only="repository" />);
    const search = await screen.findByRole("searchbox", { name: "Search Repository" });
    await waitFor(() => expect(search).toHaveFocus());
    await user.type(search, "repo-9");
    await user.keyboard("{Enter}");
    expect(onToggle).toHaveBeenCalledWith("repository", "example/repo-9");
    expect(screen.getByRole("menuitemcheckbox", { name: "example/repo-9" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });
});

describe("FilterMenu's submenus", () => {
  it("search past 8 values only, and start empty each time they open", async () => {
    const user = userEvent.setup();
    const eight: FilterMenuCategory[] = [
      { id: "repository", label: "Repository", options: repositories(8) },
      ...MANY,
    ].map((category, index) =>
      index === 1 ? { ...category, id: "many", label: "Many" } : category,
    );
    render(<Harness initial={eight} />);
    await openMenu(user);
    await user.keyboard("{ArrowDown}{ArrowRight}");
    await screen.findByRole("menuitemcheckbox", { name: "example/repo-8" });
    expect(screen.queryByRole("searchbox", { name: "Search Repository" })).toBeNull();
    await user.keyboard("{ArrowLeft}{ArrowDown}{ArrowRight}");
    const search = await screen.findByRole("searchbox", { name: "Search Many" });
    await waitFor(() => expect(search).toHaveFocus());
    await user.type(search, "repo-9");
    expect(screen.getAllByRole("menuitemcheckbox").map((item) => item.textContent)).toEqual([
      "example/repo-9",
    ]);
    // Left moves the caret through a query instead of closing the submenu.
    await user.keyboard("{ArrowLeft}{ArrowLeft}");
    expect(search).toHaveFocus();
    // Down to the values, Left closes the submenu back to its category; reopened, it's empty.
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitemcheckbox", { name: "example/repo-9" })).toHaveFocus();
    await user.keyboard("{ArrowLeft}");
    await waitFor(() => expect(screen.getByRole("menuitem", { name: /^Many/ })).toHaveFocus());
    expect(screen.getByRole("searchbox", { name: "Filter by" })).toHaveValue("");
    await user.keyboard("{ArrowRight}");
    expect(await screen.findByRole("searchbox", { name: "Search Many" })).toHaveValue("");
  });

  it("keep their own typeahead without a search, leaving the menu's search alone", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const search = await openMenu(user);
    await user.keyboard("{ArrowDown}{ArrowRight}");
    const app = await screen.findByRole("menuitemcheckbox", { name: "example/app" });
    await waitFor(() => expect(app).toHaveFocus());
    await user.keyboard("e");
    expect(search).toHaveValue("");
    expect(screen.getByRole("menuitemcheckbox", { name: "example/docs" })).toBeInTheDocument();
  });

  it("leave composing keys in their search to the input method", async () => {
    const user = userEvent.setup();
    const onToggle = vi.fn();
    render(<Harness onToggle={onToggle} initial={MANY} />);
    await openMenu(user);
    await user.keyboard("{ArrowDown}{ArrowRight}");
    const search = await screen.findByRole("searchbox", { name: "Search Repository" });
    await waitFor(() => expect(search).toHaveFocus());
    await user.type(search, "repo-2");
    fireEvent.keyDown(search, { key: "Enter", isComposing: true });
    fireEvent.keyDown(search, { key: "ArrowDown", keyCode: 229 });
    fireEvent.keyDown(search, { key: "Escape", isComposing: true });
    expect(onToggle).not.toHaveBeenCalled();
    expect(search).toHaveFocus();
    fireEvent.keyDown(search, { key: "Enter" });
    expect(onToggle).toHaveBeenCalledWith("repository", "example/repo-2");
    expect(search).toBeInTheDocument();
  });
});

describe("FilterChip's menus", () => {
  it("search a chip's values past 8 and check them in place, focus going back to the chip", async () => {
    const user = userEvent.setup();
    const onToggle = vi.fn();
    render(
      <FilterChip
        field="Repository"
        operator="is"
        values="example/repo-1"
        category={MANY[0]!}
        onToggle={onToggle}
        onRemove={vi.fn()}
        removeLabel="Remove Repository filter"
      />,
    );
    const values = screen.getByRole("button", { name: "Repository is example/repo-1" });
    await user.click(values);
    const search = await screen.findByRole("searchbox", { name: "Search Repository" });
    await waitFor(() => expect(search).toHaveFocus());
    await user.type(search, "repo-3");
    await user.keyboard("{Enter}");
    expect(onToggle).toHaveBeenCalledWith("repository", "example/repo-3");
    expect(screen.getByRole("menu")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(values).toHaveFocus());
  });

  it("give focus back to the operator once one is picked", async () => {
    const user = userEvent.setup();
    render(
      <FilterChip
        field="Label"
        operator="is any of"
        operators={[
          { id: "anyOf", label: "is any of" },
          { id: "noneOf", label: "is none of" },
        ]}
        operatorId="anyOf"
        onOperatorChange={vi.fn()}
        values="bug"
        category={CATEGORIES[0]!}
        onToggle={vi.fn()}
        onRemove={vi.fn()}
        removeLabel="Remove Label Filter"
      />,
    );
    const operator = screen.getByRole("button", { name: "Label is any of, change how it matches" });
    await user.click(operator);
    await user.click(await screen.findByRole("menuitemradio", { name: "is none of" }));
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(operator).toHaveFocus();
  });
});

describe("FilterChip", () => {
  it("opens its values to change in place and removes itself", async () => {
    const user = userEvent.setup();
    const onToggle = vi.fn();
    const onRemove = vi.fn();
    render(
      <FilterChip
        field="Repository"
        operator="is"
        values="example/app"
        category={CATEGORIES[0]!}
        onToggle={onToggle}
        onRemove={onRemove}
        removeLabel="Remove Repository filter"
      />,
    );
    await user.click(screen.getByRole("button", { name: "Repository is example/app" }));
    await user.click(await screen.findByRole("menuitemcheckbox", { name: /example\/docs/ }));
    expect(onToggle).toHaveBeenCalledWith("repository", "example/docs");
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Remove Repository filter" }));
    expect(onRemove).toHaveBeenCalled();
  });
});

describe("FilterMenu's category options", () => {
  const categories: FilterMenuCategory[] = [
    {
      id: "label",
      label: "Label",
      note: "Details still loading for 2 pull requests.",
      options: [{ id: "bug", label: "bug", count: 3, checked: false }],
    },
    {
      id: "exclude-label",
      label: "Exclude Label",
      omitFromSearch: true,
      options: [{ id: "bug", label: "bug", count: 3, checked: false }],
    },
    {
      id: "assignee",
      label: "Assignee",
      disabledReason: "Update the desktop app to filter by this",
      options: [],
    },
  ];

  it("shows a category that can't be used disabled, with why, and a note under its values", async () => {
    const user = userEvent.setup();
    render(
      <FilterMenu categories={categories} onToggle={vi.fn()} trigger={<FilterMenuButton />} />,
    );
    await user.click(screen.getByRole("button", { name: "Add Filter" }));
    const assignee = await screen.findByRole("menuitem", { name: /Assignee/ });
    expect(assignee).toHaveAttribute("aria-disabled", "true");
    expect(assignee).toHaveTextContent("Update the desktop app to filter by this");
    await user.click(screen.getByRole("menuitem", { name: "Label" }));
    expect(await screen.findByText("Details still loading for 2 pull requests.")).toBeVisible();
  });

  it("lists a value to require once as it's typed, leaving its exclusion to its submenu", async () => {
    const user = userEvent.setup();
    render(
      <FilterMenu categories={categories} onToggle={vi.fn()} trigger={<FilterMenuButton />} />,
    );
    await user.click(screen.getByRole("button", { name: "Add Filter" }));
    await user.type(await screen.findByRole("searchbox", { name: "Filter by" }), "bug");
    expect(
      screen.getAllByRole("menuitemcheckbox").map((item) => item.getAttribute("aria-label")),
    ).toEqual(["Label bug"]);
  });
});

describe("FilterChip's operator", () => {
  it("opens the other ways it can match and picks one", async () => {
    const user = userEvent.setup();
    const onOperatorChange = vi.fn();
    render(
      <FilterChip
        field="Label"
        operator="is any of"
        operators={[
          { id: "anyOf", label: "is any of" },
          { id: "allOf", label: "is all of" },
          { id: "noneOf", label: "is none of" },
        ]}
        operatorId="anyOf"
        onOperatorChange={onOperatorChange}
        values="2 labels"
        category={CATEGORIES[0]!}
        onToggle={vi.fn()}
        onRemove={vi.fn()}
        removeLabel="Remove Label Filter"
      />,
    );
    await user.click(
      screen.getByRole("button", { name: "Label is any of, change how it matches" }),
    );
    expect(await screen.findByRole("menuitemradio", { name: "is any of" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    await user.click(screen.getByRole("menuitemradio", { name: "is none of" }));
    expect(onOperatorChange).toHaveBeenCalledWith("noneOf");
  });
});

describe("FilterChip without values", () => {
  it("shows its label and × alone, and removes itself", async () => {
    const user = userEvent.setup();
    const onRemove = vi.fn();
    render(<FilterChip field="Owned by you" onRemove={onRemove} removeLabel="Show All Files" />);
    expect(screen.getByText("Owned by you")).toBeVisible();
    // Just one control: no values to open.
    expect(
      screen.getAllByRole("button").map((button) => button.getAttribute("aria-label")),
    ).toEqual(["Show All Files"]);
    await user.click(screen.getByRole("button", { name: "Show All Files" }));
    expect(onRemove).toHaveBeenCalled();
  });

  it("can keep × hidden until hover or focus, still tabbable and in its place", async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <FilterChip field="Owned by you" onRemove={vi.fn()} removeLabel="Show All Files" />,
    );
    const always = screen.getByRole("button", { name: "Show All Files" });
    expect(always.className).not.toContain("opacity-0");
    rerender(
      <FilterChip
        field="Owned by you"
        onRemove={vi.fn()}
        removeLabel="Show All Files"
        removeOnHover
      />,
    );
    const remove = screen.getByRole("button", { name: "Show All Files" });
    expect(remove.className).toContain("opacity-0");
    expect(remove.className).toContain("group-hover/filter-chip:opacity-100");
    expect(remove.className).toContain("group-focus-within/filter-chip:opacity-100");
    await user.tab();
    expect(remove).toHaveFocus();
  });
});

describe("filterMenuMatches", () => {
  it("ignores case and accents", () => {
    expect(filterMenuMatches("Café/App", "cafe")).toBe(true);
    expect(filterMenuMatches("example/app", " APP ")).toBe(true);
    expect(filterMenuMatches("example/app", "docs")).toBe(false);
  });
});
