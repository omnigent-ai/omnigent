import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, onTestFinished, vi } from "vitest";

import type { QuickPickItem } from "./model";
import type { QuickPickSegment } from "./presentation";
import { QuickPick } from "./QuickPick";

afterEach(cleanup);

function item(id: string, overrides: Partial<QuickPickItem<string>> = {}): QuickPickItem<string> {
  return { id, label: id, value: id, ...overrides };
}

// Sessions [s1, s2], In messages [m1 (disabled), m2], Actions [a1, a2].
const grouped: QuickPickSegment<string>[] = [
  {
    sourceId: "titles",
    ranking: "provider",
    items: [item("s1", { group: "Sessions" }), item("s2", { group: "Sessions" })],
  },
  {
    sourceId: "content",
    ranking: "provider",
    items: [
      item("m1", { group: "In messages", disabledReason: "Unavailable" }),
      item("m2", { group: "In messages" }),
    ],
  },
  {
    sourceId: "commands",
    ranking: "client",
    items: [item("a1", { group: "Actions" }), item("a2", { group: "Actions" })],
  },
];

function renderPicker(segments = grouped, loop?: boolean) {
  const onAccept = vi.fn();
  render(
    <QuickPick
      label="Command palette"
      segments={segments}
      query=""
      onQueryChange={vi.fn()}
      onAccept={onAccept}
      emptyMessage="No results found"
      loop={loop}
    />,
  );
  const input = screen.getByRole("combobox");
  const press = (key: string, init: Record<string, unknown> = {}) =>
    fireEvent.keyDown(input, { key, ...init });
  const active = () =>
    document.getElementById(input.getAttribute("aria-activedescendant") ?? "")?.textContent;
  return { input, press, active, onAccept };
}

describe("QuickPick arrow keys", () => {
  it("step over disabled rows and stop at both ends", () => {
    const { press, active } = renderPicker();
    expect(active()).toBe("s1");
    press("ArrowUp");
    expect(active()).toBe("s1");
    press("ArrowDown");
    press("ArrowDown");
    expect(active()).toBe("m2");
    press("ArrowDown");
    press("ArrowDown");
    expect(active()).toBe("a2");
    press("ArrowDown");
    expect(active()).toBe("a2");
    press("ArrowUp");
    press("ArrowUp");
    press("ArrowUp");
    expect(active()).toBe("s2");
  });

  it("wrap at both ends only when the picker loops", () => {
    const { press, active } = renderPicker(grouped, true);
    press("ArrowUp");
    expect(active()).toBe("a2");
    press("ArrowDown");
    expect(active()).toBe("s1");
  });

  it("jump to the ends with Meta, while Ctrl moves one row", () => {
    const { press, active } = renderPicker();
    press("ArrowDown", { metaKey: true });
    expect(active()).toBe("a2");
    press("ArrowUp", { metaKey: true });
    expect(active()).toBe("s1");
    press("ArrowDown", { ctrlKey: true });
    expect(active()).toBe("s2");
    press("ArrowUp", { ctrlKey: true });
    expect(active()).toBe("s1");
  });

  it("move to the first enabled row of the next or previous group with Alt", () => {
    const { press, active } = renderPicker();
    press("ArrowDown", { altKey: true });
    expect(active()).toBe("m2");
    press("ArrowDown", { altKey: true });
    expect(active()).toBe("a1");
    // No group after Actions: one row, then it stays at the end.
    press("ArrowDown", { altKey: true });
    expect(active()).toBe("a2");
    press("ArrowDown", { altKey: true });
    expect(active()).toBe("a2");
    press("ArrowUp", { altKey: true });
    expect(active()).toBe("m2");
    press("ArrowUp", { altKey: true });
    expect(active()).toBe("s1");
    press("ArrowUp", { altKey: true });
    expect(active()).toBe("s1");
  });

  it("pass over a group whose rows are all disabled with Alt", () => {
    const segments = grouped.map((segment) =>
      segment.sourceId === "content"
        ? {
            ...segment,
            items: segment.items.map((entry) => ({ ...entry, disabledReason: "Unavailable" })),
          }
        : segment,
    );
    const { press, active } = renderPicker(segments);
    press("ArrowDown", { altKey: true });
    expect(active()).toBe("a1");
  });

  it("move one row with Alt when the rows have no groups", () => {
    const { press, active } = renderPicker([
      { sourceId: "s", ranking: "client", items: [item("x"), item("y")] },
    ]);
    press("ArrowDown", { altKey: true });
    expect(active()).toBe("y");
    press("ArrowUp", { altKey: true });
    expect(active()).toBe("x");
  });
});

describe("QuickPick group keys with ungrouped rows", () => {
  it("step once from an ungrouped row instead of joining the group above", () => {
    const { press, active } = renderPicker([
      { sourceId: "early", ranking: "provider", items: [item("p1", { group: "Earlier" })] },
      {
        sourceId: "sessions",
        ranking: "provider",
        items: [item("a1", { group: "Sessions" }), item("a2", { group: "Sessions" })],
      },
      { sourceId: "loose", ranking: "provider", items: [item("u")] },
      { sourceId: "actions", ranking: "provider", items: [item("z1", { group: "Actions" })] },
    ]);
    press("End");
    press("ArrowUp");
    expect(active()).toBe("u");
    press("ArrowUp", { altKey: true });
    expect(active()).toBe("a2");
  });

  it("skip a disabled group even when an ungrouped row follows it", () => {
    const { press, active } = renderPicker([
      { sourceId: "sessions", ranking: "provider", items: [item("a1", { group: "Sessions" })] },
      {
        sourceId: "disabled",
        ranking: "provider",
        items: [item("d1", { group: "Disabled", disabledReason: "Not here" }), item("u")],
      },
      { sourceId: "actions", ranking: "provider", items: [item("z1", { group: "Actions" })] },
    ]);
    press("ArrowDown", { altKey: true });
    expect(active()).toBe("z1");
  });
});

describe("QuickPick revealing the active row", () => {
  const rowsOf = (count: number): QuickPickSegment<string>[] => [
    {
      sourceId: "search",
      ranking: "provider",
      items: Array.from({ length: count }, (_, index) => item(`row-${index}`)),
    },
  ];

  function spyOnScroll() {
    const original = Element.prototype.scrollIntoView;
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    onTestFinished(() => {
      Element.prototype.scrollIntoView = original;
    });
    return scrollIntoView;
  }

  function renderControlled(segments: QuickPickSegment<string>[], query: string) {
    return (
      <QuickPick
        label="Command palette"
        segments={segments}
        query={query}
        onQueryChange={vi.fn()}
        onAccept={vi.fn()}
        emptyMessage="No results found"
      />
    );
  }

  it("scrolls back to the top row when a new query resets the selection", () => {
    const scrollIntoView = spyOnScroll();
    const { rerender } = render(renderControlled(rowsOf(20), ""));
    fireEvent.keyDown(screen.getByRole("combobox"), { key: "End" });
    expect(scrollIntoView.mock.contexts.at(-1)).toBe(
      screen.getByRole("option", { name: "row-19" }),
    );
    scrollIntoView.mockClear();
    rerender(renderControlled(rowsOf(20), "row"));
    expect(scrollIntoView).toHaveBeenCalledOnce();
    expect(scrollIntoView.mock.contexts[0]).toBe(screen.getByRole("option", { name: "row-0" }));
  });

  it("scrolls to a result that arrives above the top row", () => {
    const scrollIntoView = spyOnScroll();
    const { rerender } = render(renderControlled(rowsOf(3), "row"));
    scrollIntoView.mockClear();
    const late: QuickPickSegment<string> = {
      sourceId: "titles",
      ranking: "provider",
      items: [item("late")],
    };
    rerender(renderControlled([late, ...rowsOf(3)], "row"));
    expect(scrollIntoView.mock.contexts.at(-1)).toBe(screen.getByRole("option", { name: "late" }));
  });

  it("still scrolls for keys after the pointer hovers the active top row", () => {
    const scrollIntoView = spyOnScroll();
    render(renderControlled(rowsOf(20), ""));
    fireEvent.pointerMove(screen.getByRole("option", { name: "row-0" }));
    scrollIntoView.mockClear();
    fireEvent.keyDown(screen.getByRole("combobox"), { key: "End" });
    expect(scrollIntoView).toHaveBeenCalledOnce();
    expect(scrollIntoView.mock.contexts[0]).toBe(screen.getByRole("option", { name: "row-19" }));
  });

  it("leaves the scroll alone when the pointer picks a row", () => {
    const scrollIntoView = spyOnScroll();
    render(renderControlled(rowsOf(3), ""));
    scrollIntoView.mockClear();
    fireEvent.pointerMove(screen.getByRole("option", { name: "row-2" }));
    expect(scrollIntoView).not.toHaveBeenCalled();
  });
});

describe("QuickPick other navigation keys", () => {
  it("go to the ends with Home and End", () => {
    const { press, active } = renderPicker();
    press("End");
    expect(active()).toBe("a2");
    press("Home");
    expect(active()).toBe("s1");
  });

  it("page by the rows that fit, ten without a layout, without wrapping", () => {
    const many = Array.from({ length: 15 }, (_, index) => item(`row-${index}`));
    const { press, active } = renderPicker([{ sourceId: "s", ranking: "client", items: many }]);
    press("PageDown");
    expect(active()).toBe("row-10");
    press("PageDown");
    expect(active()).toBe("row-14");
    press("PageUp");
    expect(active()).toBe("row-4");
    press("PageUp");
    expect(active()).toBe("row-0");
  });

  it("leave Ctrl+N/P and Ctrl+J/K to the text field", () => {
    const { press, active } = renderPicker();
    for (const key of ["n", "j", "p", "k"]) press(key, { ctrlKey: true });
    expect(active()).toBe("s1");
  });

  it("ignore navigation while an IME is composing", () => {
    const { press, active } = renderPicker();
    press("ArrowDown", { isComposing: true });
    press("End", { keyCode: 229 });
    expect(active()).toBe("s1");
  });

  it("accept the row the keys moved to, and scroll it into view", () => {
    const original = Element.prototype.scrollIntoView;
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    onTestFinished(() => {
      Element.prototype.scrollIntoView = original;
    });
    const { press, onAccept } = renderPicker();
    press("ArrowDown");
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "nearest" });
    expect(scrollIntoView.mock.contexts.at(-1)).toBe(screen.getByRole("option", { name: "s2" }));
    press("Enter");
    expect(onAccept).toHaveBeenCalledWith(expect.objectContaining({ id: "s2" }));
  });
});
