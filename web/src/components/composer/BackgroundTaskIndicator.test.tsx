import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChatStore } from "@/store/chatStore";
import type { BackgroundTaskInfo } from "@/lib/types";

import { BackgroundTaskIndicator } from "./BackgroundTaskIndicator";

function setBackground(
  count: number,
  tasks: BackgroundTaskInfo[] = [],
  conversationId: string | null = "conv-1",
) {
  useChatStore.setState({
    backgroundTaskCount: count,
    backgroundTasks: tasks,
    conversationId,
  });
}

function badge(name: string | RegExp = /background tasks? still running/) {
  return screen.getByRole("button", { name });
}

// Radix attaches its outside-press listener and runs its close-time focus
// restore inside a setTimeout(0) — flush one macrotask so tests observe both.
const flushRadix = () =>
  act(async () => {
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
  });

beforeEach(() => {
  setBackground(0, [], null);
});

afterEach(() => {
  setBackground(0, [], null);
  cleanup();
});

describe("BackgroundTaskIndicator", () => {
  it("renders nothing for an explicit zero count, even with leftover task detail", () => {
    // WHY: the count is authoritative — zero means no badge.
    setBackground(0, [{ description: "stale detail" }]);
    const { container } = render(<BackgroundTaskIndicator />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the count on the badge with a singular accessible name", () => {
    setBackground(1, [{ description: "Only task" }]);
    render(<BackgroundTaskIndicator />);
    expect(badge("1 background task still running")).toHaveTextContent("1");
    expect(badge()).toHaveClass(
      "gap-1",
      "px-1",
      "md:px-2",
      "font-normal",
      "tabular-nums",
      "text-muted-foreground",
    );
    expect(badge().querySelector("svg")).toHaveAttribute("stroke-width", "1.5");
  });

  it("shows the count on the badge with a plural accessible name", () => {
    setBackground(2, [{ description: "One" }, { description: "Two" }]);
    render(<BackgroundTaskIndicator />);
    expect(badge("2 background tasks still running")).toHaveTextContent("2");
  });

  it("toggles the popover on click and lists tasks in reported order", () => {
    setBackground(2, [{ description: "First task" }, { description: "Second task" }]);
    render(<BackgroundTaskIndicator />);
    const trigger = badge("2 background tasks still running");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("list")).toBeNull();

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    const items = screen.getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("First task");
    expect(items[1]).toHaveTextContent("Second task");
    expect(screen.getAllByRole("status", { name: "Running" })).toHaveLength(2);

    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("list")).toBeNull();
  });

  it("closes on Escape and restores focus to the trigger", async () => {
    setBackground(1, [{ description: "One" }]);
    render(<BackgroundTaskIndicator />);
    const trigger = badge("1 background task still running");
    fireEvent.click(trigger);
    expect(screen.getByRole("list")).toBeInTheDocument();

    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.queryByRole("list")).toBeNull();
    await flushRadix();
    expect(document.activeElement).toBe(trigger);
  });

  it("does not open on hover", async () => {
    const user = userEvent.setup();
    setBackground(1, [{ description: "Watch PR checks" }]);
    render(<BackgroundTaskIndicator />);

    await user.hover(badge());

    expect(badge()).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("delivers an outside button click once and leaves focus on that button", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    setBackground(1, [{ description: "Watch PR checks" }]);
    render(
      <>
        <BackgroundTaskIndicator />
        <button type="button" onClick={onClick}>
          Other control
        </button>
      </>,
    );
    await user.click(badge());
    expect(screen.getByRole("dialog")).toBeVisible();

    const other = screen.getByRole("button", { name: "Other control" });
    await user.click(other);
    await flushRadix();

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(other).toHaveFocus();
  });

  it("closes on outside pointer interaction", async () => {
    setBackground(1, [{ description: "One" }]);
    render(<BackgroundTaskIndicator />);
    fireEvent.click(badge("1 background task still running"));
    expect(screen.getByRole("list")).toBeInTheDocument();

    await flushRadix(); // Radix attaches the outside-press listener a tick late
    fireEvent.pointerDown(document.body);
    fireEvent.click(document.body); // a full press: dismiss is deferred to click
    expect(screen.queryByRole("list")).toBeNull();
  });

  it("closes without stealing focus when the count drops to zero while open", async () => {
    setBackground(1, [{ description: "One" }]);
    render(<BackgroundTaskIndicator />);
    fireEvent.click(badge("1 background task still running"));
    expect(screen.getByRole("list")).toBeInTheDocument();

    act(() => setBackground(0, []));
    expect(screen.queryByRole("list")).toBeNull();
    expect(screen.queryByRole("button", { name: /background task/ })).toBeNull();
    // Nothing may force focus anywhere — the composer keeps what it had.
    await flushRadix();
    expect(document.activeElement).toBe(document.body);
  });

  it("resets the open state when the conversation changes", () => {
    setBackground(2, [{ description: "One" }, { description: "Two" }], "conv-1");
    render(<BackgroundTaskIndicator />);
    fireEvent.click(badge("2 background tasks still running"));
    expect(screen.getByRole("list")).toBeInTheDocument();

    act(() => setBackground(2, [{ description: "One" }, { description: "Two" }], "conv-2"));
    expect(screen.queryByRole("list")).toBeNull();
    expect(badge("2 background tasks still running")).toBeInTheDocument();
  });

  it("does not send focus to the new session's trigger when a switch closes the panel", async () => {
    // WHY: the switch-close goes through the store effect, not a user gesture;
    // with focus still inside the panel, Radix's default close-autofocus would
    // yank it to the NEW session's trigger (Sol P1). Focus must drop neutrally.
    setBackground(2, [{ description: "One" }, { description: "Two" }], "conv-old");
    render(<BackgroundTaskIndicator />);
    fireEvent.click(badge("2 background tasks still running"));
    const dialog = screen.getByRole("dialog");
    expect(dialog.contains(document.activeElement)).toBe(true);

    act(() => setBackground(1, [{ description: "New session task" }], "conv-new"));
    await flushRadix();

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(badge("1 background task still running")).not.toHaveFocus();
    expect(document.activeElement).toBe(document.body);
  });

  it("keeps external focus untouched when a session switch closes the panel", async () => {
    // WHY: a user who already moved on (composer, navigation) must not feel
    // the close at all — the guard only suppresses the trigger yank.
    setBackground(2, [{ description: "One" }, { description: "Two" }], "conv-old");
    render(
      <>
        <BackgroundTaskIndicator />
        <textarea data-testid="composer" />
      </>,
    );
    fireEvent.click(badge("2 background tasks still running"));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    screen.getByTestId("composer").focus();
    act(() => setBackground(1, [{ description: "New session task" }], "conv-new"));
    await flushRadix();

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByTestId("composer")).toHaveFocus();
    expect(badge("1 background task still running")).not.toHaveFocus();
  });

  it("restores focus normally after a session-switch close (no stale guard)", async () => {
    // WHY: the close-reason guard is consumed by the switch close and cleared
    // on the next open — an ordinary Escape afterwards must still restore the
    // trigger.
    setBackground(2, [{ description: "One" }, { description: "Two" }], "conv-old");
    render(<BackgroundTaskIndicator />);
    fireEvent.click(badge("2 background tasks still running"));
    act(() => setBackground(1, [{ description: "New" }], "conv-new"));
    await flushRadix();

    const newTrigger = badge("1 background task still running");
    fireEvent.click(newTrigger);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    const user = userEvent.setup();
    await user.keyboard("{Escape}");
    await flushRadix();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(newTrigger).toHaveFocus();
  });

  it("labels rows by description, then command, then a generic fallback", () => {
    setBackground(4, [
      { description: "Wait for CI", command: "gh run watch" },
      { command: "sleep 30" },
      { description: "   ", command: "npm run build" },
      {},
    ]);
    render(<BackgroundTaskIndicator />);
    fireEvent.click(badge("4 background tasks still running"));

    expect(screen.getByText("Wait for CI")).toBeInTheDocument();
    expect(screen.getByText("gh run watch")).toBeInTheDocument();
    // Command-only: the command is the label, not duplicated as a second line.
    expect(screen.getAllByText("sleep 30")).toHaveLength(1);
    // Blank description falls through to the command.
    expect(screen.getAllByText("npm run build")).toHaveLength(1);
    // Neither field: generic label.
    expect(screen.getByText("Background task")).toBeInTheDocument();
  });

  it("keeps the count authoritative when detail rows are partial", () => {
    setBackground(3, [{ description: "Only known task" }]);
    render(<BackgroundTaskIndicator />);
    fireEvent.click(badge("3 background tasks still running"));

    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByText(/2 more/)).toBeInTheDocument();
    expect(screen.getByText(/details unavailable/i)).toBeInTheDocument();
  });

  it("clamps detail rows to the authoritative count when the list overshoots", () => {
    // WHY: the count is authoritative in BOTH directions — a stale detail
    // list longer than the count must not render extra rows.
    setBackground(2, [{ description: "A" }, { description: "B" }, { description: "C" }]);
    render(<BackgroundTaskIndicator />);
    fireEvent.click(badge("2 background tasks still running"));

    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByText("A")).toBeInTheDocument();
    expect(screen.getByText("B")).toBeInTheDocument();
    expect(screen.queryByText("C")).toBeNull();
    expect(screen.queryByText(/details unavailable/i)).toBeNull();
  });

  it("opens a simple unavailable-details panel for count-only data", () => {
    setBackground(2, []);
    render(<BackgroundTaskIndicator />);
    fireEvent.click(badge("2 background tasks still running"));

    // The count stays visible in the panel, not just on the badge.
    expect(screen.getByText(/2 background tasks still running/)).toBeInTheDocument();
    expect(screen.getByText(/details unavailable/i)).toBeInTheDocument();
    expect(screen.queryByRole("list")).toBeNull();
  });

  it("shows the command once when it duplicates the label", () => {
    // WHY: only a DISTINCT command earns the muted second line.
    setBackground(1, [{ description: "npm run build", command: "npm run build" }]);
    render(<BackgroundTaskIndicator />);
    fireEvent.click(badge("1 background task still running"));
    expect(screen.getAllByText("npm run build")).toHaveLength(1);
  });

  it("exposes the panel as a labelled, non-modal dialog with a plain list", () => {
    // WHY: rows are informational, not actions — no menu roles.
    setBackground(2, [{ description: "One" }, { description: "Two" }]);
    render(<BackgroundTaskIndicator />);
    fireEvent.click(badge("2 background tasks still running"));

    const dialog = screen.getByRole("dialog", { name: "2 background tasks" });
    expect(dialog).not.toHaveAttribute("aria-modal", "true");
    expect(screen.getByRole("list")).toBeInTheDocument();
    expect(screen.queryByRole("menu")).toBeNull();
    expect(screen.queryByRole("menuitem")).toBeNull();
  });

  it("opens via native keyboard activation (Enter and Space) without submitting", async () => {
    // WHY: fireEvent.keyDown never triggers a button's native click; only
    // user-event reproduces Enter-keydown / Space-keyup activation, which is
    // what keyboard users in the composer actually get.
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    setBackground(2, [{ description: "One" }, { description: "Two" }]);
    render(
      <form
        onSubmit={(e) => {
          e.preventDefault();
          onSubmit();
        }}
      >
        <BackgroundTaskIndicator />
      </form>,
    );
    const trigger = badge("2 background tasks still running");

    await user.tab();
    expect(trigger).toHaveFocus();

    await user.keyboard("{Enter}");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    const dialog = screen.getByRole("dialog", { name: "2 background tasks" });
    // Radix moves focus into the panel on open; keyboard close returns it.
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(onSubmit).not.toHaveBeenCalled();

    await user.keyboard("{Escape}");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(trigger).toHaveFocus();
    expect(onSubmit).not.toHaveBeenCalled();

    await user.keyboard(" ");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("list")).toBeInTheDocument();
    expect(
      screen.getByRole("dialog", { name: "2 background tasks" }).contains(document.activeElement),
    ).toBe(true);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("announces count changes through a polite live region, like the pill it replaced", () => {
    // WHY: the old pill carried role="status", so a screen reader heard every
    // count change without focus leaving the composer. The band must preserve
    // that announcement — the tally only, never a re-listing of every task.
    setBackground(2, [{ description: "Wait for CI" }, { description: "Build check" }]);
    const { container } = render(<BackgroundTaskIndicator />);
    const region = container.querySelector<HTMLElement>('[role="status"], [aria-live="polite"]');
    if (region === null) {
      throw new Error(
        "no polite live region found: count changes are not announced to screen readers",
      );
    }
    expect(region).toHaveTextContent("2 background tasks still running");
    expect(region).not.toHaveTextContent("Wait for CI");

    act(() =>
      setBackground(3, [
        { description: "Wait for CI" },
        { description: "Build check" },
        { description: "Deploy" },
      ]),
    );
    expect(region).toHaveTextContent("3 background tasks still running");
    expect(region).not.toHaveTextContent("Deploy");

    act(() => setBackground(0, []));
    expect(container).toBeEmptyDOMElement();
  });

  it("wraps long labels and starts commands in a two-line preview", () => {
    const longDescription = `Watch the ${"very ".repeat(40)}long deploy`;
    const longCommand = `echo ${"y".repeat(300)}`;
    setBackground(1, [{ description: longDescription, command: longCommand }]);
    render(<BackgroundTaskIndicator />);
    fireEvent.click(badge("1 background task still running"));

    const label = screen.getByText(longDescription);
    expect(label).toHaveClass("[overflow-wrap:anywhere]");
    expect(label).not.toHaveClass("truncate");
    const command = screen.getByText(longCommand);
    expect(command).toHaveClass("line-clamp-2", "whitespace-pre-wrap", "[overflow-wrap:anywhere]");
    expect(command).not.toHaveClass("truncate");
  });

  it("uses a real type=button trigger so toggling never submits the composer form", () => {
    const onSubmit = vi.fn();
    setBackground(1, [{ description: "One" }]);
    render(
      <form
        onSubmit={(e) => {
          e.preventDefault();
          onSubmit();
        }}
      >
        <BackgroundTaskIndicator />
      </form>,
    );
    const trigger = badge("1 background task still running");
    expect(trigger).toHaveAttribute("type", "button");
    fireEvent.click(trigger);
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

describe("BackgroundTaskIndicator command previews", () => {
  const longCommand = [
    "while true; do",
    "  gh pr checks 123 --watch",
    "  gh pr view 123 --json reviews",
    "  sleep 30",
    "done",
  ].join("\n");
  let commandHeights: Map<string, number>;

  beforeEach(() => {
    commandHeights = new Map([[longCommand, 80]]);
    const naturalHeight = (element: Element) => {
      if (element.getAttribute("data-testid") !== "background-task-command") return 0;
      const text = element.textContent ?? "";
      return commandHeights.get(text) ?? text.split("\n").length * 16;
    };
    // jsdom has no layout; model a two-line box and its unclipped content.
    vi.spyOn(Element.prototype, "scrollHeight", "get").mockImplementation(function (this: Element) {
      return naturalHeight(this);
    });
    vi.spyOn(Element.prototype, "clientHeight", "get").mockImplementation(function (this: Element) {
      const height = naturalHeight(this);
      return this.classList.contains("line-clamp-2") ? Math.min(height, 32) : height;
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("supports icon-only expand/collapse by click, Enter, and Space without submitting or closing", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    setBackground(1, [{ description: "Watch PR checks", command: longCommand }]);
    render(
      <form
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit();
        }}
      >
        <BackgroundTaskIndicator />
      </form>,
    );
    await user.click(badge());

    const command = screen.getByTestId("background-task-command");
    const toggle = screen.getByRole("button", { name: "Expand command" });
    const chevron = toggle.querySelector("svg");
    expect(command.textContent).toBe(longCommand);
    expect(command).toHaveClass("line-clamp-2", "whitespace-pre-wrap");
    expect(command.id).not.toBe("");
    expect(toggle).toHaveAttribute("type", "button");
    expect(toggle).toHaveAttribute("aria-controls", command.id);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle.textContent).toBe("");
    expect(screen.queryByText(/^(Show|Hide) command$/)).toBeNull();
    expect(chevron).toHaveClass("lucide-chevron-right");
    expect(chevron).toHaveAttribute("aria-hidden", "true");
    expect(chevron).not.toHaveClass("rotate-90");

    await user.click(toggle);
    expect(toggle).toHaveAccessibleName("Collapse command");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle.textContent).toBe("");
    expect(chevron).toHaveClass("rotate-90");
    expect(command).not.toHaveClass("line-clamp-2");
    expect(command.textContent).toBe(longCommand);
    expect(screen.getByRole("dialog")).toBeVisible();

    await user.keyboard("{Enter}");
    expect(toggle).toHaveAccessibleName("Expand command");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(chevron).not.toHaveClass("rotate-90");
    expect(command).toHaveClass("line-clamp-2");

    await user.keyboard(" ");
    expect(toggle).toHaveAccessibleName("Collapse command");
    expect(chevron).toHaveClass("rotate-90");
    expect(command).not.toHaveClass("line-clamp-2");
    expect(screen.getByRole("dialog")).toBeVisible();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it.each(["sleep 30", "echo ready\necho done"])(
    "does not offer expansion when the command fits in two lines: %s",
    async (shortCommand) => {
      const user = userEvent.setup();
      setBackground(1, [{ description: "Short command", command: shortCommand }]);
      render(<BackgroundTaskIndicator />);
      await user.click(badge());

      expect(screen.getByTestId("background-task-command").textContent).toBe(shortCommand);
      expect(screen.queryByRole("button", { name: /^(Expand|Collapse) command$/ })).toBeNull();
    },
  );

  it("updates expansion availability when a resize changes how many lines fit", async () => {
    const user = userEvent.setup();
    const commandText = "gh pr checks 123 --watch";
    const OriginalResizeObserver = globalThis.ResizeObserver;
    let notifyCommandResize: (() => void) | undefined;
    vi.stubGlobal(
      "ResizeObserver",
      class extends OriginalResizeObserver {
        private readonly callback: ResizeObserverCallback;

        constructor(callback: ResizeObserverCallback) {
          super(callback);
          this.callback = callback;
        }

        override observe(target: Element, options?: ResizeObserverOptions) {
          super.observe(target, options);
          if (target.getAttribute("data-testid") === "background-task-command") {
            notifyCommandResize = () => this.callback([], this);
          }
        }
      },
    );
    setBackground(1, [{ description: "Watch PR checks", command: commandText }]);
    render(<BackgroundTaskIndicator />);
    await user.click(badge());
    expect(screen.queryByRole("button", { name: "Expand command" })).toBeNull();
    expect(notifyCommandResize).toBeDefined();

    commandHeights.set(commandText, 64);
    act(() => notifyCommandResize!());
    expect(screen.getByRole("button", { name: "Expand command" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );

    commandHeights.set(commandText, 16);
    act(() => notifyCommandResize!());
    expect(screen.queryByRole("button", { name: /^(Expand|Collapse) command$/ })).toBeNull();
  });

  it("starts collapsed when the popover is reopened", async () => {
    const user = userEvent.setup();
    setBackground(1, [{ id: "watch-pr", description: "Watch PR checks", command: longCommand }]);
    render(<BackgroundTaskIndicator />);
    await user.click(badge());
    await user.click(screen.getByRole("button", { name: "Expand command" }));
    expect(screen.getByRole("button", { name: "Collapse command" })).toBeVisible();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    await flushRadix();
    await user.click(badge());

    expect(screen.getByRole("button", { name: "Expand command" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.getByTestId("background-task-command")).toHaveClass("line-clamp-2");
  });

  it("does not carry an expanded command to another session", async () => {
    const user = userEvent.setup();
    const task = { id: "watch-pr", description: "Watch PR checks", command: longCommand };
    setBackground(1, [task], "session-a");
    render(<BackgroundTaskIndicator />);
    await user.click(badge());
    await user.click(screen.getByRole("button", { name: "Expand command" }));

    act(() => setBackground(1, [task], "session-b"));
    expect(screen.queryByRole("dialog")).toBeNull();
    await flushRadix();
    await user.click(badge());

    expect(screen.getByRole("button", { name: "Expand command" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.getByTestId("background-task-command")).toHaveClass("line-clamp-2");
  });

  it("collapses a replacement command for the same task without closing the panel", async () => {
    const user = userEvent.setup();
    const task = { id: "watch-pr", description: "Watch PR checks", command: longCommand };
    setBackground(1, [task]);
    render(<BackgroundTaskIndicator />);
    await user.click(badge());
    await user.click(screen.getByRole("button", { name: "Expand command" }));
    expect(screen.getByRole("button", { name: "Collapse command" })).toBeVisible();

    const replacement = "gh pr checks 456\ngh pr view 456 --json reviews\nsleep 60";
    act(() => setBackground(1, [{ ...task, command: replacement }]));

    expect(screen.getByRole("dialog")).toBeVisible();
    expect(screen.getByTestId("background-task-command").textContent).toBe(replacement);
    expect(screen.getByTestId("background-task-command")).toHaveClass("line-clamp-2");
    expect(screen.getByRole("button", { name: "Expand command" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByRole("button", { name: "Collapse command" })).toBeNull();
  });

  it("expands only the selected task and gives each command its own accessible target", async () => {
    const user = userEvent.setup();
    const secondCommand = "npm run lint\nnpm run type-check\nnpm run test\nnpm run build";
    setBackground(2, [
      { id: "watch-pr", description: "Watch PR checks", command: longCommand },
      { id: "check-build", description: "Check build", command: secondCommand },
    ]);
    render(<BackgroundTaskIndicator />);
    await user.click(badge());
    const commands = screen.getAllByTestId("background-task-command");
    const toggles = screen.getAllByRole("button", { name: "Expand command" });
    expect(commands).toHaveLength(2);
    expect(toggles).toHaveLength(2);
    expect(commands[0]!.id).not.toBe(commands[1]!.id);
    expect(toggles[0]).toHaveAttribute("aria-controls", commands[0]!.id);
    expect(toggles[1]).toHaveAttribute("aria-controls", commands[1]!.id);

    await user.click(toggles[0]!);
    expect(commands[0]).not.toHaveClass("line-clamp-2");
    expect(commands[1]).toHaveClass("line-clamp-2");
    expect(toggles[0]).toHaveAttribute("aria-expanded", "true");
    expect(toggles[1]).toHaveAttribute("aria-expanded", "false");
  });

  it.each([
    { label: "missing", description: undefined },
    { label: "blank", description: "   " },
    { label: "matching", description: longCommand },
  ])(
    "compacts a command with a $label description without duplicating it",
    async ({ description }) => {
      const user = userEvent.setup();
      setBackground(1, [{ description, command: longCommand }]);
      render(<BackgroundTaskIndicator />);
      await user.click(badge());

      const commands = screen.getAllByTestId("background-task-command");
      expect(commands).toHaveLength(1);
      expect(screen.getAllByText(longCommand, { normalizer: (text) => text })).toHaveLength(1);
      expect(commands[0]).toHaveClass("line-clamp-2");
      await user.click(screen.getByRole("button", { name: "Expand command" }));
      expect(commands[0]).not.toHaveClass("line-clamp-2");
      expect(screen.getAllByText(longCommand, { normalizer: (text) => text })).toHaveLength(1);
    },
  );
});
