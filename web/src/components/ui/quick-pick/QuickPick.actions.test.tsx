import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";

import type { QuickPickGroupAction, QuickPickItem } from "./model";
import type { QuickPickSegment } from "./presentation";
import { QuickPick, type QuickPickProps } from "./QuickPick";

const items: QuickPickItem<string>[] = [
  { id: "alpha", label: "Alpha", value: "alpha" },
  { id: "beta", label: "Beta", value: "beta" },
];
const segments: QuickPickSegment<string>[] = [{ sourceId: "items", ranking: "client", items }];

type PickerProps = Omit<QuickPickProps<string>, "query" | "onQueryChange"> & { query?: string };

/** The picker with the query it owns, as a caller holds it. */
function Picker({ query: initialQuery = "", ...props }: PickerProps) {
  const [query, setQuery] = useState(initialQuery);
  return <QuickPick<string> {...props} query={query} onQueryChange={setQuery} />;
}

function renderPicker(props: Partial<PickerProps> = {}) {
  const onAccept = vi.fn();
  render(
    <TooltipProvider>
      <Picker
        label="Things"
        segments={segments}
        onAccept={onAccept}
        emptyMessage="No things"
        {...props}
      />
    </TooltipProvider>,
  );
  const input = screen.getByRole("combobox", { name: "Things" }) as HTMLInputElement;
  return { input, onAccept };
}
describe("QuickPick row buttons", () => {
  const closeChord = { ctrl: true, key: "Backspace" } as const;
  function withClose(run: (id: string) => void): QuickPickSegment<string>[] {
    return [
      {
        sourceId: "items",
        ranking: "client",
        items: items.map((item) => ({
          ...item,
          actions: [
            {
              id: "close",
              label: "Close tab",
              icon: <span />,
              chord: closeChord,
              run: () => run(item.id),
            },
          ],
        })),
      },
    ];
  }

  it("puts the buttons beside the option, not inside it, and names their keys on the option", () => {
    renderPicker({ segments: withClose(() => {}) });
    const [alpha] = screen.getAllByRole("option");
    expect(alpha.querySelector("button")).toBeNull();
    expect(alpha.getAttribute("aria-keyshortcuts")).toBe("Control+Backspace");
    const row = alpha.closest("[data-slot=quick-pick-row]")!;
    const button = row.querySelector<HTMLButtonElement>("[data-slot=quick-pick-row-action]")!;
    expect(button.tabIndex).toBe(-1);
    expect(button.closest("[aria-hidden]")).not.toBeNull();
    expect(screen.getByRole("listbox").querySelectorAll("[role=option] button")).toHaveLength(0);
  });

  it("runs a row's button by click without accepting the row or leaving the query", () => {
    const run = vi.fn();
    const { input, onAccept } = renderPicker({ segments: withClose(run) });
    input.focus();
    const beta = screen.getAllByRole("option")[1].closest("[data-slot=quick-pick-row]")!;
    const button = beta.querySelector<HTMLButtonElement>("[data-slot=quick-pick-row-action]")!;
    expect(fireEvent.mouseDown(button)).toBe(false);
    fireEvent.click(button);
    expect(run).toHaveBeenCalledWith("beta");
    expect(onAccept).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(input);
  });

  it("runs the active row's button by its keys, and shows them in the footer", () => {
    const run = vi.fn();
    const { input, onAccept } = renderPicker({ segments: withClose(run), onClose: () => {} });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Backspace", ctrlKey: true });
    expect(run).toHaveBeenCalledWith("beta");
    expect(onAccept).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Close tab (Ctrl+Backspace)" }));
    expect(run).toHaveBeenLastCalledWith("beta");
  });

  it("hands the selection to the next row when the active one goes, or the previous at the end", () => {
    function Shrinking() {
      const [ids, setIds] = useState(["alpha", "beta", "gamma"]);
      return (
        <TooltipProvider>
          <Picker
            label="Things"
            segments={[
              {
                sourceId: "items",
                ranking: "client",
                items: ids.map((id) => ({
                  id,
                  label: id,
                  value: id,
                  actions: [
                    {
                      id: "close",
                      label: "Close",
                      icon: <span />,
                      chord: closeChord,
                      run: () => setIds((current) => current.filter((each) => each !== id)),
                    },
                  ],
                })),
              },
            ]}
            onAccept={() => {}}
            emptyMessage="No things"
          />
        </TooltipProvider>
      );
    }
    render(<Shrinking />);
    const input = screen.getByRole("combobox", { name: "Things" });
    const active = () =>
      document.getElementById(input.getAttribute("aria-activedescendant") ?? "")?.textContent;
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(active()).toBe("beta");
    fireEvent.keyDown(input, { key: "Backspace", ctrlKey: true });
    expect(active()).toBe("gamma");
    fireEvent.keyDown(input, { key: "Backspace", ctrlKey: true });
    expect(active()).toBe("alpha");
  });

  it("hands the selection to the closed row's neighbour when it was highlighted by default", () => {
    function Reordering() {
      const [ids, setIds] = useState(["alpha", "beta", "gamma"]);
      return (
        <TooltipProvider>
          <Picker
            label="Things"
            segments={[
              {
                sourceId: "items",
                ranking: "client",
                items: ids.map((id) => ({
                  id,
                  label: id,
                  value: id,
                  actions: [
                    {
                      id: "close",
                      label: "Close",
                      icon: <span />,
                      chord: closeChord,
                      // Like a tab list that moves the tab now on screen to the top.
                      run: () =>
                        setIds((current) => {
                          const rest = current.filter((each) => each !== id);
                          return [rest[rest.length - 1]!, ...rest.slice(0, -1)];
                        }),
                    },
                  ],
                })),
              },
            ]}
            onAccept={() => {}}
            emptyMessage="No things"
          />
        </TooltipProvider>
      );
    }
    render(<Reordering />);
    const input = screen.getByRole("combobox", { name: "Things" });
    const active = () =>
      document.getElementById(input.getAttribute("aria-activedescendant") ?? "")?.textContent;
    expect(active()).toBe("alpha");
    fireEvent.keyDown(input, { key: "Backspace", ctrlKey: true });
    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
      "gamma",
      "beta",
    ]);
    expect(active()).toBe("beta");
  });

  /** A list whose rows close only once a later step finishes, as a shell that asks first. */
  function renderSlowClose() {
    let finishClose: () => void = () => {};
    function SlowClose() {
      const [ids, setIds] = useState(["alpha", "beta", "gamma"]);
      return (
        <TooltipProvider>
          <Picker
            label="Things"
            segments={[
              {
                sourceId: "items",
                ranking: "client",
                items: ids.map((id) => ({
                  id,
                  label: id,
                  value: id,
                  actions: [
                    {
                      id: "close",
                      label: "Close",
                      icon: <span />,
                      chord: closeChord,
                      // Like a shell that asks first: the row goes only once that's answered.
                      run: () => {
                        finishClose = () =>
                          setIds((current) => current.filter((each) => each !== id));
                      },
                    },
                  ],
                })),
              },
            ]}
            onAccept={() => {}}
            emptyMessage="No things"
          />
        </TooltipProvider>
      );
    }
    render(<SlowClose />);
    const input = screen.getByRole("combobox", { name: "Things" });
    const active = () =>
      document.getElementById(input.getAttribute("aria-activedescendant") ?? "")?.textContent;
    return { input, active, finishClose: () => act(() => finishClose()) };
  }

  it("keeps a newer pointer pick when a row's close finishes later", () => {
    const { input, active, finishClose } = renderSlowClose();
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Backspace", ctrlKey: true });
    expect(active()).toBe("beta");
    fireEvent.pointerMove(screen.getByRole("option", { name: "alpha" }));
    expect(active()).toBe("alpha");
    finishClose();
    expect(screen.queryByRole("option", { name: "beta" })).toBeNull();
    expect(active()).toBe("alpha");
  });

  it("moves to the next row when a row's close finishes later with no newer pick", () => {
    const { input, active, finishClose } = renderSlowClose();
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Backspace", ctrlKey: true });
    expect(active()).toBe("beta");
    finishClose();
    expect(screen.queryByRole("option", { name: "beta" })).toBeNull();
    expect(active()).toBe("gamma");
  });

  it("leaves the chord to the query while it has text before the caret or selected", () => {
    const run = vi.fn();
    const { input } = renderPicker({ segments: withClose(run), query: "al" });
    // The caret sits at the end, as after typing: Ctrl+Backspace deletes a word.
    input.setSelectionRange(2, 2);
    expect(fireEvent.keyDown(input, { key: "Backspace", ctrlKey: true })).toBe(true);
    input.setSelectionRange(0, 2);
    expect(fireEvent.keyDown(input, { key: "Backspace", ctrlKey: true })).toBe(true);
    expect(run).not.toHaveBeenCalled();
    // At the start there's nothing to delete, so the row's button takes it.
    input.setSelectionRange(0, 0);
    expect(fireEvent.keyDown(input, { key: "Backspace", ctrlKey: true })).toBe(false);
    expect(run).toHaveBeenCalledWith("alpha");
  });

  it("runs no button by keys on a disabled row", () => {
    const run = vi.fn();
    const [segment] = withClose(run);
    renderPicker({
      segments: [
        {
          ...segment!,
          items: segment!.items.map((item) => ({ ...item, disabledReason: "Not now" })),
        },
      ],
    });
    const input = screen.getByRole("combobox", { name: "Things" });
    fireEvent.keyDown(input, { key: "Backspace", ctrlKey: true });
    expect(run).not.toHaveBeenCalled();
  });

  it("leaves Backspace alone without the modifier", () => {
    const run = vi.fn();
    const { input } = renderPicker({ segments: withClose(run) });
    expect(fireEvent.keyDown(input, { key: "Backspace" })).toBe(true);
    expect(run).not.toHaveBeenCalled();
  });
});

describe("QuickPick group headings", () => {
  const grouped: QuickPickSegment<string>[] = [
    {
      sourceId: "items",
      ranking: "client",
      items: [
        { id: "a", label: "Alpha", group: "Files", value: "a" },
        { id: "b", label: "Beta", group: "Files", value: "b" },
        { id: "c", label: "Gamma", group: "Pages", value: "c" },
      ],
    },
  ];

  it("keeps the plain caption, exactly, without counts or heading buttons", () => {
    renderPicker({ segments: grouped });
    const group = screen.getByRole("group", { name: "Files" });
    const heading = document.getElementById(group.getAttribute("aria-labelledby")!)!;
    expect(heading.outerHTML).toBe(
      `<div id="${heading.id}" role="presentation" class="px-2 py-1.5 text-sm font-medium text-muted-foreground">Files</div>`,
    );
    expect(document.querySelector("[data-slot=quick-pick-group-heading]")).toBeNull();
  });

  it("shows counts in the heading and its buttons beside it, outside the listbox", () => {
    const run = vi.fn();
    const actions = (group: string): QuickPickGroupAction[] =>
      group === "Files"
        ? [{ id: "close-all", label: "Close all", ariaLabel: "Close all files", run }]
        : [];
    const { input } = renderPicker({ segments: grouped, groupActions: actions, groupCounts: true });
    expect(screen.getByRole("group", { name: "Files 2" })).toBeTruthy();
    expect(screen.getByRole("group", { name: "Pages 1" })).toBeTruthy();
    const button = screen.getByRole("button", { name: "Close all files" });
    expect(button.closest('[role="listbox"]')).toBeNull();
    expect(screen.getByRole("listbox").querySelectorAll("button")).toHaveLength(0);
    expect(button.tabIndex).not.toBe(-1);
    expect(fireEvent.mouseDown(button)).toBe(false);
    button.focus();
    fireEvent.click(button);
    expect(run).toHaveBeenCalledOnce();
    expect(document.activeElement).toBe(input);
    expect(screen.getAllByRole("button", { name: /Close all/ })).toHaveLength(1);
  });

  it("keeps the heading's label clear of its buttons, which it leaves room for", () => {
    // jsdom lays nothing out; give the buttons the width they'd have.
    const measure = vi
      .spyOn(Element.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: Element) {
        return (this as HTMLElement).dataset?.actionsFor === undefined
          ? new DOMRect(0, 0, 0, 0)
          : new DOMRect(0, 0, 180, 32);
      });
    try {
      const summary = vi.fn(() => "3 results");
      const { input } = renderPicker({
        segments: grouped,
        groupCounts: true,
        summary,
        groupActions: (group) =>
          group === "Files" ? [{ id: "close-all", label: "Close all", run: () => {} }] : [],
      });
      const headings = () => [
        ...document.querySelectorAll<HTMLElement>("[data-slot=quick-pick-group-heading]"),
      ];
      expect(headings().map((heading) => heading.style.paddingRight)).toEqual(["188px", ""]);
      // Moving the selection renders once; measuring the same buttons again sets no state.
      const settled = summary.mock.calls.length;
      fireEvent.keyDown(input, { key: "ArrowDown" });
      expect(summary.mock.calls.length - settled).toBe(1);
      expect(headings().map((heading) => heading.style.paddingRight)).toEqual(["188px", ""]);
    } finally {
      measure.mockRestore();
    }
  });

  it("closes the picker on Escape from a heading button", () => {
    const onDismiss = vi.fn();
    renderPicker({
      segments: grouped,
      onDismiss,
      groupActions: () => [{ id: "close-all", label: "Close all", run: () => {} }],
    });
    fireEvent.keyDown(screen.getAllByRole("button", { name: "Close all" })[0], { key: "Escape" });
    expect(onDismiss).toHaveBeenCalledOnce();
  });
});
