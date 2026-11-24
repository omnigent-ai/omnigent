import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { readQuickPickHistory, recordQuickPickHistory } from "./history";
import type { QuickPickItem, QuickPickSource } from "./model";
import type { QuickPickSegment } from "./presentation";
import { formatQuickPickSummary, QuickPick, type QuickPickProps } from "./QuickPick";

afterEach(() => {
  cleanup();
  localStorage.clear();
});

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

describe("QuickPick keyboard", () => {
  it("steps with Ctrl-N and Ctrl-P like the arrows once opted in, skipping disabled rows", async () => {
    const { input, onAccept } = renderPicker({
      stepsWithCtrlNP: true,
      segments: [
        {
          ...commands,
          items: [
            commands.items[0],
            item("archive", {
              label: "Archive session",
              group: "Actions",
              disabledReason: "Open a session first",
            }),
            commands.items[1],
          ],
        },
      ],
    });
    await userEvent.keyboard("{Control>}n{/Control}");
    expect(activeOption(input)).toHaveTextContent("Open settings");
    await userEvent.keyboard("{Control>}p{/Control}");
    expect(activeOption(input)).toHaveTextContent("New session");
    await userEvent.keyboard("{Control>}n{/Control}");
    expect(input).toHaveValue("");
    await userEvent.keyboard("{Enter}");
    expect(onAccept).toHaveBeenCalledWith(commands.items[1], expect.anything());
  });

  it("leaves ⌘N and Ctrl-Shift-N alone when opted in", () => {
    const { input } = renderPicker({ stepsWithCtrlNP: true });
    for (const chord of [{ metaKey: true }, { ctrlKey: true, shiftKey: true }]) {
      expect(fireEvent.keyDown(input, { key: "n", code: "KeyN", ...chord })).toBe(true);
      expect(activeOption(input)).toHaveTextContent("New session");
    }
  });

  it("marks its query field as an open pick list's only when it steps with Ctrl-N/P", () => {
    const { input, unmount } = renderPicker({ stepsWithCtrlNP: true });
    expect(input).toHaveAttribute("data-pick-list-input");
    unmount();
    expect(renderPicker().input).not.toHaveAttribute("data-pick-list-input");
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
    expect(onAccept).toHaveBeenCalledWith(expect.objectContaining({ id: "new-session" }), {
      alternate: false,
    });
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

describe("QuickPick shown active row", () => {
  it("reports each row that becomes active, by keys or as a query filters the list", () => {
    const onActiveRowShown = vi.fn();
    const { input } = renderPicker({ onActiveRowShown });
    expect(onActiveRowShown).toHaveBeenLastCalledWith("new-session");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(onActiveRowShown).toHaveBeenLastCalledWith("open-settings");
    fireEvent.keyDown(input, { key: "ArrowUp" });
    // A query that leaves another row on top shows it, though the user didn't move.
    fireEvent.change(input, { target: { value: "settings" } });
    expect(onActiveRowShown).toHaveBeenLastCalledWith("open-settings");
    // Nothing matching reports nothing, so a preview keeps the last row.
    onActiveRowShown.mockClear();
    fireEvent.change(input, { target: { value: "zzz" } });
    expect(onActiveRowShown).not.toHaveBeenCalled();
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
    expect(onAccept).toHaveBeenLastCalledWith(expect.objectContaining({ id: "new-session" }), {
      alternate: false,
    });
    const settings = screen.getByRole("option", { name: /Open settings/ });
    // jsdom never moves focus on click, so check the press can't take focus from the query.
    expect(fireEvent.mouseDown(settings)).toBe(false);
    fireEvent.click(settings);
    expect(onAccept).toHaveBeenLastCalledWith(expect.objectContaining({ id: "open-settings" }), {
      alternate: false,
    });
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

describe("QuickPick sections", () => {
  it("keeps consecutive ungrouped rows in one section, apart from a group after them", () => {
    renderPicker({
      segments: [
        {
          sourceId: "results",
          ranking: "client",
          items: [item("one"), item("two"), item("three"), item("grouped", { group: "Actions" })],
        },
      ],
    });
    const sections = screen.getAllByRole("group");
    expect(sections.map((section) => within(section).getAllByRole("option").length)).toEqual([
      3, 1,
    ]);
  });
});

describe("QuickPick history", () => {
  it("records accepted items and lists them first next time", () => {
    const { input, onAccept } = renderPicker({ historyKey: "commands" });
    fireEvent.keyDown(input, { key: "End" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onAccept).toHaveBeenCalled();
    expect(readQuickPickHistory("commands")).toEqual(["open-settings"]);
    cleanup();

    const next = renderPicker({ historyKey: "commands" });
    const recent = screen.getByRole("group", { name: "Recent" });
    expect(
      within(recent)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Open settings"]);
    expect(activeOption(next.input)).toHaveTextContent("Open settings");
  });

  it("reads recents again when the namespace changes", () => {
    recordQuickPickHistory("commands", "open-settings");
    recordQuickPickHistory("sessions", "new-session");
    const { input, rerender } = renderPicker({ historyKey: "commands" });
    expect(activeOption(input)).toHaveTextContent("Open settings");
    rerender(<Harness historyKey="sessions" />);
    expect(activeOption(input)).toHaveTextContent("New session");
  });

  it("refreshes recents after an accept that keeps the picker open", () => {
    const { input } = renderPicker({ historyKey: "commands", onAccept: () => "keepOpen" });
    expect(screen.queryByRole("group", { name: "Recent" })).not.toBeInTheDocument();
    fireEvent.keyDown(input, { key: "End" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByRole("group", { name: "Recent" })).toBeVisible();
    expect(activeOption(input)).toHaveTextContent("Open settings");
  });

  it("does not remember an item that opts out", () => {
    const { input } = renderPicker({
      historyKey: "commands",
      segments: [{ ...commands, items: [item("once", { rememberInHistory: false })] }],
    });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(readQuickPickHistory("commands")).toEqual([]);
  });

  it("ignores history for items that no longer exist", () => {
    recordQuickPickHistory("commands", "removed");
    renderPicker({ historyKey: "commands" });
    expect(screen.queryByRole("group", { name: "Recent" })).not.toBeInTheDocument();
  });
});

describe("QuickPick async sources", () => {
  it("shows loading, then the loaded rows", async () => {
    let resolve: (items: QuickPickItem<string>[]) => void = () => {};
    const source: QuickPickSource<string> = {
      id: "remote",
      load: () =>
        new Promise((settle) => {
          resolve = settle;
        }),
    };
    renderPicker({ segments: [], sources: [source] });
    expect(screen.getByRole("status")).toHaveTextContent("Loading results…");
    await act(async () => resolve([item("remote-1")]));
    expect(screen.getByRole("option", { name: "remote-1" })).toBeVisible();
    expect(screen.getByRole("status").textContent).toBe("1 result");
  });

  it("explains a failure and recovers with Try again", async () => {
    let attempts = 0;
    const source: QuickPickSource<string> = {
      id: "remote",
      load: async () => {
        attempts += 1;
        if (attempts === 1) throw new Error("Couldn't reach the server.");
        return [item("recovered")];
      },
    };
    renderPicker({ segments: [], sources: [source] });
    expect(await screen.findByRole("status")).toHaveTextContent("Couldn't reach the server.");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("option", { name: "recovered" })).toBeVisible();
  });

  it("returns focus to the query when Try again is pressed from the keyboard", async () => {
    let attempts = 0;
    const source: QuickPickSource<string> = {
      id: "remote",
      load: async () => {
        attempts += 1;
        if (attempts === 1) throw new Error("Couldn't reach the server.");
        return [item("recovered")];
      },
    };
    const { input } = renderPicker({ segments: [], sources: [source] });
    const retry = await screen.findByRole("button", { name: "Try again" });
    retry.focus();
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByRole("option", { name: "recovered" })).toBeVisible();
    expect(document.activeElement).toBe(input);
  });

  it("keeps the rows it has and offers Try again beside them after a later failure", async () => {
    const source: QuickPickSource<string> = {
      id: "remote",
      queryDependent: true,
      load: async ({ query }) => {
        if (query) throw new Error("Search failed.");
        return [item("first-result")];
      },
    };
    const { input } = renderPicker({ segments: [], sources: [source] });
    expect(await screen.findByRole("option", { name: "first-result" })).toBeVisible();
    fireEvent.change(input, { target: { value: "first" } });
    expect(await screen.findByText("Search failed.")).toBeVisible();
    expect(screen.getByRole("option", { name: "first-result" })).toBeVisible();
    // Screen readers hear the failure too, with the rows that remain.
    expect(screen.getByRole("status")).toHaveTextContent("1 result. Search failed.");
    expect(screen.getByRole("button", { name: "Try again" })).toBeVisible();
  });

  it("lists the sources' rows first with sourcesFirst", () => {
    const source: QuickPickSource<string> = {
      id: "sessions",
      ranking: "provider",
      load: () => [item("Recent session")],
    };
    renderPicker({ sources: [source], sourcesFirst: true });
    expect(optionNames()[0]).toBe("Recent session");
  });
});

describe("QuickPick footer and alternate accept", () => {
  it("shows no footer for an inline picker that can't close", () => {
    renderPicker();
    expect(document.querySelector("[data-slot=quick-pick-footer]")).toBeNull();
  });

  it("summarizes the results and runs Select and Close from its hints", () => {
    const onClose = vi.fn();
    const { onAccept } = renderPicker({ onClose, summary: formatQuickPickSummary });
    expect(document.querySelector("[data-slot=quick-pick-summary]")).toHaveTextContent("2 results");
    fireEvent.click(screen.getByRole("button", { name: "Select (Enter)" }));
    expect(onAccept).toHaveBeenCalledWith(expect.objectContaining({ id: "new-session" }), {
      alternate: false,
    });
    fireEvent.click(screen.getByRole("button", { name: "Close (Escape)" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("accepts the active row as alternate with the command modifier or its hint", () => {
    const { input, onAccept } = renderPicker({ onClose: vi.fn(), alternateLabel: "Open beside" });
    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true });
    expect(onAccept).toHaveBeenLastCalledWith(expect.objectContaining({ id: "new-session" }), {
      alternate: true,
    });
    fireEvent.click(screen.getByRole("button", { name: /^Open beside/ }));
    expect(onAccept).toHaveBeenCalledTimes(2);
    expect(onAccept).toHaveBeenLastCalledWith(expect.anything(), { alternate: true });
  });

  it("runs the picker's own alternate instead of the row when it has one", () => {
    const onAlternate = vi.fn();
    const { input, onAccept } = renderPicker({ onAlternate });
    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true });
    expect(onAlternate).toHaveBeenCalledTimes(1);
    expect(onAccept).not.toHaveBeenCalled();
  });

  it("names the active row's alternate, and hides the hint for a row without one", () => {
    const { input } = renderPicker({
      onClose: vi.fn(),
      alternateLabel: (row) => (row.id === "new-session" ? "Open beside" : undefined),
    });
    expect(screen.getByRole("button", { name: /^Open beside/ })).toBeVisible();
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(screen.queryByRole("button", { name: /^Open beside/ })).toBeNull();
  });

  it("dismisses with Escape outside a dialog", () => {
    const onDismiss = vi.fn();
    const { input } = renderPicker({ onDismiss });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("dismisses with Escape from a control among the results, returning focus to the query", () => {
    const onDismiss = vi.fn();
    const { input } = renderPicker({
      onDismiss,
      listEnd: <button type="button">Show more</button>,
    });
    const showMore = screen.getByRole("button", { name: "Show more" });
    showMore.focus();
    fireEvent.keyDown(showMore, { key: "Escape" });
    expect(onDismiss).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(input);
  });

  it("formats loading and timing in the default summary", () => {
    expect(formatQuickPickSummary({ resultCount: 2000, loading: false, elapsedMs: 41.6 })).toBe(
      "2,000 results · 42 ms",
    );
    expect(formatQuickPickSummary({ resultCount: 1, loading: true })).toBe("1 result · Searching…");
  });
});

describe("QuickPick free text", () => {
  const createItem = (query: string) =>
    query.length > 20
      ? { error: "Project names are at most 20 characters." }
      : item(`new:${query}`, { label: `Create "${query}"` });

  it("lists a row made from the typed text first and doesn't remember it", () => {
    const { input, onAccept } = renderPicker({ createItem, historyKey: "projects" });
    fireEvent.change(input, { target: { value: "New ideas" } });
    expect(optionNames()[0]).toBe('Create "New ideas"');
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onAccept).toHaveBeenCalledWith(expect.objectContaining({ id: "new:New ideas" }), {
      alternate: false,
    });
    expect(readQuickPickHistory("projects")).toEqual([]);
  });

  it("explains why typed text can't be used, tied to the query field", () => {
    const { input } = renderPicker({ createItem });
    fireEvent.change(input, { target: { value: "a name that is far too long" } });
    expect(input.getAttribute("aria-invalid")).toBe("true");
    const error = document.getElementById(input.getAttribute("aria-describedby") ?? "");
    expect(error).toHaveTextContent("Project names are at most 20 characters.");
  });
});

describe("QuickPick row styles", () => {
  it("styles a destructive row as a menu's destructive item", () => {
    renderPicker({ segments: [{ ...commands, items: [item("delete", { destructive: true })] }] });
    expect(screen.getByRole("option", { name: "delete" }).dataset.variant).toBe("destructive");
  });

  it("shows a row's detail unless it is disabled", () => {
    renderPicker({
      segments: [
        {
          ...commands,
          items: [
            item("engine", { detail: "Ghostty" }),
            item("other", { detail: "Hidden", disabledReason: "Not here" }),
          ],
        },
      ],
    });
    expect(screen.getByRole("option", { name: /engine/ })).toHaveTextContent("Ghostty");
    expect(screen.getByRole("option", { name: /other/ })).not.toHaveTextContent("Hidden");
  });

  it("names each row's group when rows are ranked across groups", () => {
    const { input } = renderPicker({
      segments: [
        {
          sourceId: "commands",
          ranking: "client",
          groupOrder: "match",
          items: [item("theme-a", { label: "Terminal theme", group: "Settings" })],
        },
      ],
    });
    fireEvent.change(input, { target: { value: "theme" } });
    const row = screen.getByRole("option", { name: "Terminal theme" });
    expect(row.querySelector("[data-slot=quick-pick-section]")).toHaveTextContent("Settings:");
  });
});

describe("QuickPick child pages", () => {
  it("shows a back button and goes back with Escape, Backspace in an empty query, or the footer", () => {
    const onBack = vi.fn();
    const { input } = renderPicker({ onBack, backLabel: "Back to themes" });
    expect(screen.getByRole("button", { name: "Back to themes" })).toBeVisible();
    fireEvent.keyDown(input, { key: "Escape" });
    fireEvent.keyDown(input, { key: "Backspace" });
    fireEvent.click(screen.getByRole("button", { name: "Back (Escape)" }));
    expect(onBack).toHaveBeenCalledTimes(3);
  });

  it("edits a non-empty query with Backspace instead of going back", () => {
    const onBack = vi.fn();
    const { input } = renderPicker({ onBack, query: "new" });
    fireEvent.keyDown(input, { key: "Backspace" });
    expect(onBack).not.toHaveBeenCalled();
  });

  it("goes back with Escape from the back button and returns focus to the query", () => {
    const onBack = vi.fn();
    const { input } = renderPicker({ onBack });
    const back = screen.getByRole("button", { name: "Back" });
    back.focus();
    fireEvent.keyDown(back, { key: "Escape" });
    expect(onBack).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(input);
  });

  it("marks a row that opens a page and opens it with Right Arrow at the end of the query", () => {
    const { input, onAccept } = renderPicker({
      segments: [{ ...commands, items: [item("appearance", { opensPage: true }), item("plain")] }],
    });
    expect(screen.getByRole("option", { name: "appearance" }).querySelector("svg")).not.toBeNull();
    fireEvent.keyDown(input, { key: "ArrowRight" });
    expect(onAccept).toHaveBeenCalledWith(expect.objectContaining({ id: "appearance" }), {
      alternate: false,
    });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowRight" });
    expect(onAccept).toHaveBeenCalledTimes(1);
  });

  it("lets the caller handle Right Arrow for the active row", () => {
    const onExpand = vi.fn(() => true);
    const { input, onAccept } = renderPicker({ onExpand });
    fireEvent.keyDown(input, { key: "ArrowRight" });
    expect(onExpand).toHaveBeenCalledWith(expect.objectContaining({ id: "new-session" }));
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

describe("QuickPick cards", () => {
  const apps: QuickPickSegment<string> = {
    sourceId: "apps",
    ranking: "client",
    fuzzyMatching: "words",
    items: [
      item("terminal", {
        label: "Terminal",
        group: "Apps",
        layout: "card",
        description: "Run commands in zsh",
        shortcut: ["⌘", "⌥", "T"],
      }),
      item("files", { label: "Files", group: "Apps", layout: "card", description: "Browse files" }),
      item("changes", {
        label: "Changes",
        group: "Apps",
        layout: "card",
        disabledReason: "Nothing changed yet",
      }),
      item("browser", { label: "Browser", group: "More" }),
    ],
  };
  /** Each option's label, and whether it shows as a card inside the card grid. */
  const layouts = () =>
    screen
      .getAllByRole("option")
      .map((option) => [
        option.querySelector("[data-slot=quick-pick-label]")?.textContent,
        option.dataset.layout === "card" && option.closest("[data-slot=quick-pick-cards]") !== null,
      ]);

  it("shows card items in a grid, with their hint and shortcut, and other rows as a list", () => {
    renderPicker({ segments: [apps] });
    expect(layouts()).toEqual([
      ["Terminal", true],
      ["Files", true],
      ["Changes", true],
      ["Browser", false],
    ]);
    // The cards stay in their group, which is named by its heading.
    const [appsGroup] = screen.getAllByRole("group");
    expect(within(appsGroup!).getAllByRole("option")).toHaveLength(3);
    const terminal = screen.getByRole("option", { name: /Terminal/ });
    expect(terminal).toHaveTextContent("Run commands in zsh");
    expect(terminal.querySelector("[data-slot=shortcut-keys]")).not.toBeNull();
    // A disabled card says why in place of its hint.
    expect(screen.getByRole("option", { name: /Changes/ })).toHaveTextContent(
      "Nothing changed yet",
    );
  });

  it("moves through cards and rows in reading order, skipping a disabled card, and accepts with Enter", () => {
    const { input, onAccept } = renderPicker({ segments: [apps] });
    expect(activeOption(input)).toHaveTextContent("Terminal");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(activeOption(input)).toHaveTextContent("Files");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(activeOption(input)).toHaveTextContent("Browser");
    fireEvent.keyDown(input, { key: "ArrowUp" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onAccept).toHaveBeenLastCalledWith(expect.objectContaining({ id: "files" }), {
      alternate: false,
    });
  });

  it("shows cards as ranked list rows while there's a query", () => {
    const { input } = renderPicker({ segments: [apps] });
    fireEvent.change(input, { target: { value: "files" } });
    expect(layouts()).toEqual([["Files", false]]);
  });
});

describe("QuickPick rendering", () => {
  it("renders once for each change, with no heading buttons to line up", () => {
    const summary = vi.fn(() => "2 results");
    const view = renderPicker({ summary });
    const settled = summary.mock.calls.length;
    // A new prop renders the list once more, and measuring its headings asks for nothing else.
    view.rerender(<Harness summary={summary} onAccept={vi.fn()} placeholder="Search" />);
    expect(summary.mock.calls.length - settled).toBe(1);
  });
});
