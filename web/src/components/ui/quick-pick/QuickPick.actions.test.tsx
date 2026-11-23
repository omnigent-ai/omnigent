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

  it("keeps a newer pointer pick when a row's close finishes later", () => {
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
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Backspace", ctrlKey: true });
    expect(active()).toBe("beta");
    fireEvent.pointerMove(screen.getByRole("option", { name: "alpha" }));
    expect(active()).toBe("alpha");
    act(() => finishClose());
    expect(screen.queryByRole("option", { name: "beta" })).toBeNull();
    expect(active()).toBe("alpha");
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
    // The listbox holds only its groups and options.
    expect(button.closest('[role="listbox"]')).toBeNull();
    expect(screen.getByRole("listbox").querySelectorAll("button")).toHaveLength(0);
    // A real button in the tab order, which keeps the query's focus when clicked.
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
    renderPicker({
      segments: grouped,
      groupCounts: true,
      groupActions: (group) =>
        group === "Files" ? [{ id: "close-all", label: "Close all", run: () => {} }] : [],
    });
    const headings = [
      ...document.querySelectorAll<HTMLElement>("[data-slot=quick-pick-group-heading]"),
    ];
    expect(headings.map((heading) => heading.style.paddingRight)).toEqual(["188px", ""]);
    measure.mockRestore();
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
