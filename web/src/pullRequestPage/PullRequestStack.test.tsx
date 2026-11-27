import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { STACK_READ_AHEAD_INTENT_DELAY_MS } from "./useStackReadAheadIntent";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EXAMPLE_STACK_FORMAT, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";
import { parseStack } from "@/lib/pullRequestStacks/stackFormats";
import {
  type StackMember,
  type StackMembersRead,
  stackMemberLookups,
} from "@/lib/pullRequestStacks/stackMembers";
import {
  type NativeStackAnswer,
  partialNativeStack,
  readNativeStack,
} from "@/lib/pullRequestStacks/nativeStack";
import {
  AT_STACK_BASE,
  CHILD_NOT_READ,
  END_NOT_READ,
  NOTHING_STACKED,
  PARENT_NOT_READ,
  POSITION_NOT_READ,
} from "@/lib/pullRequestStacks/stackNavigation";
import { usePaletteCommandsStore } from "@/store/paletteCommands";

import {
  CURRENT_ROW_CLASS,
  PullRequestStackCard,
  PullRequestStackDependency,
  PullRequestStackMenu,
  PullRequestStackScope,
  openOnCurrentStackRow,
  PullRequestStackPending,
  revealCurrentStackRow,
  SEE_WHOLE_STACK,
  STACK_LOADING,
  STACK_LOADING_REST,
  STACK_PARTIAL,
} from "./PullRequestStack";
import {
  partialNativeStackView,
  type PullRequestStackView,
  pullRequestStackView,
} from "./usePullRequestStack";

// Stack descriptions in an example format, which the page reads once a build registers it.
const STACK_FORMATS = [EXAMPLE_STACK_FORMAT];
withPullRequestIntegrations({ stackFormats: STACK_FORMATS });

const shell = vi.hoisted(() => ({ opened: [] as string[] }));
vi.mock("@/lib/openLinkInApp", () => ({
  openLink: (_conversationId: string | undefined, href: string) => shell.opened.push(href),
}));

const prUrl = (number: number) => `https://github.com/acme/app/pull/${number}`;

/**
 * `[depth, number or branch, url?]` per entry: a string is a branch not pushed yet, and a URL
 * puts the PR elsewhere than acme/app on github.com.
 */
type Entry = [number, number | string, string?];

const EXTERNAL_LIB = "https://github.com/other-org/lib/pull/12";
const EXTERNAL_HOST = "https://git.example.com/acme/app/pull/3";

function bodyOf(entries: Entry[], self: number, dependency?: number | string): string {
  const lines = entries.map(([depth, entry, url]) => {
    const indent = " ".repeat((depth + (dependency ? 1 : 0)) * 2);
    if (typeof entry === "string") return `${indent}- ${entry}`;
    if (entry === self) return `${indent}- [**stack/${entry}**](${prUrl(entry)}) ← _this PR_`;
    return `${indent}- [stack/${entry}](${url ?? prUrl(entry)})`;
  });
  if (dependency) {
    const url = typeof dependency === "string" ? dependency : prUrl(dependency);
    lines.unshift(`- [stack/dependency](${url}) ⬆️ (dependency: merge first)`);
  }
  return `## Stacked changes\n${lines.join("\n")}\n\n---------\nThe change.\n`;
}

const member = (number: number, over: Partial<StackMember> = {}): StackMember => ({
  url: prUrl(number),
  number,
  title: `PR ${number}`,
  state: "OPEN",
  is_draft: false,
  updated_at: null,
  review_decision: null,
  head_ref: `stack/${number}`,
  head_sha: null,
  base_ref: "main",
  base_sha: null,
  head_repository: "acme/app",
  base_repository: "acme/app",
  ...over,
});

const loaded = (...members: StackMember[]): StackMembersRead => ({
  kind: "loaded",
  answer: { viewer: "octo", members, missing: [], unread: [] },
});

/** The stack below #100, #101 (this PR), then #103 and an unpushed branch on #101. */
const BRANCHED: Entry[] = [
  [0, 100],
  [1, 101],
  [2, 103],
  [2, "stack/docs"],
];

interface ViewOptions {
  read?: StackMembersRead;
  selectInPlace?: (url: string) => boolean;
  dependency?: number | string;
}

function viewOf(
  entries: Entry[],
  self: number,
  { read = { kind: "unavailable" }, selectInPlace = () => true, dependency }: ViewOptions = {},
) {
  const parse = parseStack(STACK_FORMATS, bodyOf(entries, self, dependency), {
    host: "github.com",
    repository: "acme/app",
    number: self,
  });
  if (!parse) throw new Error("not a stack");
  return pullRequestStackView(parse, read, selectInPlace);
}

let location = "";
function LocationProbe() {
  const current = useLocation();
  location = `${current.pathname}${current.search}`;
  return null;
}

function renderRouted(ui: React.ReactNode) {
  return render(
    <MemoryRouter initialEntries={["/c/session_1"]}>
      {ui}
      <LocationProbe />
    </MemoryRouter>,
  );
}

/** The stack's own rows in its open menu, below the heading and its Previous and Next. */
const stackRows = (menu: HTMLElement) =>
  within(menu.querySelector<HTMLElement>("[data-stack-menu-rows]")!).getAllByRole("menuitem");

const commands = () =>
  Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) => group.commands);

beforeEach(() => {
  shell.opened = [];
});
afterEach(cleanup);

describe("PullRequestStackMenu", () => {
  it("names this PR's level and lists the stack with each member's state", async () => {
    const stack = viewOf(BRANCHED, 101, {
      read: loaded(
        member(100, { title: "Add auth backend", review_decision: "APPROVED" }),
        member(101, { title: "Add auth API" }),
        member(103, { title: "Add login UI", is_draft: true }),
      ),
    });
    renderRouted(<PullRequestStackMenu stack={stack} active />);
    await userEvent.click(screen.getByRole("button", { name: "Stack: level 2 of 3" }));
    const menu = screen.getByRole("menu");
    expect(within(menu).getByText("Stack · 3 PRs")).toBeTruthy();
    const items = stackRows(menu);
    expect(items[0].textContent).toContain("Add auth backend");
    expect(items[0].textContent).toContain("Approved");
    // This PR is marked, and choosing it goes nowhere; a branch not pushed yet can't be opened.
    expect(items[1].getAttribute("aria-current")).toBe("page");
    await userEvent.click(items[1]);
    expect(location).toBe("/c/session_1");
    expect(shell.opened).toEqual([]);
    expect(items[2].textContent).toContain("Draft");
    expect(items[3].textContent).toContain("Not pushed yet");
    expect(items[3].getAttribute("aria-disabled")).toBe("true");
  });

  it("opens a member where this PR shows when that view can show it", async () => {
    const selected: string[] = [];
    const stack = viewOf(BRANCHED, 101, {
      selectInPlace: (url) => {
        selected.push(url);
        return true;
      },
    });
    renderRouted(<PullRequestStackMenu stack={stack} active />);
    await userEvent.click(screen.getByRole("button", { name: "Stack: level 2 of 3" }));
    await userEvent.click(screen.getByRole("menuitem", { name: /stack\/100/ }));
    expect(selected).toEqual([prUrl(100)]);
    expect(location).toBe("/c/session_1");
  });

  it("opens a member on the code host when this view can't show it, from every entry", async () => {
    const stack = viewOf(BRANCHED, 101, { selectInPlace: () => false });
    renderRouted(<PullRequestStackMenu stack={stack} active />);
    await userEvent.click(screen.getByRole("button", { name: "Stack: level 2 of 3" }));
    await userEvent.click(screen.getByRole("menuitem", { name: /stack\/100/ }));
    expect(shell.opened).toEqual([prUrl(100)]);
    cleanup();
    renderRouted(<PullRequestStackMenu stack={stack} active />);
    act(() => commands()[0].run());
    cleanup();
    renderRouted(<PullRequestStackCard stack={stack} />);
    await userEvent.click(screen.getByRole("link", { name: /stack\/100/ }));
    expect(shell.opened).toEqual([prUrl(100), prUrl(100), prUrl(100)]);
    expect(location).toBe("/c/session_1");
  });

  it("tells a tolerated stack's reader it may be out of date", async () => {
    const parse = parseStack(
      STACK_FORMATS,
      `## Stacked changes\n- [stack/100](${prUrl(100)})\n  - [**stack/101**](${prUrl(101)}) ← _this PR_\n\n---\n`,
      { host: "github.com", repository: "acme/app", number: 101 },
    );
    expect(parse?.trust).toBe("tolerated");
    renderRouted(
      <PullRequestStackMenu
        stack={pullRequestStackView(parse!, { kind: "unavailable" }, () => true)}
        active
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Stack: level 2 of 2" }));
    expect(screen.getByText(/Stack info may be out of date/)).toBeTruthy();
  });
});

describe("the stack menu's look", () => {
  // Nine PRs in a straight line, this PR second: the depth the menu once named "↳ level 9".
  const NINE: Entry[] = Array.from({ length: 9 }, (_value, depth) => [depth, 100 + depth]);

  it("lists a straight stack flat, each row led by its place, with no tree or level words", async () => {
    renderRouted(<PullRequestStackMenu stack={viewOf(NINE, 101)} active />);
    await userEvent.click(screen.getByRole("button", { name: "Stack: level 2 of 9" }));
    const items = within(screen.getByRole("menu"))
      .getAllByRole("menuitem")
      .filter((item) => item.textContent?.includes("PR 1") || item.textContent?.includes("stack/"));
    const rows = items.slice(0, 9);
    expect(rows.map((row) => row.textContent?.match(/^\d+/)?.[0])).toEqual([
      "1",
      "2",
      "3",
      "4",
      "5",
      "6",
      "7",
      "8",
      "9",
    ]);
    for (const row of rows) {
      expect(row.textContent).not.toMatch(/[├└│]|level/);
    }
  });

  it("keeps a forked stack's tree, naming a deep row's level as a badge, not in words", async () => {
    // A straight line seven deep, then a second branch on the base.
    const forked: Entry[] = [...NINE.slice(0, 7), [1, 200]];
    renderRouted(<PullRequestStackMenu stack={viewOf(forked, 101)} active />);
    await userEvent.click(screen.getByRole("button", { name: /^Stack: level 2 of/ }));
    const menu = screen.getByRole("menu");
    expect(menu.textContent).toMatch(/[├└]/);
    expect(menu.textContent).not.toContain("level 6");
    const badge = within(menu).getByTitle("Level 6");
    expect(badge.textContent).toBe("6");
  });

  for (const [shape, entries] of [
    [
      "straight",
      [
        [0, 100],
        [1, 101],
        [2, 102],
      ],
    ],
    [
      "forked",
      [
        [0, 100],
        [1, 101],
        [2, 102],
        [1, 103],
      ],
    ],
  ] as const) {
    it(`picks a ${shape} stack's row by typing its title, past its step and tree`, async () => {
      const stack = viewOf(entries as unknown as Entry[], 101, {
        read: loaded(
          member(100, { title: "Add auth backend" }),
          member(101, { title: "Current" }),
          member(102, { title: "Build UI" }),
          member(103, { title: "Docs" }),
        ),
      });
      renderRouted(<PullRequestStackMenu stack={stack} active />);
      await userEvent.click(screen.getByRole("button", { name: /^Stack: level 2 of/ }));
      const menu = screen.getByRole("menu");
      // Past the frame the menu opens on this PR's row in.
      await act(() => new Promise(requestAnimationFrame));
      const row = (title: string) =>
        within(menu)
          .getAllByRole("menuitem")
          .find((item) => item.textContent?.includes(title))!;
      act(() => row("Build UI").focus());
      await userEvent.keyboard("a");
      expect(document.activeElement).toBe(row("Add auth backend"));
    });
  }
});

describe("Previous and Next in stack in ⌘K", () => {
  it("offers both while the page is active, greying out what can't run with why", () => {
    const stack = viewOf(
      [
        [0, 100],
        [1, 101],
      ],
      100,
    );
    const { rerender } = renderRouted(<PullRequestStackMenu stack={stack} active />);
    const [previous, next] = commands();
    expect(previous).toMatchObject({
      label: "Previous in stack",
      availability: { available: false, reason: AT_STACK_BASE },
    });
    expect(next).toMatchObject({
      label: "Next in stack (#101)",
      availability: { available: true },
    });
    rerender(
      <MemoryRouter>
        <PullRequestStackMenu stack={stack} active={false} />
      </MemoryRouter>,
    );
    expect(commands()).toEqual([]);
  });

  it("keeps Previous for a stack read loosely from its description", () => {
    const parse = parseStack(
      STACK_FORMATS,
      `## Stacked changes\n- [stack/100](${prUrl(100)})\n  - [**stack/101**](${prUrl(101)}) ← _this PR_\n\n---\n`,
      { host: "github.com", repository: "acme/app", number: 101 },
    );
    expect(parse?.trust).toBe("tolerated");
    renderRouted(
      <PullRequestStackMenu
        stack={pullRequestStackView(parse!, { kind: "unavailable" }, () => true)}
        active
      />,
    );
    expect(commands()[0]).toMatchObject({
      label: "Previous in stack (#100)",
      availability: { available: true },
    });
  });

  it("runs Next on the only child, and greys it out at the top of the stack", () => {
    const selected: string[] = [];
    const stack = viewOf(
      [
        [0, 100],
        [1, 101],
      ],
      100,
      {
        selectInPlace: (url) => {
          selected.push(url);
          return true;
        },
      },
    );
    renderRouted(<PullRequestStackMenu stack={stack} active />);
    commands()[1].run();
    expect(selected).toEqual([prUrl(101)]);
    cleanup();
    renderRouted(
      <PullRequestStackMenu
        stack={viewOf(
          [
            [0, 100],
            [1, 101],
          ],
          101,
        )}
        active
      />,
    );
    expect(commands()[1].availability).toEqual({ available: false, reason: NOTHING_STACKED });
  });

  it("opens the stack menu to pick from when several PRs are stacked on this one", async () => {
    const stack = viewOf(
      [
        [0, 100],
        [1, 101],
        [1, 102],
      ],
      100,
    );
    renderRouted(<PullRequestStackMenu stack={stack} active />);
    const next = commands()[1];
    expect(next.label).toBe("Next in stack…");
    next.run();
    const menu = await screen.findByRole("menu");
    // The menu lists the PRs stacked on this one to pick from.
    expect(within(menu).getByRole("menuitem", { name: /stack\/101/ })).toBeTruthy();
    expect(within(menu).getByRole("menuitem", { name: /stack\/102/ })).toBeTruthy();
  });

  it("names each row's key, as the menu's hint does", () => {
    const stack = viewOf(BRANCHED, 101);
    renderRouted(<PullRequestStackMenu stack={stack} active />);
    // First and Last in stack have no keys of their own.
    expect(commands().map((command) => command.shortcut)).toEqual([
      "pullRequestStack.previous",
      "pullRequestStack.next",
      undefined,
      undefined,
    ]);
  });
});

describe("⇧J and ⇧K", () => {
  const press = (key: "J" | "K", target: Element | Window = window) =>
    fireEvent.keyDown(target, { key, code: `Key${key}`, shiftKey: true });

  it("move down and up the stack, as the ⌘K rows do", () => {
    const selected: string[] = [];
    const straight: Entry[] = [
      [0, 100],
      [1, 101],
      [2, 102],
    ];
    const stack = viewOf(straight, 101, {
      selectInPlace: (url) => {
        selected.push(url);
        return true;
      },
    });
    renderRouted(<PullRequestStackMenu stack={stack} active />);
    press("K");
    press("J");
    expect(selected).toEqual([prUrl(100), prUrl(102)]);
  });

  it("open the menu to pick from where several PRs are stacked on this one", async () => {
    const stack = viewOf(
      [
        [0, 100],
        [1, 101],
        [1, 102],
      ],
      100,
    );
    renderRouted(<PullRequestStackMenu stack={stack} active />);
    press("J");
    const menu = await screen.findByRole("menu");
    expect(within(menu).getByRole("menuitem", { name: /stack\/101/ })).toBeTruthy();
  });

  it("leave the keys to typing, and to a step that can't run", () => {
    const selected: string[] = [];
    const selectInPlace = (url: string) => {
      selected.push(url);
      return true;
    };
    // The base has nothing before it.
    renderRouted(
      <>
        <input aria-label="Comment" />
        <PullRequestStackMenu stack={viewOf(BRANCHED, 100, { selectInPlace })} active />
      </>,
    );
    const field = screen.getByRole("textbox", { name: "Comment" });
    field.focus();
    expect(press("J", field)).toBe(true);
    field.blur();
    expect(press("K")).toBe(true);
    expect(selected).toEqual([]);
  });

  it("do nothing while the page isn't active", () => {
    const selected: string[] = [];
    const stack = viewOf(BRANCHED, 101, {
      selectInPlace: (url) => {
        selected.push(url);
        return true;
      },
    });
    renderRouted(<PullRequestStackMenu stack={stack} active={false} />);
    press("K");
    expect(selected).toEqual([]);
  });

  it("are named in the menu, whose rows are only the stack's", async () => {
    renderRouted(<PullRequestStackMenu stack={viewOf(BRANCHED, 101)} active />);
    await userEvent.click(screen.getByRole("button", { name: "Stack: level 2 of 3" }));
    const menu = screen.getByRole("menu");
    expect(
      stackRows(menu).filter((row) => /in stack/.test(row.getAttribute("aria-label") ?? "")),
    ).toEqual([]);
    const hint = menu.querySelector("[data-stack-keys-hint]");
    expect(hint?.textContent).toMatch(/J.*Next.*K.*Previous/);
  });
});

describe("this PR's row", () => {
  const marked = (element: Element) =>
    CURRENT_ROW_CLASS.split(" ").every((name) => element.classList.contains(name));

  it("is marked in the menu and on the card, and no other row is", async () => {
    const stack = viewOf(BRANCHED, 101);
    renderRouted(
      <>
        <PullRequestStackMenu stack={stack} active />
        <PullRequestStackCard stack={stack} />
      </>,
    );
    const card = screen.getByRole("navigation", { name: "Stack" });
    const cardRows = [...card.querySelectorAll("li > *")];
    expect(cardRows.map(marked)).toEqual([false, true, false, false]);
    await userEvent.click(screen.getByRole("button", { name: "Stack: level 2 of 3" }));
    const menuRows = stackRows(screen.getByRole("menu"));
    expect(menuRows.map(marked)).toEqual([false, true, false, false]);
    expect(menuRows[1]).toHaveAttribute("aria-current", "page");
  });
});

describe("revealCurrentStackRow", () => {
  const ROW = 20;
  const VIEW = 100;

  /** A scrolling region of `count` 20px rows showing 100px, with `current` this PR's. */
  function region(count: number, current: number) {
    const element = document.createElement("div");
    element.getBoundingClientRect = () => new DOMRect(0, 0, 300, VIEW);
    const rows = Array.from({ length: count }, (_, index) => {
      const row = document.createElement("div");
      row.setAttribute("role", "menuitem");
      row.tabIndex = -1;
      if (index === current) {
        row.setAttribute("aria-current", "page");
        row.setAttribute("data-disabled", "");
      }
      row.getBoundingClientRect = () => new DOMRect(0, index * ROW - element.scrollTop, 300, ROW);
      element.append(row);
      return row;
    });
    document.body.append(element);
    return { element, rows };
  }

  afterEach(() => {
    document.body.replaceChildren();
  });

  it("centres a row out of view and leaves one in view where it is", () => {
    const deep = region(40, 34);
    revealCurrentStackRow(deep.element);
    // Row 34's middle (690px) at the view's middle (50px).
    expect(deep.element.scrollTop).toBe(34 * ROW + ROW / 2 - VIEW / 2);
    document.body.replaceChildren();
    const shallow = region(40, 2);
    revealCurrentStackRow(shallow.element);
    expect(shallow.element.scrollTop).toBe(0);
  });

  it("leaves focus where the menu put it, in view or not", () => {
    const deep = region(40, 34);
    deep.rows[0].focus();
    revealCurrentStackRow(deep.element);
    expect(document.activeElement).toBe(deep.rows[0]);
  });
});

describe("openOnCurrentStackRow", () => {
  /** A menu with the heading's Previous item, then `count` rows, this PR's at `current`. */
  function rows(count: number, current: number) {
    const menu = document.createElement("div");
    menu.setAttribute("role", "menu");
    const previous = document.createElement("div");
    previous.setAttribute("role", "menuitem");
    previous.tabIndex = -1;
    const element = document.createElement("div");
    const items = Array.from({ length: count }, (_, index) => {
      const row = document.createElement("div");
      row.setAttribute("role", "menuitem");
      row.tabIndex = -1;
      if (index === current) row.setAttribute("aria-current", "page");
      element.append(row);
      return row;
    });
    menu.append(previous, element);
    document.body.append(menu);
    return { menu, previous, element, items };
  }

  afterEach(() => {
    document.body.replaceChildren();
  });

  it("moves the keyboard's first row to this PR's, as a native select opens", () => {
    const menu = rows(40, 34);
    menu.items[0].focus();
    openOnCurrentStackRow(menu.element);
    expect(document.activeElement).toBe(menu.items[34]);
  });

  it("moves the keyboard's first item, the heading's Previous, to this PR's row", () => {
    const menu = rows(40, 34);
    menu.previous.focus();
    openOnCurrentStackRow(menu.element);
    expect(document.activeElement).toBe(menu.items[34]);
  });

  it("leaves a click's focus on the menu itself", () => {
    const menu = rows(40, 34);
    menu.menu.tabIndex = -1;
    menu.menu.focus();
    openOnCurrentStackRow(menu.element);
    expect(document.activeElement).toBe(menu.menu);
  });
});

describe("Previous and Next in the stack menu", () => {
  const STRAIGHT: Entry[] = [
    [0, 100],
    [1, 101],
    [2, 102],
  ];
  /** The stack menu, opened from the header's switcher. */
  async function openMenu() {
    await userEvent.click(screen.getByRole("button", { name: /^Stack/ }));
    return screen.findByRole("menu");
  }

  it("sit in the menu's heading row, not the header, and step as ⌘K's rows do", async () => {
    const selected: string[] = [];
    const stack = viewOf(STRAIGHT, 101, {
      selectInPlace: (url) => {
        selected.push(url);
        return true;
      },
    });
    renderRouted(<PullRequestStackMenu stack={stack} active />);
    // The header holds only the switcher.
    expect(screen.queryByRole("button", { name: /in stack/ })).toBeNull();
    const menu = await openMenu();
    const heading = menu.querySelector<HTMLElement>("[data-stack-menu-heading]")!;
    expect(heading).toHaveTextContent("Stack · 3 PRs");
    const steps = within(heading).getAllByRole("menuitem");
    expect(steps.map((step) => step.getAttribute("aria-label"))).toEqual([
      "Previous in stack (#100)",
      "Next in stack (#102)",
    ]);
    // ⌘K lists the same two steps under the same names.
    expect(
      commands()
        .filter((command) => /^(Previous|Next) in stack/.test(command.label))
        .map((command) => command.label),
    ).toEqual(["Previous in stack (#100)", "Next in stack (#102)"]);
    await userEvent.click(steps[0]!);
    expect(selected).toEqual([prUrl(100)]);
    expect(screen.queryByRole("menu")).toBeNull();
    await userEvent.click(
      within(await openMenu()).getByRole("menuitem", { name: "Next in stack (#102)" }),
    );
    expect(selected).toEqual([prUrl(100), prUrl(102)]);
  });

  it("are reached with the arrow keys from the first row and run with Enter", async () => {
    const selected: string[] = [];
    const stack = viewOf(STRAIGHT, 101, {
      selectInPlace: (url) => {
        selected.push(url);
        return true;
      },
    });
    renderRouted(<PullRequestStackMenu stack={stack} active />);
    screen.getByRole("button", { name: /^Stack/ }).focus();
    await userEvent.keyboard("{Enter}");
    await screen.findByRole("menu");
    // The menu opens on this PR's row; Next and Previous are the items above the rows.
    await waitFor(() => expect(document.activeElement).toHaveAttribute("aria-current", "page"));
    await userEvent.keyboard("{ArrowUp}{ArrowUp}");
    expect(document.activeElement).toHaveAttribute("aria-label", "Next in stack (#102)");
    await userEvent.keyboard("{ArrowUp}");
    expect(document.activeElement).toHaveAttribute("aria-label", "Previous in stack (#100)");
    await userEvent.keyboard("{Enter}");
    expect(selected).toEqual([prUrl(100)]);
  });

  it("are disabled at the ends of the stack with why, and skipped by the arrow keys", async () => {
    const selected: string[] = [];
    const stack = viewOf(STRAIGHT, 100, {
      selectInPlace: (url) => {
        selected.push(url);
        return true;
      },
    });
    renderRouted(<PullRequestStackMenu stack={stack} active />);
    const menu = await openMenu();
    const previous = within(menu).getByRole("menuitem", { name: /^Previous in stack/ });
    expect(previous).toHaveAttribute("aria-disabled", "true");
    expect(previous).toHaveAttribute("aria-label", `Previous in stack: ${AT_STACK_BASE}`);
    // The item itself takes no pointer, so hovering where it is shows why from its wrapper.
    expect(previous.className).toContain("data-disabled:pointer-events-none");
    const wrapper = previous.closest<HTMLElement>("[data-stack-step-reason]")!;
    expect(wrapper).not.toHaveAttribute("tabindex");
    await userEvent.hover(wrapper);
    expect((await screen.findByRole("tooltip")).textContent).toContain(AT_STACK_BASE);
    await userEvent.click(previous, { pointerEventsCheck: 0 });
    expect(selected).toEqual([]);
    // Next can step, so it's no wrapped tooltip of reasons.
    expect(
      within(menu)
        .getByRole("menuitem", { name: "Next in stack (#101)" })
        .closest("[data-stack-step-reason]"),
    ).toBeNull();
    // From Next, up goes past the disabled Previous, round to the menu's last item.
    within(menu).getByRole("menuitem", { name: "Next in stack (#101)" }).focus();
    await userEvent.keyboard("{ArrowUp}");
    expect(document.activeElement).not.toBe(previous);
    // At the top, Next is the one that can't step.
    cleanup();
    renderRouted(<PullRequestStackMenu stack={viewOf(STRAIGHT, 102)} active />);
    const top = await openMenu();
    expect(within(top).getByRole("menuitem", { name: /^Next in stack/ })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(
      within(top).getByRole("menuitem", { name: "Previous in stack (#101)" }),
    ).not.toHaveAttribute("aria-disabled");
  });

  it("keep the menu open on its rows where several PRs are stacked on this one", async () => {
    const stack = viewOf(
      [
        [0, 100],
        [1, 101],
        [1, 102],
      ],
      100,
    );
    renderRouted(<PullRequestStackMenu stack={stack} active />);
    const menu = await openMenu();
    await userEvent.click(within(menu).getByRole("menuitem", { name: "Next in stack…" }));
    expect(screen.getByRole("menu")).toBe(menu);
    expect(within(menu).getByRole("menuitem", { name: /stack\/102/ })).toBeTruthy();
  });

  it("add no ⌘K rows or keys of their own on an inactive page", async () => {
    renderRouted(<PullRequestStackMenu stack={viewOf(STRAIGHT, 101)} active={false} />);
    const menu = await openMenu();
    expect(within(menu).getAllByRole("menuitem", { name: /in stack/ })).toHaveLength(2);
    expect(commands()).toEqual([]);
  });
});

describe("PullRequestStackDependency", () => {
  it("names the PR to merge first and opens it", async () => {
    const selected: string[] = [];
    const stack = viewOf(
      [
        [0, 101],
        [1, 102],
      ],
      101,
      {
        dependency: 99,
        read: loaded(member(99, { title: "Bump the client", state: "MERGED" })),
        selectInPlace: (url) => {
          selected.push(url);
          return true;
        },
      },
    );
    renderRouted(<PullRequestStackDependency stack={stack} />);
    const chip = screen.getByRole("button", { name: "Depends on #99: Bump the client · Merged" });
    expect(chip.textContent).toContain("depends on #99");
    await userEvent.click(chip);
    expect(selected).toEqual([prUrl(99)]);
  });

  it("shows nothing for a stack without a dependency", () => {
    const { container } = renderRouted(
      <PullRequestStackDependency stack={viewOf(BRANCHED, 101)} />,
    );
    expect(container.querySelector("button")).toBeNull();
  });
});

describe("PullRequestStackCard", () => {
  it("lists every entry with its level, opening the others and marking this PR", async () => {
    const selected: string[] = [];
    const stack = viewOf(BRANCHED, 101, {
      read: loaded(member(100, { state: "MERGED" })),
      selectInPlace: (url) => {
        selected.push(url);
        return true;
      },
    });
    renderRouted(<PullRequestStackCard stack={stack} />);
    const card = screen.getByRole("navigation", { name: "Stack" });
    const rows = within(card).getAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringMatching(/^1.*PR 100.*#100.*Merged$/),
      expect.stringMatching(/^2.*stack\/101.*#101.*This PR$/),
      expect.stringMatching(/^3.*stack\/103.*#103.*Status unavailable$/),
      expect.stringMatching(/^3.*stack\/docs.*Not pushed yet$/),
    ]);
    // This PR and the branch not pushed yet are text, not links.
    expect(within(rows[1]).queryByRole("link")).toBeNull();
    expect(within(rows[3]).queryByRole("link")).toBeNull();
    const link = within(rows[2]).getByRole("link");
    expect(link.getAttribute("href")).toBe(prUrl(103));
    await userEvent.click(link);
    expect(selected).toEqual([prUrl(103)]);
  });
});

describe("entries outside this repository", () => {
  const MIXED: Entry[] = [
    [0, 100],
    [1, 101],
    [2, 12, EXTERNAL_LIB],
    [2, 3, EXTERNAL_HOST],
  ];

  it("are never read, and open their own links in the browser from the menu", async () => {
    const selected: string[] = [];
    const stack = viewOf(MIXED, 101, {
      selectInPlace: (url) => {
        selected.push(url);
        return true;
      },
    });
    expect(stack.parse.trust).toBe("verified");
    expect(stackMemberLookups(stack.parse)).toEqual([101, 100]);
    renderRouted(<PullRequestStackMenu stack={stack} active />);
    await userEvent.click(screen.getByRole("button", { name: "Stack: level 2 of 3" }));
    const lib = screen.getByRole("menuitem", { name: /other-org\/lib#12/ });
    expect(lib.getAttribute("aria-disabled")).toBeNull();
    expect(screen.getByRole("menuitem", { name: /git\.example\.com\/acme\/app#3/ })).toBeTruthy();
    await userEvent.click(lib);
    expect(shell.opened).toEqual([EXTERNAL_LIB]);
    expect(selected).toEqual([]);
    expect(location).toBe("/c/session_1");
  });

  it("stay links on the card, opened in the browser and never in place", async () => {
    const selected: string[] = [];
    const stack = viewOf(MIXED, 101, {
      selectInPlace: (url) => {
        selected.push(url);
        return true;
      },
    });
    renderRouted(<PullRequestStackCard stack={stack} />);
    const host = screen.getByRole("link", { name: /git\.example\.com\/acme\/app#3/ });
    expect(host.getAttribute("href")).toBe(EXTERNAL_HOST);
    await userEvent.click(host);
    expect(shell.opened).toEqual([EXTERNAL_HOST]);
    expect(selected).toEqual([]);
  });

  it("include a dependency elsewhere, which opens in the browser", async () => {
    const stack = viewOf(
      [
        [0, 101],
        [1, 102],
      ],
      101,
      { dependency: EXTERNAL_LIB },
    );
    expect(stackMemberLookups(stack.parse)).toEqual([101, 102]);
    renderRouted(<PullRequestStackDependency stack={stack} />);
    await userEvent.click(screen.getByRole("button", { name: /Depends on other-org\/lib#12/ }));
    expect(shell.opened).toEqual([EXTERNAL_LIB]);
    expect(location).toBe("/c/session_1");
  });
});

describe("a card row's link", () => {
  it("routes a plain click once, and leaves a modified or middle click to the browser", () => {
    const selected: string[] = [];
    const stack = viewOf(BRANCHED, 101, {
      selectInPlace: (url) => {
        selected.push(url);
        return true;
      },
    });
    renderRouted(<PullRequestStackCard stack={stack} />);
    const link = screen.getByRole("link", { name: /#100/ });
    // `fireEvent` reports whether the click went uncancelled, so the browser would follow it.
    for (const modifier of [
      { metaKey: true },
      { ctrlKey: true },
      { shiftKey: true },
      { altKey: true },
    ]) {
      expect(fireEvent.click(link, modifier)).toBe(true);
    }
    expect(fireEvent.click(link, { button: 1 })).toBe(true);
    expect(selected).toEqual([]);
    expect(fireEvent.click(link)).toBe(false);
    expect(selected).toEqual([prUrl(100)]);
    expect(shell.opened).toEqual([]);
  });
});

describe("the dependency", () => {
  const STACKED: Entry[] = [
    [0, 101],
    [1, 102],
  ];

  it("has its own row in the menu and the card, outside the stack's count", async () => {
    const selected: string[] = [];
    const stack = viewOf(STACKED, 101, {
      dependency: 99,
      read: loaded(member(99, { title: "Bump the client" }), member(101), member(102)),
      selectInPlace: (url) => {
        selected.push(url);
        return true;
      },
    });
    renderRouted(
      <>
        <PullRequestStackMenu stack={stack} active />
        <PullRequestStackCard stack={stack} />
      </>,
    );
    const card = screen.getByRole("navigation", { name: "Stack" });
    // Its whole text, and as its tooltip where a narrow card shortens it.
    expect(within(card).getByTitle("Stack · 2 PRs · merges from the top")).toHaveTextContent(
      "Stack · 2 PRs · merges from the top",
    );
    expect(within(card).getByText("Depends on")).toBeTruthy();
    expect(
      within(card)
        .getByRole("link", { name: /Bump the client/ })
        .getAttribute("href"),
    ).toBe(prUrl(99));
    await userEvent.click(screen.getByRole("button", { name: "Stack: level 1 of 2" }));
    const menu = screen.getByRole("menu");
    expect(within(menu).getByText("Stack · 2 PRs")).toBeTruthy();
    await userEvent.click(within(menu).getByRole("menuitem", { name: /Bump the client/ }));
    expect(selected).toEqual([prUrl(99)]);
  });

  it.each([
    ["merged", loaded(member(99, { state: "MERGED" })), "Merged"],
    ["unread", { kind: "unavailable" } as StackMembersRead, "Status unavailable"],
  ])("never tells anyone to merge it, %s", (_name, read, status) => {
    const stack = viewOf(STACKED, 101, { dependency: 99, read });
    renderRouted(
      <>
        <PullRequestStackDependency stack={stack} />
        <PullRequestStackCard stack={stack} />
      </>,
    );
    expect(screen.getByRole("button", { name: new RegExp(`^Depends on #99: .+ · ${status}$`) }));
    expect(document.body.textContent).not.toMatch(/merge first/i);
  });

  it("never tells anyone to merge it when the stack section is tolerated", () => {
    const parse = parseStack(
      STACK_FORMATS,
      `## Stacked changes\n- [dep](${prUrl(99)}) ⬆️ (dependency: merge first)\n  - [**stack/101**](${prUrl(101)}) ← _this PR_\n\n---\n`,
      { host: "github.com", repository: "acme/app", number: 101 },
    );
    expect(parse?.trust).toBe("tolerated");
    renderRouted(
      <PullRequestStackDependency
        stack={pullRequestStackView(parse!, { kind: "unavailable" }, () => true)}
      />,
    );
    expect(screen.getByRole("button").getAttribute("aria-label")).toBe(
      "Depends on #99: dep · Status unavailable",
    );
  });
});

describe("how much of the stack was read", () => {
  it("shows in the menu and on the card until every status is in", async () => {
    const partial = viewOf(BRANCHED, 101, { read: loaded(member(100), member(101)) });
    const { unmount } = renderRouted(
      <>
        <PullRequestStackMenu stack={partial} active />
        <PullRequestStackCard stack={partial} />
      </>,
    );
    const card = screen.getByRole("navigation", { name: "Stack" });
    expect(within(card).getByText("Status loaded for 2 of 3")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Stack: level 2 of 3" }));
    expect(within(screen.getByRole("menu")).getByText("Status loaded for 2 of 3")).toBeTruthy();
    // Every entry stays listed, read or not.
    expect(stackRows(screen.getByRole("menu"))).toHaveLength(4);
    unmount();
    const complete = viewOf(BRANCHED, 101, {
      read: loaded(member(100), member(101), member(103)),
    });
    renderRouted(<PullRequestStackCard stack={complete} />);
    expect(document.querySelector("[data-stack-coverage]")).toBeNull();
  });
});

describe("a GitHub stack read only in part", () => {
  const SELF = { url: prUrl(102), host: "github.com", repository: "acme/app", number: 102 };
  const nativeMember = (number: number) => ({
    url: prUrl(number),
    number,
    state: "OPEN",
    isDraft: false,
    headRefName: `stack/${number}`,
  });
  /** GitHub's answer listing `positions` of a stack of `size`, this PR at `selfAt`. */
  function partialView(
    positions: number[],
    selfAt: number | null,
    size: number | null = 60,
  ): PullRequestStackView {
    const answer: NativeStackAnswer = {
      stackEntry: selfAt === null ? null : { position: selfAt },
      stack:
        size === null
          ? null
          : {
              id: "PRS_1",
              number: 7,
              size,
              baseRefName: "main",
              entries: {
                hasNextPage: true,
                nodes: positions.map((position) => ({
                  position,
                  pullRequest: nativeMember(position === selfAt ? 102 : 47 + position),
                })),
              },
            },
    };
    const read = readNativeStack(answer, SELF);
    if (read.kind !== "stacked-partial") throw new Error(read.kind);
    return partialNativeStackView(
      partialNativeStack(read, SELF, "stack/102"),
      SELF.url,
      { kind: "unavailable" },
      () => true,
    );
  }
  const rowNumbers = () =>
    [...screen.getByRole("navigation", { name: "Stack" }).querySelectorAll("ol > li")].map(
      (row) => row.querySelector("span[aria-hidden]")?.textContent,
    );

  it("goes to an end only where the rows read reach it", () => {
    const middle = partialView([54, 55, 56], 55);
    expect(middle.first).toEqual({ kind: "unavailable", reason: END_NOT_READ });
    expect(middle.last).toEqual({ kind: "unavailable", reason: END_NOT_READ });
    const reachesTop = partialView([59, 60], 59);
    expect(reachesTop.last).toMatchObject({ kind: "go", destination: { url: prUrl(107) } });
    expect(reachesTop.first).toEqual({ kind: "unavailable", reason: END_NOT_READ });
    // A stack of unknown size has no last position to reach.
    expect(partialView([1, 2], 2, null).last).toEqual({
      kind: "unavailable",
      reason: END_NOT_READ,
    });
  });

  it("keeps GitHub's position and size for a window in the middle of the stack", async () => {
    const stack = partialView([54, 55, 56], 55);
    renderRouted(
      <>
        <PullRequestStackMenu stack={stack} active />
        <PullRequestStackCard stack={stack} />
      </>,
    );
    expect(rowNumbers()).toEqual(["54", "55", "56"]);
    const [previous, next] = commands();
    expect(previous).toMatchObject({ label: "Previous in stack (#101)" });
    expect(next).toMatchObject({ label: "Next in stack (#103)" });
    const switcher = screen.getByRole("button", { name: "Stack: level 55 of 60" });
    expect(switcher.textContent).toContain("55/60");
  });

  it("says a neighbor wasn't read rather than that the stack ends there", () => {
    renderRouted(<PullRequestStackMenu stack={partialView([55], 55)} active />);
    const [previous, next] = commands();
    expect(previous.availability).toEqual({ available: false, reason: PARENT_NOT_READ });
    expect(next.availability).toEqual({ available: false, reason: CHILD_NOT_READ });
  });

  it("still says where the stack really ends", () => {
    renderRouted(<PullRequestStackMenu stack={partialView([1], 1)} active />);
    expect(commands()[0].availability).toEqual({ available: false, reason: AT_STACK_BASE });
    cleanup();
    renderRouted(<PullRequestStackMenu stack={partialView([60], 60)} active />);
    expect(commands()[1].availability).toEqual({ available: false, reason: NOTHING_STACKED });
  });

  it("names no level and moves nowhere when this PR's position is unknown", () => {
    const stack = partialView([54, 55, 56], null);
    renderRouted(
      <>
        <PullRequestStackMenu stack={stack} active />
        <PullRequestStackCard stack={stack} />
      </>,
    );
    const switcher = screen.getByRole("button", { name: "Stack" });
    expect(switcher.textContent).not.toMatch(/\d/);
    expect(rowNumbers()).toEqual([""]);
    const [previous, next] = commands();
    expect(previous.availability).toEqual({ available: false, reason: POSITION_NOT_READ });
    expect(next.availability).toEqual({ available: false, reason: POSITION_NOT_READ });
  });

  it("counts no PRs when GitHub didn't say how many there are", () => {
    const stack = partialView([], 2, null);
    renderRouted(
      <>
        <PullRequestStackMenu stack={stack} active />
        <PullRequestStackCard stack={stack} />
      </>,
    );
    const card = screen.getByRole("navigation", { name: "Stack" });
    expect(card.textContent).not.toContain("PR ·");
    expect(card.textContent).not.toMatch(/Stack · \d/);
    const switcher = screen.getByRole("button", { name: "Stack: level 2" });
    expect(switcher.textContent).toContain("2");
    expect(switcher.textContent).not.toContain("/");
  });

  it("counts GitHub's whole stack, says only part was read, and opens it on GitHub", async () => {
    renderRouted(<PullRequestStackCard stack={partialView([54, 55, 56], 55)} />);
    const card = screen.getByRole("navigation", { name: "Stack" });
    expect(card.textContent).toContain("Stack · 60 PRs");
    expect(card.textContent).toContain(STACK_PARTIAL);
    await userEvent.click(screen.getByRole("button", { name: SEE_WHOLE_STACK }));
    expect(shell.opened).toEqual([prUrl(102)]);
  });

  it("offers the whole stack on GitHub from the stack menu too", async () => {
    renderRouted(<PullRequestStackMenu stack={partialView([54, 55, 56], 55)} active />);
    await userEvent.click(screen.getByRole("button", { name: "Stack: level 55 of 60" }));
    expect(screen.getByText(STACK_PARTIAL)).toBeTruthy();
    await userEvent.click(screen.getByRole("menuitem", { name: SEE_WHOLE_STACK }));
    expect(shell.opened).toEqual([prUrl(102)]);
  });

  it("says the rest of a stack shown in part is loading, in the menu and the card", async () => {
    const stack = { ...partialView([54, 55, 56], 55), completing: true };
    renderRouted(
      <>
        <PullRequestStackMenu stack={stack} active />
        <PullRequestStackCard stack={stack} />
      </>,
    );
    const card = screen.getByRole("navigation", { name: "Stack" });
    expect(card.textContent).toContain(STACK_LOADING_REST);
    expect(card.textContent).not.toContain(STACK_PARTIAL);
    await userEvent.click(screen.getByRole("button", { name: "Stack: level 55 of 60" }));
    const menu = screen.getByRole("menu");
    expect(within(menu).getByRole("status").textContent).toContain(STACK_LOADING_REST);
    expect(within(menu).queryByText(STACK_PARTIAL)).toBeNull();
    expect(within(menu).queryByRole("menuitem", { name: SEE_WHOLE_STACK })).toBeNull();
  });
});

describe("PullRequestStackScope", () => {
  it("keeps the menu open as the loaded header's takes the first one's place, for that PR only", async () => {
    const stack = viewOf(BRANCHED, 101);
    const ui = (selectedUrl: string, key: string) => (
      <PullRequestStackScope selectedUrl={selectedUrl}>
        <PullRequestStackMenu key={key} stack={stack} active />
      </PullRequestStackScope>
    );
    const { rerender } = renderRouted(ui(prUrl(101), "preview"));
    await userEvent.click(screen.getByRole("button", { name: /^Stack/ }));
    expect(screen.getByRole("menu")).toBeTruthy();
    // Another instance of the menu, as the loaded header renders, opens as the first was.
    rerender(<MemoryRouter>{ui(prUrl(101), "loaded")}</MemoryRouter>);
    expect(screen.getByRole("menu")).toBeTruthy();
    // Another PR shown, then this one again: closed.
    rerender(<MemoryRouter>{ui(prUrl(102), "loaded")}</MemoryRouter>);
    rerender(<MemoryRouter>{ui(prUrl(101), "again")}</MemoryRouter>);
    expect(screen.queryByRole("menu")).toBeNull();
  });
});

describe("a long stack's Overview card", () => {
  // A straight stack of #200–#239; this PR is the 11th, #210.
  const LONG: Entry[] = Array.from({ length: 40 }, (_, index) => [index, 200 + index]);
  const SELF_URL = prUrl(210);
  const card = () => screen.getByRole("navigation", { name: "Stack" });
  /** The step number of each PR row the card shows, top to bottom. */
  const shownSteps = () =>
    [...card().querySelectorAll("ol > li[data-stack-row-key]")].map((row) =>
      Number(row.querySelector("span[aria-hidden]")?.textContent),
    );
  const FOLDED_STEPS = [1, 8, 9, 10, 11, 12, 13, 14, 40];

  /** GitHub's own read of the same 40 PRs, as a late native read replaces the description's. */
  function nativeLong(): PullRequestStackView {
    const self = { url: SELF_URL, host: "github.com", repository: "acme/app", number: 210 };
    const read = readNativeStack(
      {
        stackEntry: { position: 11 },
        stack: {
          id: "PRS_long",
          number: 3,
          size: 40,
          baseRefName: "main",
          entries: {
            hasNextPage: false,
            nodes: LONG.map(([, number], index) => ({
              position: index + 1,
              pullRequest: {
                url: prUrl(number as number),
                number: number as number,
                state: "OPEN",
                isDraft: false,
                headRefName: `stack/${number}`,
              },
            })),
          },
        },
      },
      self,
    );
    if (read.kind !== "stacked") throw new Error(read.kind);
    return pullRequestStackView(read.parse, { kind: "unavailable" }, () => true);
  }

  it("folds to the base, this PR with three on each side, and the top", () => {
    renderRouted(<PullRequestStackCard stack={viewOf(LONG, 210)} />);
    expect(shownSteps()).toEqual(FOLDED_STEPS);
    expect(card().querySelector("[aria-current=page]")?.textContent).toContain("#210");
  });

  it("names each gap by what it shows and where, with its drawing hidden", () => {
    renderRouted(<PullRequestStackCard stack={viewOf(LONG, 210)} />);
    const gaps = within(card()).getAllByRole("button", { name: /^Show \d+ more/ });
    expect(gaps.map((gap) => gap.getAttribute("aria-label"))).toEqual([
      "Show 6 more pull requests between #200 and #207",
      "Show 25 more pull requests between #213 and #239",
    ]);
    for (const gap of gaps) {
      expect(gap.tagName).toBe("BUTTON");
      expect(
        [...gap.querySelectorAll(":scope > span > span")].every(
          (part) => part.getAttribute("aria-hidden") === "true",
        ),
      ).toBe(true);
    }
  });

  it("shows a gap's rows from the keyboard and moves focus to the first one", async () => {
    renderRouted(<PullRequestStackCard stack={viewOf(LONG, 210)} />);
    const before = within(card()).getByRole("button", { name: /between #200 and #207/ });
    before.focus();
    await userEvent.keyboard("{Enter}");
    expect(within(card()).queryByRole("button", { name: /between #200 and #207/ })).toBeNull();
    expect(document.activeElement?.tagName).toBe("A");
    expect(document.activeElement?.textContent).toContain("#201");
    expect(shownSteps()).toEqual([...Array.from({ length: 14 }, (_, index) => index + 1), 40]);
    const after = within(card()).getByRole("button", { name: /between #213 and #239/ });
    after.focus();
    await userEvent.keyboard(" ");
    expect(document.activeElement?.textContent).toContain("#214");
    expect(shownSteps()).toHaveLength(40);
  });

  it("moves focus to a shown row that isn't a link, rather than losing it", async () => {
    const withBranch: Entry[] = LONG.map((entry, index) =>
      index === 1 ? [1, "stack/wip"] : entry,
    );
    renderRouted(<PullRequestStackCard stack={viewOf(withBranch, 210)} />);
    const gap = within(card()).getByRole("button", { name: /between #200 and #207/ });
    gap.focus();
    await userEvent.keyboard("{Enter}");
    const focused = document.activeElement as HTMLElement;
    expect(focused.tagName).toBe("DIV");
    expect(focused.closest("li")?.dataset.stackRowKey).toBe("branch:stack/wip");
    expect(focused.tabIndex).toBe(-1);
  });

  it("says whether the whole stack shows, and keeps focus on its toggle", async () => {
    renderRouted(<PullRequestStackCard stack={viewOf(LONG, 210)} />);
    const expand = within(card()).getByRole("button", { name: "Show the whole stack" });
    expect(expand).toHaveAttribute("aria-expanded", "false");
    expect(expand).toHaveTextContent("Expand all");
    await userEvent.click(expand);
    expect(shownSteps()).toHaveLength(40);
    const collapse = within(card()).getByRole("button", { name: "Fold the stack around this PR" });
    expect(collapse).toHaveAttribute("aria-expanded", "true");
    expect(collapse).toHaveTextContent("Collapse");
    await userEvent.click(collapse);
    expect(document.activeElement).toBe(collapse);
    expect(shownSteps()).toEqual(FOLDED_STEPS);
  });

  it("lists a straight stack flat by step, its gaps too, with no tree or level marks", () => {
    renderRouted(<PullRequestStackCard stack={viewOf(LONG, 210)} />);
    // Its steps give the order, as the menu lists it.
    expect(card().textContent).not.toMatch(/[├└│┆]/);
    expect(within(card()).queryByRole("img", { name: /^Level/ })).toBeNull();
    expect(within(card()).getAllByRole("button", { name: /^Show \d+ more/ })).toHaveLength(2);
  });

  it("keeps a forked stack's tree, and the lines its gaps carry", () => {
    // #200 forks: #239 sits beside the chain of #201–#238 instead of on top of it.
    const forked: Entry[] = [...LONG.slice(0, 39), [1, 239]];
    renderRouted(<PullRequestStackCard stack={viewOf(forked, 210)} />);
    expect(card().textContent).toMatch(/[├└]/);
    expect(within(card()).getAllByRole("img", { name: /^Level/ }).length).toBeGreaterThan(0);
    const gap = within(card()).getByRole("button", { name: /between #213 and #239/ });
    expect(gap.textContent).toMatch(/[│┆]/);
  });

  it("doesn't fold, or offer Expand all, for twelve PRs or fewer", () => {
    renderRouted(<PullRequestStackCard stack={viewOf(LONG.slice(0, 12), 205)} />);
    expect(shownSteps()).toHaveLength(12);
    expect(within(card()).queryByRole("button", { name: "Show the whole stack" })).toBeNull();
  });

  it("keeps what was opened as late reads replace the card for the same PR, then folds anew for another", async () => {
    const outer = (stack: PullRequestStackView, wrapped: boolean, selected = SELF_URL) => (
      <MemoryRouter>
        <PullRequestStackScope selectedUrl={selected}>
          {/* The description's card and GitHub's own sit in different trees, as on the page. */}
          {wrapped ? (
            <section>
              <PullRequestStackCard stack={stack} />
            </section>
          ) : (
            <PullRequestStackCard stack={stack} />
          )}
        </PullRequestStackScope>
      </MemoryRouter>
    );
    const { rerender } = render(outer(viewOf(LONG, 210), true));
    await userEvent.click(within(card()).getByRole("button", { name: /between #200 and #207/ }));
    const opened = [...Array.from({ length: 14 }, (_, index) => index + 1), 40];
    expect(shownSteps()).toEqual(opened);
    // Statuses arrive late.
    const members = LONG.map(([, number]) => member(number as number));
    rerender(outer(viewOf(LONG, 210, { read: loaded(...members) }), true));
    expect(shownSteps()).toEqual(opened);
    // A read still completing, then GitHub's own stack replacing the description's card.
    rerender(outer({ ...viewOf(LONG, 210), completing: true }, true));
    expect(shownSteps()).toEqual(opened);
    rerender(outer(nativeLong(), false));
    expect(shownSteps()).toEqual(opened);
    expect(card().querySelector("[aria-current=page]")?.textContent).toContain("#210");
    // Expand all holds across the same swaps.
    await userEvent.click(within(card()).getByRole("button", { name: "Show the whole stack" }));
    rerender(outer(viewOf(LONG, 210), true));
    expect(shownSteps()).toHaveLength(40);
    // Another PR shown starts folded around it.
    rerender(outer(viewOf(LONG, 220), true, prUrl(220)));
    expect(shownSteps()).toEqual([1, 18, 19, 20, 21, 22, 23, 24, 40]);
  });

  it("offers Go to first and last in stack in the menu, from the same commands as ⌘K", async () => {
    const selectInPlace = vi.fn(() => true);
    const stack = viewOf(LONG, 210, { selectInPlace });
    renderRouted(<PullRequestStackMenu stack={stack} active />);
    const palette = commands().filter((command) =>
      /^Go to (first|last) in stack/.test(command.label),
    );
    expect(palette.map((command) => command.label)).toEqual([
      "Go to first in stack (#200)",
      "Go to last in stack (#239)",
    ]);
    await userEvent.click(screen.getByRole("button", { name: /^Stack/ }));
    const menu = screen.getByRole("menu");
    await userEvent.click(
      within(menu).getByRole("menuitem", { name: /Go to last in stack \(#239\)/ }),
    );
    expect(selectInPlace).toHaveBeenLastCalledWith(prUrl(239));
    // ⌘K's command runs the same move.
    act(() => palette[0]!.run());
    expect(selectInPlace).toHaveBeenLastCalledWith(prUrl(200));
  });

  it("names why an end can't be opened, in the menu and ⌘K alike", async () => {
    const atTop: Entry[] = [...LONG.slice(0, 39), [39, "stack/next"]];
    renderRouted(<PullRequestStackMenu stack={viewOf(atTop, 200)} active />);
    const [first, last] = commands().filter((command) =>
      /^Go to (first|last) in stack/.test(command.label),
    );
    expect(first?.availability).toEqual({ available: false, reason: AT_STACK_BASE });
    expect(last?.availability).toEqual({ available: false, reason: "Not pushed yet" });
    await userEvent.click(screen.getByRole("button", { name: /^Stack/ }));
    const item = within(screen.getByRole("menu")).getByRole("menuitem", {
      name: /Go to first in stack/,
    });
    expect(item).toHaveAttribute("aria-disabled", "true");
    expect(item).toHaveAttribute("title", AT_STACK_BASE);
  });
});

describe("PullRequestStackPending", () => {
  it("holds the switcher's place as a status while the stack reads", () => {
    renderRouted(<PullRequestStackPending />);
    expect(screen.getByRole("status", { name: STACK_LOADING })).toBeTruthy();
  });
});

describe("reading a member ahead from its row", () => {
  /** A stack whose read-ahead records each interest that starts and ends. */
  function watchedStack(entries: Entry[] = BRANCHED, self = 101) {
    const events: string[] = [];
    const readAhead = vi.fn((url: string) => {
      events.push(`start ${Number(url.split("/").pop())}`);
      return () => events.push(`end ${Number(url.split("/").pop())}`);
    });
    return { stack: { ...viewOf(entries, self), readAhead }, events, readAhead };
  }
  const rest = (ms = 150) =>
    act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, ms);
      });
    });

  it("reads a member ahead while the pointer rests on its menu row, until it leaves", async () => {
    const { stack, events } = watchedStack();
    renderRouted(<PullRequestStackMenu stack={stack} active />);
    await userEvent.click(screen.getByRole("button", { name: "Stack: level 2 of 3" }));
    const row = screen.getByRole("menuitem", { name: /stack\/100/ });
    await userEvent.hover(row);
    await rest();
    expect(events).toEqual(["start 100"]);
    // As the pointer moves to the next row, it leaves this one.
    fireEvent.pointerLeave(row);
    await userEvent.hover(screen.getByRole("menuitem", { name: /stack\/103/ }));
    await rest();
    expect(events).toEqual(["start 100", "end 100", "start 103"]);
  });

  it("reads the member the keyboard moves to, as focus rests on its row", async () => {
    const { stack, events } = watchedStack();
    renderRouted(<PullRequestStackMenu stack={stack} active />);
    screen.getByRole("button", { name: "Stack: level 2 of 3" }).focus();
    await userEvent.keyboard("{Enter}");
    // The keyboard opens on this PR's own row, which has nothing to read ahead.
    await waitFor(() => expect(document.activeElement).toHaveAttribute("aria-current", "page"));
    await rest();
    expect(events).toEqual([]);
    await userEvent.keyboard("{ArrowUp}");
    await rest();
    expect(events).toEqual(["start 100"]);
    // Past this PR's own row again, to the member stacked on it.
    await userEvent.keyboard("{ArrowDown}{ArrowDown}");
    await rest();
    expect(events).toEqual(["start 100", "end 100", "start 103"]);
  });

  it("reads nothing for rows swept past, and lets go as the menu closes", async () => {
    const { stack, events } = watchedStack();
    renderRouted(<PullRequestStackMenu stack={stack} active />);
    await userEvent.click(screen.getByRole("button", { name: "Stack: level 2 of 3" }));
    const swept = screen.getByRole("menuitem", { name: /stack\/100/ });
    const rested = screen.getByRole("menuitem", { name: /stack\/103/ });
    // The sweep runs on a clock only the test moves, so a slow machine can't rest on a row.
    vi.useFakeTimers();
    try {
      fireEvent.pointerEnter(swept);
      act(() => vi.advanceTimersByTime(STACK_READ_AHEAD_INTENT_DELAY_MS - 1));
      fireEvent.pointerLeave(swept);
      fireEvent.pointerEnter(rested);
      act(() => vi.advanceTimersByTime(STACK_READ_AHEAD_INTENT_DELAY_MS));
    } finally {
      vi.useRealTimers();
    }
    expect(events).toEqual(["start 103"]);
    await userEvent.keyboard("{Escape}");
    await rest(0);
    expect(screen.queryByRole("menu")).toBeNull();
    expect(events).toEqual(["start 103", "end 103"]);
  });

  it("reads nothing for this PR's row, a branch not pushed yet or a PR elsewhere", async () => {
    const { stack, readAhead } = watchedStack(
      [
        [0, 100, EXTERNAL_LIB],
        [1, 101],
        [2, "stack/docs"],
      ],
      101,
    );
    renderRouted(<PullRequestStackMenu stack={stack} active />);
    await userEvent.click(screen.getByRole("button", { name: /^Stack: level 2 of/ }));
    await screen
      .getAllByRole("menuitem")
      .reduce<Promise<unknown>>(
        (done, item) => done.then(() => userEvent.hover(item)).then(() => rest()),
        Promise.resolve(),
      );
    expect(readAhead).not.toHaveBeenCalled();
  });

  it("reads a member ahead from the Overview's card, and still opens it on a click", async () => {
    const selected: string[] = [];
    const { stack, events } = watchedStack();
    const shown = {
      ...stack,
      selectInPlace: (url: string) => {
        selected.push(url);
        return true;
      },
    };
    renderRouted(<PullRequestStackCard stack={shown} />);
    const link = screen.getByRole("link", { name: /stack\/103/ });
    await userEvent.hover(link);
    await rest();
    expect(events).toEqual(["start 103"]);
    await userEvent.click(link);
    expect(selected).toEqual([prUrl(103)]);
    // The click left focus on the link, so its interest lasts until both leave.
    await userEvent.unhover(link);
    expect(events).toEqual(["start 103"]);
    act(() => link.blur());
    expect(events).toEqual(["start 103", "end 103"]);
  });
});

describe("a stack deeper than the tree indents", () => {
  // Forked at #200, so the menu draws its tree too; #205 and #206 sit past the deepest indent.
  const DEEP: Entry[] = [
    [0, 200],
    [1, 201],
    [2, 202],
    [3, 203],
    [4, 204],
    [5, 205],
    [6, 206],
    [1, 210],
  ];

  it("names each deep entry's level on the card, this PR's included", () => {
    renderRouted(<PullRequestStackCard stack={viewOf(DEEP, 206)} />);
    const rows = within(screen.getByRole("navigation", { name: "Stack" })).getAllByRole("listitem");
    expect(within(rows[5]).getByRole("img", { name: "Level 6" })).toHaveTextContent("6");
    expect(within(rows[6]).getByRole("img", { name: "Level 7" })).toHaveTextContent("7");
    expect(within(rows[4]).queryByRole("img", { name: /^Level/ })).toBeNull();
    expect(screen.queryByText(/↳ level/)).toBeNull();
  });

  it("names each deep entry's level in the menu, without a tab stop of its own", async () => {
    renderRouted(<PullRequestStackMenu stack={viewOf(DEEP, 206)} active />);
    await userEvent.click(screen.getByRole("button", { name: /^Stack: level 7/ }));
    const items = within(screen.getByRole("menu")).getAllByRole("menuitem");
    const deep = items.filter((item) => within(item).queryByRole("img", { name: /^Level/ }));
    expect(deep.map((item) => within(item).getByRole("img").getAttribute("aria-label"))).toEqual([
      "Level 6",
      "Level 7",
    ]);
    expect(deep.every((item) => item.querySelector("[tabindex='0']") === null)).toBe(true);
  });
});
