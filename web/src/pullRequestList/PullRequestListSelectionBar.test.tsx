// The selection bar at any width: actions shorten, then move into More from the end, never
// clipping; focus follows a button that folds, an open More keeps what it lists, and the
// measuring copies stay out of reach.

import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CheckIcon, ExternalLinkIcon, GitPullRequestDraftIcon, UserMinusIcon } from "lucide-react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ListSelectionAction } from "./listSelectionActions";
import {
  PullRequestListSelectionBar,
  type PullRequestListSelectionBarProps,
} from "./PullRequestListSelectionBar";

// jsdom lays nothing out. Here a character is 6px wide, an icon 13px, and a control's padding
// 18px; the list is `listWidth` wide and the divider 9px, with no gaps or margins.
let listWidth = 1000;
function widthOf(element: Element): number {
  if (element.matches("[data-testid='list']")) return listWidth;
  if (element.matches(".pr-list-selection-divider")) return 9;
  const drawn = [element, ...element.querySelectorAll("[data-text]")].reduce(
    (total, part) => total + (part.getAttribute("data-text")?.length ?? 0),
    0,
  );
  const characters = (element.textContent ?? "").length + drawn;
  const icons = element.querySelectorAll("svg").length + (element.matches("svg") ? 1 : 0);
  const control = element.matches("button, .pr-list-selection-control") ? 18 : 0;
  return characters * 6 + icons * 13 + control;
}

const observers: FakeResizeObserver[] = [];
class FakeResizeObserver {
  disconnected = false;
  readonly callback: ResizeObserverCallback;
  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
    observers.push(this);
  }
  observe() {
    this.disconnected = false;
  }
  unobserve() {}
  disconnect() {
    this.disconnected = true;
  }
}
const frames: FrameRequestCallback[] = [];
const runFrames = () => {
  for (const frame of frames.splice(0)) frame(0);
};
/** The list resizes to `width`, and the frame the bar waits for runs. */
const resize = (width: number) =>
  act(() => {
    listWidth = width;
    for (const observer of observers) {
      if (!observer.disconnected) observer.callback([], observer as unknown as ResizeObserver);
    }
    runFrames();
  });

beforeEach(() => {
  listWidth = 1000;
  observers.length = 0;
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  vi.stubGlobal("requestAnimationFrame", (frame: FrameRequestCallback) => frames.push(frame));
  vi.stubGlobal("cancelAnimationFrame", () => {});
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
    const width = widthOf(this);
    return { width, height: 0, top: 0, left: 0, right: width, bottom: 0, x: 0, y: 0 } as DOMRect;
  });
});
afterEach(() => {
  cleanup();
  frames.length = 0;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const action = (over: Partial<ListSelectionAction> & { id: string }): ListSelectionAction => ({
  label: over.id,
  paletteLabel: over.id,
  icon: CheckIcon,
  run: vi.fn(),
  ...over,
});
// Full: 133, 157 and 127 wide; short: 97, 85 and 61.
const approve = () =>
  action({
    id: "approve",
    label: "Approve… (1 of 3)",
    shortLabel: "Approve…",
    fits: { acting: 1, of: 3 },
  });
const removeMe = () =>
  action({
    id: "remove-me",
    label: "Remove Me as Reviewer",
    shortLabel: "Remove Me",
    icon: UserMinusIcon,
    fits: { acting: 3, of: 3 },
  });
const convert = () =>
  action({
    id: "convert",
    label: "Convert to Draft",
    shortLabel: "Draft",
    icon: GitPullRequestDraftIcon,
  });
const open = action({ id: "open", label: "Open", icon: ExternalLinkIcon, inMoreMenu: true });

// With "3 selected" (60), the divider, More and Clear (31 each), the bar is 548 wide at full
// length, 374 short, then 313, 228 and 131 with one, two and three actions in More.
function renderBar(props: Partial<PullRequestListSelectionBarProps> = {}) {
  const base: PullRequestListSelectionBarProps = {
    selectedCount: 3,
    selectAllCount: null,
    onSelectAll: () => {},
    actions: [approve(), removeMe(), convert()],
    moreGroups: [[open]],
    remove: null,
    onClear: () => {},
    ...props,
  };
  const view = render(
    <div data-testid="list">
      <PullRequestListSelectionBar {...base} />
    </div>,
  );
  const rerender = (next: Partial<PullRequestListSelectionBarProps>) =>
    view.rerender(
      <div data-testid="list">
        <PullRequestListSelectionBar {...base} {...next} />
      </div>,
    );
  return { ...view, rerender };
}

// An open menu hides the rest of the page from readers, so these look past that.
const bar = () => screen.getByRole("toolbar", { name: "Selected pull requests", hidden: true });
const barButtons = () =>
  within(bar())
    .getAllByRole("button", { hidden: true })
    .map((button) => button.getAttribute("aria-label") ?? button.textContent);
const menuItems = () => screen.getAllByRole("menuitem").map((item) => item.textContent);

describe("PullRequestListSelectionBar", () => {
  it("shortens every label, then moves actions into More from the end, and back as it widens", async () => {
    listWidth = 600;
    renderBar();
    expect(barButtons()).toEqual([
      "Approve… (1 of 3)",
      "Remove Me as Reviewer",
      "Convert to Draft",
      "More Actions",
      "Clear Selection",
    ]);
    resize(500);
    // Short labels keep the full one as the name and tooltip; a share shows as a badge.
    const approveButton = within(bar()).getByRole("button", {
      name: "Approve… (1 of 3)",
      hidden: true,
    });
    expect(approveButton).toHaveTextContent("Approve…1/3");
    expect(approveButton).toHaveAttribute("title", "Approve… (1 of 3)");
    expect(
      within(bar()).getByRole("button", { name: "Remove Me as Reviewer", hidden: true }),
    ).toHaveTextContent(/^Remove Me$/);
    resize(350);
    expect(barButtons()).toEqual([
      "Approve… (1 of 3)",
      "Remove Me as Reviewer",
      "More Actions",
      "Clear Selection",
    ]);
    resize(250);
    resize(150);
    expect(barButtons()).toEqual(["More Actions", "Clear Selection"]);
    expect(bar()).toHaveTextContent(/^3 selected/);
    resize(100);
    // The count keeps its number; its words stay for screen readers.
    expect(within(bar()).getByText("selected", { exact: false })).toHaveClass(
      "pr-list-visually-hidden",
    );
    // What folded leads More, in the bar's order, before what More always lists.
    await userEvent.click(
      within(bar()).getByRole("button", { name: "More Actions", hidden: true }),
    );
    expect(menuItems()).toEqual([
      "Approve… (1 of 3)",
      "Remove Me as Reviewer",
      "Convert to Draft",
      "Open",
    ]);
    await userEvent.keyboard("{Escape}");
    resize(600);
    expect(barButtons()).toHaveLength(5);
  });

  it("runs a folded action from More, and shows one that can't act disabled with its reason", async () => {
    listWidth = 250;
    const draft = convert();
    const busy = { ...removeMe(), unavailableReason: "Another change is still being made." };
    renderBar({ actions: [approve(), busy, draft] });
    await userEvent.click(
      within(bar()).getByRole("button", { name: "More Actions", hidden: true }),
    );
    const removeItem = screen.getByRole("menuitem", { name: /Remove Me as Reviewer/ });
    expect(removeItem).toHaveAttribute("aria-disabled", "true");
    expect(removeItem).toHaveTextContent("Another change is still being made.");
    await userEvent.click(screen.getByRole("menuitem", { name: "Convert to Draft" }));
    expect(draft.run).toHaveBeenCalledOnce();
  });

  it("gives a button that can't act its reason as its tooltip, even when shortened", () => {
    listWidth = 500;
    const busy = { ...approve(), unavailableReason: "Nothing here can be approved." };
    renderBar({ actions: [busy, removeMe(), convert()] });
    const button = within(bar()).getByRole("button", { name: "Approve… (1 of 3)", hidden: true });
    expect(button).toHaveAttribute("title", "Nothing here can be approved.");
    expect(button).toHaveAttribute("aria-disabled", "true");
  });

  it("hands a focused button's focus to More as it folds, and leaves other focus alone", () => {
    listWidth = 600;
    renderBar();
    within(bar()).getByRole("button", { name: "Convert to Draft", hidden: true }).focus();
    resize(350);
    expect(document.activeElement).toBe(
      within(bar()).getByRole("button", { name: "More Actions", hidden: true }),
    );
    // A button that stays keeps its focus while another folds.
    within(bar()).getByRole("button", { name: "Approve… (1 of 3)", hidden: true }).focus();
    resize(250);
    expect(document.activeElement).toBe(
      within(bar()).getByRole("button", { name: "Approve… (1 of 3)", hidden: true }),
    );
    // Clear never folds, so it keeps its focus as everything else does.
    const clear = within(bar()).getByRole("button", { name: "Clear Selection", hidden: true });
    clear.focus();
    resize(100);
    expect(document.activeElement).toBe(clear);
  });

  it("keeps fitting while More is open, and keeps listing what it listed until it closes", async () => {
    listWidth = 600;
    const { rerender } = renderBar();
    await userEvent.click(
      within(bar()).getByRole("button", { name: "More Actions", hidden: true }),
    );
    expect(menuItems()).toEqual(["Open"]);
    const focused = document.activeElement;
    resize(250);
    // The bar still fits; what folded joins the open menu in the bar's order.
    expect(barButtons()).toEqual(["Approve… (1 of 3)", "More Actions", "Clear Selection"]);
    expect(menuItems()).toEqual(["Remove Me as Reviewer", "Convert to Draft", "Open"]);
    expect(document.activeElement).toBe(focused);
    resize(600);
    // Back in the bar, they stay in the open menu too: nothing leaves it while it's open.
    expect(barButtons()).toHaveLength(5);
    expect(menuItems()).toEqual(["Remove Me as Reviewer", "Convert to Draft", "Open"]);
    // Each still acts as the selection says now; one gone from it no longer acts at all.
    const remove = removeMe();
    rerender({ actions: [approve(), remove] });
    await userEvent.click(screen.getByRole("menuitem", { name: "Remove Me as Reviewer" }));
    expect(remove.run).toHaveBeenCalledOnce();
    await userEvent.click(
      within(bar()).getByRole("button", { name: "More Actions", hidden: true }),
    );
    expect(menuItems()).toEqual(["Open"]);
  });

  it("lists an action gone from the selection, while the menu stays open, as one that does nothing", async () => {
    listWidth = 250;
    const draft = convert();
    const { rerender } = renderBar({ actions: [approve(), removeMe(), draft] });
    await userEvent.click(
      within(bar()).getByRole("button", { name: "More Actions", hidden: true }),
    );
    rerender({ actions: [approve(), removeMe()] });
    const gone = screen.getByRole("menuitem", { name: "Convert to Draft" });
    expect(gone).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(gone);
    expect(draft.run).not.toHaveBeenCalled();
  });

  it("keeps focus in the bar when More closes with nothing left to list", async () => {
    // Without Open and Copy, More shows only once an action folds into it.
    listWidth = 320;
    renderBar({ moreGroups: [] });
    await userEvent.click(
      within(bar()).getByRole("button", { name: "More Actions", hidden: true }),
    );
    resize(1000);
    await userEvent.keyboard("{Escape}");
    expect(within(bar()).queryByRole("button", { name: "More Actions", hidden: true })).toBeNull();
    expect(document.activeElement).toBe(
      within(bar()).getByRole("button", { name: "Clear Selection", hidden: true }),
    );
  });

  it("refits when a label changes, as a run's progress does", async () => {
    listWidth = 560;
    const { rerender } = renderBar();
    expect(
      within(bar()).getByRole("button", { name: "Approve… (1 of 3)", hidden: true }),
    ).toHaveTextContent("Approve… (1 of 3)");
    // A running label has no short form, so the others shorten to make room.
    const running = action({ id: "approve", label: "Approving 1 of 3 pull requests…" });
    await act(async () => {
      rerender({ actions: [running, removeMe(), convert()] });
    });
    act(runFrames);
    expect(
      within(bar()).getByRole("button", { name: "Remove Me as Reviewer", hidden: true }),
    ).toHaveTextContent(/^Remove Me$/);
  });

  it("brings a folded action back when its count changes, with no resize", async () => {
    // 20 selected (78): Remove Me's 10/20 badge keeps it in More; at 9/20 it fits again.
    listWidth = 328;
    const approved = action({
      id: "approve",
      label: "Approve…",
      shortLabel: "Approve…",
      fits: { acting: 20, of: 20 },
    });
    const removal = (acting: number) =>
      action({
        id: "remove-me",
        label: `Remove Me as Reviewer (${acting} of 20)`,
        shortLabel: "Remove Me",
        icon: UserMinusIcon,
        fits: { acting, of: 20 },
      });
    const { rerender } = renderBar({
      selectedCount: 20,
      actions: [approved, removal(10), convert()],
    });
    expect(barButtons()).not.toContain("Remove Me as Reviewer (10 of 20)");
    // Settled: nothing the first fit set off is still waiting for a frame.
    await act(async () => {});
    act(runFrames);
    expect(frames).toHaveLength(0);
    await act(async () => {
      rerender({ selectedCount: 20, actions: [approved, removal(9), convert()] });
    });
    // Only the copies' text changed; the bar sees it without the list resizing.
    act(runFrames);
    expect(barButtons()).toContain("Remove Me as Reviewer (9 of 20)");
  });

  it("keeps a focused action's button and focus when another leaves the selection", () => {
    listWidth = 350;
    const { rerender } = renderBar();
    const removeButton = within(bar()).getByRole("button", {
      name: "Remove Me as Reviewer",
      hidden: true,
    });
    removeButton.focus();
    // Convert to Draft was in More; it goes, so More holds none of the bar's actions now.
    rerender({ actions: [approve(), removeMe()] });
    act(runFrames);
    expect(document.activeElement).toBe(removeButton);
    expect(barButtons()).toEqual([
      "Approve… (1 of 3)",
      "Remove Me as Reviewer",
      "More Actions",
      "Clear Selection",
    ]);
  });

  it("hands focus to More when an action joining the selection pushes the focused one into it", () => {
    listWidth = 380;
    const { rerender } = renderBar({ actions: [approve(), removeMe()] });
    const removeButton = within(bar()).getByRole("button", {
      name: "Remove Me as Reviewer",
      hidden: true,
    });
    removeButton.focus();
    // A wider newcomer before it: Remove Me no longer fits.
    const close = action({
      id: "close",
      label: "Close every one of these",
      shortLabel: "Close every one",
    });
    rerender({ actions: [approve(), close, removeMe()] });
    expect(document.activeElement).toBe(
      within(bar()).getByRole("button", { name: "More Actions", hidden: true }),
    );
  });

  it("pays for an open More's trigger until it closes, though nothing is folded", async () => {
    listWidth = 320;
    renderBar({ moreGroups: [] });
    await userEvent.click(
      within(bar()).getByRole("button", { name: "More Actions", hidden: true }),
    );
    resize(530);
    const parts = [...bar().children].filter((part) => !part.matches("[data-bar-sizers]"));
    expect(parts.reduce((total, part) => total + widthOf(part), 0)).toBeLessThanOrEqual(listWidth);
    expect(within(bar()).getByRole("button", { name: "More Actions", hidden: true })).toBeTruthy();
  });

  it("measures copies nobody can reach: inert, hidden, with no controls or text", () => {
    const { container } = renderBar({ selectAllCount: 12 });
    const sizers = container.querySelector("[data-bar-sizers]")!;
    expect(sizers).toHaveAttribute("inert");
    expect(sizers).toHaveAttribute("aria-hidden", "true");
    expect(sizers.querySelectorAll("button, [tabindex], a, input")).toHaveLength(0);
    expect(sizers.textContent).toBe("");
    // Select All is the only "Select All 12" a query finds.
    expect(screen.getAllByText("Select All 12")).toHaveLength(1);
  });

  it("shortens Select All to All, keeping its name, before any action moves", () => {
    // Select All 12 (90) and All 12 (54) join the bar: 464 short, 428 with "All".
    listWidth = 430;
    renderBar({ selectAllCount: 12 });
    expect(
      within(bar()).getByRole("button", { name: "Select All 12", hidden: true }),
    ).toHaveTextContent(/^All 12$/);
    expect(barButtons()).toContain("Convert to Draft");
  });

  it("fits once it shows: on the first selection, after Clear, and when a hidden list appears", () => {
    listWidth = 350;
    const first = renderBar();
    expect(barButtons()).not.toContain("Convert to Draft");
    first.unmount();
    expect(observers.every((observer) => observer.disconnected)).toBe(true);
    // Selected again, the new bar fits before anything resizes.
    listWidth = 600;
    renderBar();
    expect(barButtons()).toContain("Convert to Draft");
    cleanup();
    // A hidden list has no width: the bar keeps the fit it had, and fits once the list shows.
    listWidth = 0;
    renderBar();
    expect(barButtons()).toContain("Convert to Draft");
    resize(350);
    expect(barButtons()).not.toContain("Convert to Draft");
  });
});

it("says how many of a selection removal acts on when some rows stay linked", async () => {
  const run = vi.fn();
  renderBar({
    remove: { reason: undefined, keys: "⌫", share: { acting: 2, of: 3 }, run },
  });
  const remove = screen.getByTitle("Remove from This Session (2 of 3)");
  expect(remove).toHaveTextContent("Remove");
  expect(remove).toHaveTextContent("(2 of 3)");
  await userEvent.click(remove);
  expect(run).toHaveBeenCalledOnce();
});
