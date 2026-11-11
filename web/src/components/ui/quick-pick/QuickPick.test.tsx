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
      listStart: <p>Couldn&apos;t load sessions.</p>,
      listEnd: <button type="button">Search older sessions</button>,
      inputEnd: <button type="button">Close search</button>,
    });
    fireEvent.change(input, { target: { value: "zzzz" } });
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    expect(screen.getByText("No results found")).toBeTruthy();
    expect(screen.getByRole("status").textContent).toBe("No results found");
    expect(screen.getByText("Couldn't load sessions.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Search older sessions" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Close search" })).toBeTruthy();
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
    fireEvent.click(screen.getByRole("option", { name: /Open settings/ }));
    expect(onAccept).toHaveBeenLastCalledWith(expect.objectContaining({ id: "open-settings" }));
    expect(document.activeElement).toBe(input);
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
