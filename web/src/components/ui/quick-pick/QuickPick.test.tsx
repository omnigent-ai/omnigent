import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { QuickPickItem } from "./model";
import type { QuickPickSegment } from "./presentation";
import { QuickPick, type QuickPickProps } from "./QuickPick";

afterEach(cleanup);

function item(id: string, overrides: Partial<QuickPickItem<string>> = {}): QuickPickItem<string> {
  return { id, label: id, value: id, ...overrides };
}

const commands: QuickPickSegment<string> = {
  sourceId: "commands",
  ranking: "client",
  fuzzyMatching: "words",
  items: [
    item("new-session", { label: "New session", group: "Actions", shortcut: ["⌘", "N"] }),
    item("open-settings", { label: "Open settings", group: "Actions" }),
  ],
};

type Props = Partial<QuickPickProps<string>>;

function Harness(props: Props) {
  const [query, setQuery] = useState(props.query ?? "");
  return (
    <QuickPick
      label="Command palette"
      segments={[commands]}
      emptyMessage="No results found"
      onAccept={vi.fn()}
      {...props}
      query={query}
      onQueryChange={setQuery}
    />
  );
}

function renderPicker(props: Props = {}) {
  const onAccept = props.onAccept ?? vi.fn();
  const view = render(<Harness {...props} onAccept={onAccept} />);
  const input = screen.getByRole("combobox", { name: "Command palette" });
  return { ...view, input, onAccept };
}

const optionNames = () => screen.getAllByRole("option").map((option) => option.textContent);
const activeOption = (input: HTMLElement) =>
  document.getElementById(input.getAttribute("aria-activedescendant") ?? "");

describe("QuickPick accessibility", () => {
  it("wires the query field to the listbox as a combobox", () => {
    const { input } = renderPicker();
    const listbox = screen.getByRole("listbox", { name: "Command palette" });
    expect(input.getAttribute("aria-controls")).toBe(listbox.id);
    expect(input.getAttribute("aria-autocomplete")).toBe("list");
    expect(input.getAttribute("aria-expanded")).toBe("true");
    expect(document.activeElement).toBe(input);
  });

  it("points aria-activedescendant at the selected option", () => {
    const { input } = renderPicker();
    const active = activeOption(input);
    expect(active?.textContent).toContain("New session");
    expect(active?.getAttribute("aria-selected")).toBe("true");
    const others = screen.getAllByRole("option").filter((option) => option !== active);
    expect(others.every((option) => option.getAttribute("aria-selected") === "false")).toBe(true);
  });

  it("names each group by its heading", () => {
    renderPicker();
    const group = screen.getByRole("group", { name: "Actions" });
    expect(within(group).getAllByRole("option")).toHaveLength(2);
  });

  it("marks disabled options and announces the result count", () => {
    renderPicker({
      segments: [
        {
          ...commands,
          items: [...commands.items, item("archive", { disabledReason: "Open a session first" })],
        },
      ],
    });
    const disabled = screen.getByRole("option", { name: /archive/ });
    expect(disabled.getAttribute("aria-disabled")).toBe("true");
    expect(disabled.textContent).toContain("Open a session first");
    expect(screen.getByRole("status").textContent).toBe("3 results");
  });
});

describe("QuickPick rows", () => {
  it("keeps an option's ID when rows above it change", () => {
    const { input } = renderPicker();
    const openSettingsId = screen.getByRole("option", { name: /Open settings/ }).id;
    fireEvent.change(input, { target: { value: "ope" } });
    expect(optionNames()).toEqual(["Open settings"]);
    expect(screen.getByRole("option", { name: /Open settings/ }).id).toBe(openSettingsId);
  });

  it("ranks client rows, highlights the match, and shows shortcut caps", () => {
    const { input } = renderPicker();
    fireEvent.change(input, { target: { value: "sett" } });
    expect(optionNames()).toEqual(["Open settings"]);
    const marks = [...screen.getByRole("option").querySelectorAll("mark")];
    expect(marks.map((mark) => mark.textContent)).toEqual(["sett"]);
    fireEvent.change(input, { target: { value: "" } });
    expect(screen.getByRole("option", { name: /New session/ }).textContent).toContain("⌘N");
  });

  it("keeps provider rows in order with a second line and trailing accessory", () => {
    renderPicker({
      query: "kestrel",
      segments: [
        {
          sourceId: "search",
          ranking: "provider",
          items: [
            item("b", {
              label: "Weekly sync",
              description: "the kestrel checklist",
              group: "In messages",
            }),
            item("a", {
              label: "Kestrel plan",
              accessory: <span>Archived</span>,
              group: "In messages",
            }),
          ],
        },
      ],
    });
    const [first, second] = screen.getAllByRole("option");
    expect(first.textContent).toContain("Weekly sync");
    expect(first.textContent).toContain("the kestrel checklist");
    expect(second.textContent).toContain("Archived");
  });

  it("shows the empty message and the caller's slots", () => {
    const { input } = renderPicker({
      listEnd: <button type="button">Search older sessions</button>,
      inputEnd: <button type="button">Close search</button>,
    });
    fireEvent.change(input, { target: { value: "zzzz" } });
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    expect(screen.getByRole("status").textContent).toBe("No results found");
    expect(screen.getByRole("button", { name: "Search older sessions" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Close search" })).toBeTruthy();
  });

  it("shows the caller's status above the rows and still announces the count", () => {
    const { input } = renderPicker({
      status: (
        <p>
          <span role="status">Couldn't load sessions.</span>
          <button type="button">Retry</button>
        </p>
      ),
    });
    const announced = () => screen.getAllByRole("status").map((region) => region.textContent);
    expect(announced()).toEqual(["Couldn't load sessions.", "2 results"]);
    expect(screen.getByRole("button", { name: "Retry" }).closest('[role="status"]')).toBeNull();
    fireEvent.change(input, { target: { value: "zzzz" } });
    expect(announced()).toEqual(["Couldn't load sessions.", "No results found"]);
  });

  it("shows nothing for an empty list when the caller passes no empty message", () => {
    const { input } = renderPicker({ emptyMessage: null });
    fireEvent.change(input, { target: { value: "zzzz" } });
    expect(screen.getByRole("status").textContent).toBe("");
    expect(screen.getByRole("status").className).toContain("sr-only");
  });
});

describe("QuickPick active row", () => {
  const titles = (ids: string[]): QuickPickSegment<string> => ({
    sourceId: "titles",
    ranking: "provider",
    items: ids.map((id) => item(id, { group: "Sessions" })),
  });

  it("keeps the top row active as results arrive above it, until the user picks one", () => {
    const { input, rerender, onAccept } = renderPicker({ segments: [commands] });
    expect(activeOption(input)?.textContent).toContain("New session");
    rerender(<Harness segments={[titles(["kestrel"]), commands]} onAccept={onAccept} />);
    expect(activeOption(input)?.textContent).toBe("kestrel");

    fireEvent.pointerMove(screen.getByRole("option", { name: /Open settings/ }));
    rerender(<Harness segments={[titles(["kestrel", "raven"]), commands]} onAccept={onAccept} />);
    expect(activeOption(input)?.textContent).toContain("Open settings");
  });

  it("returns to the top row when the query changes", () => {
    const { input } = renderPicker();
    fireEvent.pointerMove(screen.getByRole("option", { name: /Open settings/ }));
    expect(activeOption(input)?.textContent).toContain("Open settings");
    fireEvent.change(input, { target: { value: "s" } });
    expect(activeOption(input)?.textContent).toContain("New session");
  });

  it("falls back to the top row when the picked row goes away", () => {
    const { input, rerender, onAccept } = renderPicker({ segments: [titles(["a", "b"])] });
    fireEvent.pointerMove(screen.getByRole("option", { name: "b" }));
    rerender(<Harness segments={[titles(["a"])]} onAccept={onAccept} />);
    expect(activeOption(input)?.textContent).toBe("a");
  });
});

describe("QuickPick pointer and query picks", () => {
  it("forgets a pick when the query changes, even back to an earlier query", () => {
    const { input } = renderPicker();
    fireEvent.pointerMove(screen.getByRole("option", { name: /Open settings/ }));
    fireEvent.change(input, { target: { value: "s" } });
    fireEvent.change(input, { target: { value: "" } });
    expect(activeOption(input)?.textContent).toContain("New session");
  });

  it("forgets a pick when the caller changes the query", () => {
    const controlled = (query: string) => (
      <QuickPick
        label="Command palette"
        segments={[commands]}
        emptyMessage="No results found"
        onAccept={vi.fn()}
        query={query}
        onQueryChange={vi.fn()}
      />
    );
    const { rerender } = render(controlled(""));
    const input = screen.getByRole("combobox");
    fireEvent.pointerMove(screen.getByRole("option", { name: /Open settings/ }));
    rerender(controlled("s"));
    rerender(controlled(""));
    expect(activeOption(input)?.textContent).toContain("New session");
  });

  it("counts hovering the top row as a pick, so an earlier result arriving later doesn't take over", () => {
    const search = (ids: string[]): QuickPickSegment<string> => ({
      sourceId: "titles",
      ranking: "provider",
      items: ids.map((id) => item(id)),
    });
    const { input, rerender, onAccept } = renderPicker({ segments: [search(["b"])] });
    fireEvent.pointerMove(screen.getByRole("option", { name: "b" }));
    rerender(<Harness segments={[search(["a", "b"])]} onAccept={onAccept} />);
    expect(activeOption(input)?.textContent).toBe("b");
  });

  it("does not let a disabled row take the selection on hover", () => {
    const { input, onAccept } = renderPicker({
      segments: [
        {
          ...commands,
          items: [...commands.items, item("archive", { disabledReason: "Open a session first" })],
        },
      ],
    });
    fireEvent.pointerMove(screen.getByRole("option", { name: /archive/ }));
    expect(activeOption(input)?.textContent).toContain("New session");
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onAccept).toHaveBeenCalledWith(expect.objectContaining({ id: "new-session" }));
  });
});

describe("QuickPick caller-chosen active row", () => {
  it("activates the caller's row until the user moves, then reports the move", () => {
    const onActiveIdChange = vi.fn();
    const { input, rerender, onAccept } = renderPicker({
      activeId: "open-settings",
      onActiveIdChange,
    });
    expect(activeOption(input)?.textContent).toContain("Open settings");
    fireEvent.pointerMove(screen.getByRole("option", { name: /New session/ }));
    expect(onActiveIdChange).toHaveBeenCalledWith("new-session");
    // The caller clears its row once told about the move, and the moved row stays active.
    rerender(<Harness onAccept={onAccept} activeId={null} onActiveIdChange={onActiveIdChange} />);
    expect(activeOption(input)?.textContent).toContain("New session");
  });
});

describe("QuickPick groups", () => {
  it("keeps ungrouped rows out of the group above them", () => {
    renderPicker({
      segments: [
        { sourceId: "titles", ranking: "provider", items: [item("s1", { group: "Sessions" })] },
        { sourceId: "commands", ranking: "client", items: [item("search", { label: "Search" })] },
      ],
    });
    const sessions = screen.getByRole("group", { name: "Sessions" });
    expect(within(sessions).getByRole("option", { name: "s1" })).toBeTruthy();
    expect(within(sessions).queryByRole("option", { name: "Search" })).toBeNull();
    expect(screen.getByRole("option", { name: "Search" })).toBeTruthy();
  });
});

describe("QuickPick accepting", () => {
  it("accepts the active row with Enter and a clicked row by pointer", () => {
    const { input, onAccept } = renderPicker();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onAccept).toHaveBeenLastCalledWith(expect.objectContaining({ id: "new-session" }));
    const settings = screen.getByRole("option", { name: /Open settings/ });
    // jsdom never moves focus on click, so check the press can't take focus from the query.
    expect(fireEvent.mouseDown(settings)).toBe(false);
    fireEvent.click(settings);
    expect(onAccept).toHaveBeenLastCalledWith(expect.objectContaining({ id: "open-settings" }));
  });

  it("leaves Enter to an IME composition", () => {
    const { input, onAccept } = renderPicker();
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
    expect(onAccept).not.toHaveBeenCalled();
  });

  it("never accepts a disabled row", () => {
    const { input, onAccept } = renderPicker({
      segments: [
        {
          sourceId: "commands",
          ranking: "client",
          items: [item("archive", { disabledReason: "Open a session first" })],
        },
      ],
    });
    fireEvent.click(screen.getByRole("option", { name: /archive/ }));
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onAccept).not.toHaveBeenCalled();
  });
});

describe("QuickPick sidebar presentation", () => {
  const sidebarSegments: QuickPickSegment<string>[] = [
    {
      sourceId: "settings",
      ranking: "client",
      items: [
        item("steer", {
          label: "Always steer",
          description: "General",
          accessory: <kbd>⌘S</kbd>,
        }),
        item("theme", { label: "Terminal theme", description: "Appearance" }),
      ],
    },
  ];

  function renderSidebar(props: Props = {}) {
    render(
      <Harness
        label="Search settings"
        segments={sidebarSegments}
        emptyMessage="No results found"
        presentation="sidebar"
        autoFocus={false}
        {...props}
      />,
    );
    return screen.getByRole("combobox", { name: "Search settings" });
  }

  it("shows only its field until there is a query", () => {
    const input = renderSidebar();
    expect(input).toHaveAttribute("aria-expanded", "false");
    expect(input).not.toHaveAttribute("aria-controls");
    expect(screen.getByRole("listbox", { hidden: true }).closest(".hidden")).not.toBeNull();
    expect(screen.queryByRole("button", { name: "Clear search" })).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("");

    fireEvent.change(input, { target: { value: "steer" } });
    expect(input).toHaveAttribute("aria-expanded", "true");
    expect(input).toHaveAttribute("aria-controls", screen.getByRole("listbox").id);
    expect(screen.getByRole("status")).toHaveTextContent("1 result");
  });

  it("accepts nothing while collapsed", () => {
    const onAccept = vi.fn();
    const input = renderSidebar({ onAccept });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onAccept).not.toHaveBeenCalled();
  });

  it("lists two-line rows with their place and accessory", () => {
    const input = renderSidebar();
    fireEvent.change(input, { target: { value: "steer" } });
    const option = screen.getByRole("option");
    expect(option).toHaveTextContent("Always steer");
    expect(option).toHaveTextContent("General");
    expect(within(option).getByText("⌘S")).toBeInTheDocument();
    expect(screen.getByRole("option").querySelector("[data-slot=quick-pick-label]")).not.toBeNull();
  });

  it("clears the query with its button and keeps focus in the field", () => {
    const input = renderSidebar();
    fireEvent.change(input, { target: { value: "theme" } });
    fireEvent.click(screen.getByRole("button", { name: "Clear search" }));
    expect(input).toHaveValue("");
    expect(input).toHaveFocus();
    expect(input).toHaveAttribute("aria-expanded", "false");
  });

  it("leaves Escape to the caller", () => {
    const onKeyDown = vi.fn();
    render(
      <div onKeyDown={onKeyDown}>
        <Harness
          label="Search settings"
          segments={sidebarSegments}
          emptyMessage={null}
          presentation="sidebar"
          autoFocus={false}
        />
      </div>,
    );
    const input = screen.getByRole("combobox", { name: "Search settings" });
    fireEvent.change(input, { target: { value: "theme" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(onKeyDown).toHaveBeenCalledTimes(1);
  });
});
