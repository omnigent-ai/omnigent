import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChatStore } from "@/store/chatStore";
import type { ElicitationBlock } from "@/lib/blocks";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Composer } from "./ChatPage";
import { SlashCommandMenu } from "@/components/SlashCommandMenu";

// These tests pin the slash-command suggestions menu UX in the composer:
// (1) the first match is highlighted as soon as the menu opens, so Tab/Enter
// complete it without arrowing down first, and (2) the highlighted row is
// scrolled into view as the user navigates. Both regressed because the menu
// previously opened with nothing pre-selected (menuIndex === -1), so Tab fell
// through to the browser's default focus move and Enter sent the message.

/** Minimal ComposerProps for an interactive (writable, idle) composer. */
function composerProps(overrides: Partial<Parameters<typeof Composer>[0]> = {}) {
  return {
    status: "idle" as const,
    isWorking: false,
    disabled: false,
    onSend: vi.fn(),
    onStop: vi.fn(),
    agents: undefined,
    agentsLoading: false,
    selectedAgentId: null,
    onSelectAgent: vi.fn(),
    permissionLevel: null,
    readOnlyReason: null,
    replyQuotes: [],
    onRemoveQuote: vi.fn(),
    onClearAllQuotes: vi.fn(),
    effortLevels: ["low", "medium", "high"] as const,
    showEffort: true,
    showModels: false,
    modelPickerKind: null,
    codexModelOptions: [],
    showCodexPlanMode: false,
    ...overrides,
  };
}

/** The composer textarea, located by its aria-label. */
function textarea() {
  return screen.getByLabelText("Message the agent") as HTMLTextAreaElement;
}

/** The currently highlighted menu row, or null when none is highlighted. */
function activeRow(): HTMLElement | null {
  return document.querySelector('[data-active="true"]');
}

function renderWithTooltips(ui: ReactElement) {
  return render(<TooltipProvider>{ui}</TooltipProvider>);
}

describe("Composer slash-command menu", () => {
  beforeEach(() => {
    // Two skills so the menu has skill rows distinct from the built-ins.
    // Skills fill the textarea (with a trailing space) on selection rather
    // than executing, which lets us assert the completed value directly
    // without invoking store actions like compact().
    useChatStore.setState({
      conversationId: "conv_test",
      skills: [
        { name: "deep-research", description: "Run a deep research sweep" },
        { name: "deslop", description: "Remove AI slop" },
      ],
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("highlights the first match as soon as the menu opens", () => {
    // isNativeWrapper so "/compact" is offered (#1139 hides it for non-native);
    // it is the first built-in, which is what this menu-UX test pins.
    render(<Composer {...composerProps({ isNativeWrapper: true })} />);
    fireEvent.change(textarea(), { target: { value: "/" } });
    // Built-ins are inserted first, so "/compact" tops the list and is the
    // default highlight — the crux of the fix (was -1 / nothing selected).
    expect(activeRow()?.textContent).toContain("/compact");
  });

  it("Tab completes the highlighted skill into the textarea", () => {
    render(<Composer {...composerProps()} />);
    const ta = textarea();
    // "/des" narrows to the "deslop" skill (built-ins don't match "des").
    fireEvent.change(ta, { target: { value: "/des" } });
    expect(activeRow()?.textContent).toContain("/deslop");

    fireEvent.keyDown(ta, { key: "Tab" });
    // Skills fill "/name " and keep focus so the user can append args.
    expect(ta.value).toBe("/deslop ");
  });

  it("Enter completes the highlighted command instead of sending", () => {
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/des" } });

    fireEvent.keyDown(ta, { key: "Enter" });
    expect(ta.value).toBe("/deslop ");
    expect(onSend).not.toHaveBeenCalled();
    // Completion fills "/deslop " (trailing space) which closes the menu —
    // no row stays highlighted.
    expect(activeRow()).toBeNull();
  });

  it("Enter sends a normal (non-slash) message", () => {
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "hello there" } });

    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("hello there", undefined);
  });

  it("does not send when Enter confirms active IME composition", () => {
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    const ta = textarea();
    fireEvent.compositionStart(ta);
    fireEvent.change(ta, { target: { value: "オムニジェント" } });

    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();

    fireEvent.compositionEnd(ta);
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("オムニジェント", undefined);
  });

  it("does not send when Enter carries the IME keyCode 229 fallback", () => {
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "omnigent" } });

    fireEvent.keyDown(ta, { key: "Enter", keyCode: 229 });
    expect(onSend).not.toHaveBeenCalled();

    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("omnigent", undefined);
  });

  it("ArrowDown moves the highlight to the next match", () => {
    // isNativeWrapper so "/compact" is offered (#1139 hides it for non-native).
    render(<Composer {...composerProps({ isNativeWrapper: true })} />);
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/" } });
    expect(activeRow()?.textContent).toContain("/compact");

    fireEvent.keyDown(ta, { key: "ArrowDown" });
    // Second built-in entry.
    expect(activeRow()?.textContent).toContain("/context");
  });
});

describe("Composer slash-command submit routing", () => {
  // Several tests below swap the store's setModel for a vi.fn(); restore
  // the real action after each test so the mock can't bleed into later
  // tests in this file (zustand state is module-global).
  const realSetModel = useChatStore.getState().setModel;

  beforeEach(() => {
    useChatStore.setState({
      conversationId: "conv_test",
      skills: [
        { name: "deep-research", description: "Run a deep research sweep" },
        { name: "deslop", description: "Remove AI slop" },
      ],
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    useChatStore.setState({ setModel: realSetModel });
  });

  it("routes a known skill through onSendSlashCommand with parsed args", () => {
    const onSend = vi.fn();
    const onSendSlashCommand = vi.fn();
    render(<Composer {...composerProps({ onSend, onSendSlashCommand })} />);
    const ta = textarea();
    // Trailing text after the name → menu is closed (has a space), so Enter
    // submits rather than completing the menu. Name is sent without the
    // leading slash; everything after the first token is the argument text.
    fireEvent.change(ta, { target: { value: "/deslop fix the bug" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(onSendSlashCommand).toHaveBeenCalledWith("deslop", "fix the bug");
    // It's a slash_command event, NOT a plaintext message.
    expect(onSend).not.toHaveBeenCalled();
  });

  it("routes a known skill whose args carry slashes (paths, URLs)", () => {
    const onSend = vi.fn();
    const onSendSlashCommand = vi.fn();
    render(<Composer {...composerProps({ onSend, onSendSlashCommand })} />);
    const ta = textarea();
    // The command guard checks only the "/deslop" token, so slashes in the
    // argument text (file paths, PR URLs) must not demote the send to
    // plaintext — the regression the review bot flagged on the landing
    // matcher applies here identically since both share isSlashCommandText.
    fireEvent.change(ta, { target: { value: "/deslop fix src/foo.ts" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(onSendSlashCommand).toHaveBeenCalledWith("deslop", "fix src/foo.ts");
    expect(onSend).not.toHaveBeenCalled();
  });

  it("treats a path-shaped first token as plaintext, not a command", () => {
    const onSend = vi.fn();
    const onSendSlashCommand = vi.fn();
    render(<Composer {...composerProps({ onSend, onSendSlashCommand })} />);
    const ta = textarea();
    // "/etc/hosts" has a "/" inside the first token — a file path. It must
    // fall through to the plaintext path, not error as an unknown command.
    fireEvent.change(ta, { target: { value: "/etc/hosts is broken" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(onSendSlashCommand).not.toHaveBeenCalled();
    expect(onSend).toHaveBeenCalledWith("/etc/hosts is broken", undefined);
  });

  it("sends empty arguments for a known skill with no args", () => {
    const onSend = vi.fn();
    const onSendSlashCommand = vi.fn();
    render(<Composer {...composerProps({ onSend, onSendSlashCommand })} />);
    const ta = textarea();
    // Trailing space closes the menu so Enter submits the bare command.
    fireEvent.change(ta, { target: { value: "/deslop " } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(onSendSlashCommand).toHaveBeenCalledWith("deslop", "");
    // Took the event path, not the plaintext fallback.
    expect(onSend).not.toHaveBeenCalled();
  });

  it("falls through to plaintext onSend for an unknown command", () => {
    const onSend = vi.fn();
    const onSendSlashCommand = vi.fn();
    render(<Composer {...composerProps({ onSend, onSendSlashCommand })} />);
    const ta = textarea();
    // No matching skill/builtin → not a slash_command; sent as a message.
    fireEvent.change(ta, { target: { value: "/not-a-real-skill" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(onSendSlashCommand).not.toHaveBeenCalled();
    expect(onSend).toHaveBeenCalledWith("/not-a-real-skill", undefined);
  });

  it("treats /effort as plaintext when effort controls are hidden", () => {
    const onSend = vi.fn();
    const onSendSlashCommand = vi.fn();
    render(<Composer {...composerProps({ onSend, onSendSlashCommand, showEffort: false })} />);
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/effort high" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(onSendSlashCommand).not.toHaveBeenCalled();
    expect(onSend).toHaveBeenCalledWith("/effort high", undefined);
  });

  it("native sessions (no onSendSlashCommand) send a known skill as plaintext", () => {
    // composerProps omits onSendSlashCommand — this models a native-terminal
    // session where the event path is disabled and the vendor TUI handles
    // the skill. The known skill must fall through to plaintext onSend.
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/deslop " } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(onSend).toHaveBeenCalledWith("/deslop", undefined);
  });

  it("routes /model to setModel on in-process sessions (matches REPL /model)", () => {
    // isTerminalFirst defaults to false → showModel true. The command must
    // write the override via setModel (NOT send the literal "/model …" text
    // to the agent) so the next turn runs on the new model. The visible
    // confirmation is the server-appended `[System: model changed…]`
    // transcript note, not inline composer text — so nothing to assert here
    // beyond the routing.
    const setModel = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ setModel });
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    const ta = textarea();
    // Space closes the menu so Enter submits; bare gateway id has no "/".
    fireEvent.change(ta, { target: { value: "/model databricks-gpt-5-4" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(setModel).toHaveBeenCalledWith("databricks-gpt-5-4");
    expect(onSend).not.toHaveBeenCalled();
  });

  it("clears the override for /model default|off|reset", () => {
    // The REPL clear aliases map to setModel(null) → server "default"
    // sentinel. A wrong value here (e.g. the literal "default" string)
    // would pin a bogus model instead of restoring the agent default.
    const setModel = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ setModel });
    render(<Composer {...composerProps()} />);
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/model default" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(setModel).toHaveBeenCalledWith(null);
  });

  it("treats /model as plaintext on native-wrapper sessions without a model picker", () => {
    // isNativeWrapper without showModels → showModel false: native wrappers
    // need an explicit picker-backed propagation path. Without one, /model
    // must NOT fire setModel — it falls through to a plaintext message.
    // Terminal-first SDK sessions (embedded Omnigent REPL terminal) keep the
    // in-process routing.
    const setModel = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ setModel });
    const onSend = vi.fn();
    render(
      <Composer {...composerProps({ onSend, isTerminalFirst: true, isNativeWrapper: true })} />,
    );
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/model databricks-gpt-5-4" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(setModel).not.toHaveBeenCalled();
    expect(onSend).toHaveBeenCalledWith("/model databricks-gpt-5-4", undefined);
  });

  it("opens the model picker for bare /model when the picker is available", () => {
    // claude-native (showModels): a plaintext "/model" would open Claude's
    // interactive selector inside the vendor TUI, which the web UI can't
    // render — the session just blocks. The composer must intercept the
    // bare command and open its own picker dropdown instead of sending.
    const onSend = vi.fn();
    render(
      <Composer
        {...composerProps({
          onSend,
          isTerminalFirst: true,
          isNativeWrapper: true,
          showModels: true,
          modelPickerKind: "claude",
        })}
      />,
    );
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/model " } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(onSend).not.toHaveBeenCalled();
    expect(ta.value).toBe("");
    // The AgentPicker dropdown is open with the Models rows to choose from.
    expect(screen.getAllByTestId("model-picker-item").length).toBeGreaterThan(0);
  });

  it("routes /model <name> to setModel on claude-native sessions", () => {
    // Sent as plaintext, "/model fable" would pop Claude's "Switch model?"
    // dialog inside the vendor TUI with nothing web-side to answer it —
    // the session just blocks. The command must take the picker's path
    // instead: setModel persists the override and the runner injects
    // "/model <name>" into the pane with auto-confirm.
    const setModel = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ setModel });
    const onSend = vi.fn();
    render(
      <Composer
        {...composerProps({
          onSend,
          isTerminalFirst: true,
          isNativeWrapper: true,
          showModels: true,
          modelPickerKind: "claude",
        })}
      />,
    );
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/model fable" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(setModel).toHaveBeenCalledWith("fable");
    expect(onSend).not.toHaveBeenCalled();
    // The picker only opens for the bare command, not the argument form.
    expect(screen.queryByTestId("model-picker-item")).toBeNull();
  });

  it("routes /model <name> to setModel on codex-native sessions", () => {
    // Codex-native propagates the persisted override via Codex app-server
    // `thread/settings/update`, so it follows the same picker-backed route
    // as claude-native instead of sending plaintext into the terminal.
    const setModel = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ setModel });
    const onSend = vi.fn();
    render(
      <Composer
        {...composerProps({
          onSend,
          isTerminalFirst: true,
          isNativeWrapper: true,
          showModels: true,
          modelPickerKind: "codex",
        })}
      />,
    );
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/model gpt-5.4" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(setModel).toHaveBeenCalledWith("gpt-5.4");
    expect(onSend).not.toHaveBeenCalled();
  });
});

describe("Composer effort slash-command visibility", () => {
  beforeEach(() => {
    useChatStore.setState({ conversationId: "conv_test", skills: [] });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("omits /effort from suggestions when effort controls are hidden", () => {
    // isNativeWrapper so "/compact" is offered (#1139 hides it for non-native);
    // the assertion below uses it as the surviving built-in control.
    render(<Composer {...composerProps({ showEffort: false, isNativeWrapper: true })} />);
    fireEvent.change(textarea(), { target: { value: "/" } });

    // Row testids — the active entry ("/compact") also renders in the
    // detail card, so a text query would double-match.
    expect(screen.queryByTestId("slash-menu-item-effort")).toBeNull();
    expect(screen.getByTestId("slash-menu-item-compact")).toBeInTheDocument();
  });

  it("shows /model in suggestions for in-process and picker-backed native sessions", () => {
    // Type just "/" (like the /effort case) so the highlight overlay shows
    // only "/" — keeps the menu row the sole "/model" match.
    // Default (isTerminalFirst false) → /model offered.
    const { unmount } = render(<Composer {...composerProps()} />);
    fireEvent.change(textarea(), { target: { value: "/" } });
    expect(screen.getByTestId("slash-menu-item-model")).toBeInTheDocument();
    unmount();

    // Terminal-first SDK session (embedded Omnigent REPL terminal, no
    // native wrapper) → still an in-process harness, /model stays offered.
    const { unmount: unmountSdk } = render(
      <Composer {...composerProps({ isTerminalFirst: true })} />,
    );
    fireEvent.change(textarea(), { target: { value: "/" } });
    expect(screen.getByText("/model")).toBeInTheDocument();
    unmountSdk();

    // Native wrapper without the model picker → /model suppressed.
    const { unmount: unmountNativeNoPicker } = render(
      <Composer {...composerProps({ isTerminalFirst: true, isNativeWrapper: true })} />,
    );
    fireEvent.change(textarea(), { target: { value: "/" } });
    expect(screen.queryByTestId("slash-menu-item-model")).toBeNull();
    unmountNativeNoPicker();

    // claude-native and codex-native (wrapper WITH the model picker) →
    // /model offered; it routes to setModel so the override propagates via
    // the runner.
    const { unmount: unmountClaude } = render(
      <Composer
        {...composerProps({
          isTerminalFirst: true,
          isNativeWrapper: true,
          showModels: true,
          modelPickerKind: "claude",
        })}
      />,
    );
    fireEvent.change(textarea(), { target: { value: "/" } });
    expect(screen.getByTestId("slash-menu-item-model")).toBeInTheDocument();
    unmountClaude();

    render(
      <Composer
        {...composerProps({
          isTerminalFirst: true,
          isNativeWrapper: true,
          showModels: true,
          modelPickerKind: "codex",
        })}
      />,
    );
    fireEvent.change(textarea(), { target: { value: "/" } });
    expect(screen.getByTestId("slash-menu-item-model")).toBeInTheDocument();
  });
});

describe("Composer Codex Plan-mode control", () => {
  const realSetCodexPlanMode = useChatStore.getState().setCodexPlanMode;

  beforeEach(() => {
    useChatStore.setState({
      conversationId: "conv_test",
      codexPlanMode: false,
      skills: [],
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    useChatStore.setState({ setCodexPlanMode: realSetCodexPlanMode, codexPlanMode: false });
  });

  it("toggles Codex Plan mode through the store action", async () => {
    const setCodexPlanMode = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ setCodexPlanMode });

    renderWithTooltips(<Composer {...composerProps({ showCodexPlanMode: true })} />);
    fireEvent.click(screen.getByTestId("codex-plan-mode-toggle"));

    await waitFor(() => expect(setCodexPlanMode).toHaveBeenCalledWith(true));
  });

  it("shows the active pressed state while Plan mode is enabled", () => {
    useChatStore.setState({ codexPlanMode: true });

    renderWithTooltips(<Composer {...composerProps({ showCodexPlanMode: true })} />);

    const button = screen.getByTestId("codex-plan-mode-toggle");
    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(button).toHaveAccessibleName("Exit Plan mode");
  });

  it("hides the control when the session is not Codex-native", () => {
    render(<Composer {...composerProps({ showCodexPlanMode: false })} />);
    expect(screen.queryByTestId("codex-plan-mode-toggle")).toBeNull();
  });
});

describe("SlashCommandMenu", () => {
  const COMMANDS = {
    "/alpha": "First",
    "/beta": "Second",
    "/gamma": "Third",
  };

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("marks the row at activeIndex as active", () => {
    render(<SlashCommandMenu query="" activeIndex={1} onSelect={vi.fn()} commands={COMMANDS} />);
    expect(activeRow()?.textContent).toContain("/beta");
  });

  it("scrolls the highlighted row into view when activeIndex changes", () => {
    const scrollSpy = vi.spyOn(Element.prototype, "scrollIntoView");
    const { rerender } = render(
      <SlashCommandMenu query="" activeIndex={0} onSelect={vi.fn()} commands={COMMANDS} />,
    );
    scrollSpy.mockClear();

    rerender(<SlashCommandMenu query="" activeIndex={2} onSelect={vi.fn()} commands={COMMANDS} />);
    // The effect keeps the keyboard selection visible as it scrolls past the
    // capped-height list; "nearest" avoids yanking the whole page.
    expect(scrollSpy).toHaveBeenCalledWith({ block: "nearest" });

    // The effect is keyed on activeIndex — a re-render that doesn't move the
    // selection must not re-scroll (otherwise unrelated re-renders would yank
    // the list around). Proves the [activeIndex] dependency, not "fires every
    // render".
    scrollSpy.mockClear();
    rerender(<SlashCommandMenu query="" activeIndex={2} onSelect={vi.fn()} commands={COMMANDS} />);
    expect(scrollSpy).not.toHaveBeenCalled();
  });

  it("filters rows by the typed query", () => {
    render(<SlashCommandMenu query="be" activeIndex={0} onSelect={vi.fn()} commands={COMMANDS} />);
    // Row testids (not text) — the active entry's name also appears in the
    // detail card beside the panel, so a text query would double-match.
    expect(screen.getByTestId("slash-menu-item-beta")).toBeDefined();
    expect(screen.queryByTestId("slash-menu-item-alpha")).toBeNull();
    expect(screen.queryByTestId("slash-menu-item-gamma")).toBeNull();
  });

  it("invokes onSelect with the command name when a row is clicked", () => {
    const onSelect = vi.fn();
    render(<SlashCommandMenu query="" activeIndex={0} onSelect={onSelect} commands={COMMANDS} />);
    fireEvent.click(screen.getByTestId("slash-menu-item-gamma"));
    expect(onSelect).toHaveBeenCalledWith("/gamma");
  });

  it("shows the highlighted entry's description in the detail card", () => {
    render(<SlashCommandMenu query="" activeIndex={1} onSelect={vi.fn()} commands={COMMANDS} />);
    // Descriptions moved off the rows into the Cursor-style detail card:
    // only the active entry's blurb renders, next to the panel. If the
    // card regressed (or showed the wrong entry), users would lose the
    // only place a skill's description is visible.
    const detail = screen.getByTestId("slash-menu-detail");
    expect(detail.textContent).toContain("/beta");
    expect(detail.textContent).toContain("Second");
    expect(detail.textContent).not.toContain("First");
  });
});

// Renders the real composer and inspects the highlight overlay's DOM, so a
// regression where the WHOLE draft tints (not just the token) is caught.
describe("Composer slash-command highlight overlay", () => {
  beforeEach(() => {
    useChatStore.setState({ conversationId: "conv_test", skills: [] });
  });
  afterEach(() => cleanup());

  /** The only tinted (pink) run in the overlay — should be just the token. */
  function tintedText(): string | null {
    return (
      screen.getByTestId("composer-highlight-overlay").querySelector(".text-brand-accent")
        ?.textContent ?? null
    );
  }

  /** The overlay's full text, tinted + untinted — should mirror the draft. */
  function overlayText(): string {
    return screen.getByTestId("composer-highlight-overlay").textContent ?? "";
  }

  // A slash command followed by args; only the leading token should tint.
  const COMMAND_PROMPT =
    "/cross-review have Claude Code implement GH issue #<number>, then have Codex review";

  it("tints only the token for a command with args (args stay default)", () => {
    render(<Composer {...composerProps()} />);
    fireEvent.change(textarea(), { target: { value: COMMAND_PROMPT } });
    expect(textarea().value).toBe(COMMAND_PROMPT);
    expect(tintedText()).toBe("/cross-review");
    expect(overlayText()).toBe(COMMAND_PROMPT);
  });

  it("renders no overlay for plain prose", () => {
    render(<Composer {...composerProps()} />);
    fireEvent.change(textarea(), { target: { value: "just a normal message" } });
    expect(screen.queryByTestId("composer-highlight-overlay")).toBeNull();
  });
});

describe("Composer placeholder", () => {
  afterEach(cleanup);

  it("shows the normal placeholder when the runner is live", () => {
    render(<Composer {...composerProps({})} />);
    expect(textarea().placeholder).toMatch(/ask the agent anything/i);
  });

  it("a structural read-only reason wins over the normal placeholder", () => {
    // readOnlyReason captures a session that can't take input at all, so it
    // must not be overridden by the default prompt.
    render(<Composer {...composerProps({ readOnlyReason: "Mirrored transcript" })} />);
    expect(textarea().placeholder).toBe("Mirrored transcript");
  });

  it("runner_asleep (reconnectHint): enabled composer nudges the user to send", () => {
    // Host online but runner offline — sending relaunches the runner, so the
    // composer stays writable and the placeholder is the affordance.
    render(<Composer {...composerProps({ reconnectHint: true })} />);
    expect(textarea().placeholder).toBe("Send a message to reconnect this session");
    expect(textarea().disabled).toBe(false);
  });

  it("streaming wins over the reconnect hint", () => {
    // A queued follow-up message takes precedence over the asleep nudge.
    render(<Composer {...composerProps({ reconnectHint: true, status: "streaming" })} />);
    expect(textarea().placeholder).toMatch(/send a follow-up/i);
  });

  it("unreachable (host offline / local-stranded): composer is blocked", () => {
    // A message can't wake it, so the textarea is disabled and the banner
    // below is the only affordance.
    render(<Composer {...composerProps({ unreachable: true })} />);
    expect(textarea().disabled).toBe(true);
    expect(textarea().placeholder).toMatch(/reconnect below/i);
  });

  it("unreachable wins over the reconnect hint (both set defensively)", () => {
    render(<Composer {...composerProps({ unreachable: true, reconnectHint: true })} />);
    expect(textarea().disabled).toBe(true);
    expect(textarea().placeholder).toMatch(/reconnect below/i);
  });
});

// A pending elicitation parks the agent's turn server-side on the verdict
// Future — a message posted then just sits queued and unread until the card
// is answered. These tests pin the composer lock that surfaces that state.
describe("Composer pending elicitation", () => {
  /**
   * A real ElicitationBlock (no mocks) matching the shape the BlockStream
   * reducer emits for `response.elicitation_request` — the same blocks the
   * composer's pending-elicitation selector scans.
   */
  function elicitationBlock(overrides: Partial<ElicitationBlock> = {}): ElicitationBlock {
    return {
      type: "elicitation",
      ctx: { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: "resp_1", itemId: null },
      elicitationId: "elic_1",
      targetSessionId: null,
      message: "Allow shell command?",
      phase: "tool_call",
      policyName: "ask-before-shell",
      contentPreview: "{}",
      requestedSchema: {},
      url: null,
      status: "pending",
      response: null,
      ...overrides,
    };
  }

  beforeEach(() => {
    useChatStore.setState({ conversationId: "conv_test", skills: [] });
  });

  afterEach(() => {
    // The other describes in this file never set `blocks` — clear it so a
    // leftover pending elicitation can't lock their composers.
    useChatStore.setState({ blocks: [] });
    cleanup();
    vi.restoreAllMocks();
  });

  it("locks the textarea and send button while an elicitation is pending", () => {
    useChatStore.setState({ blocks: [elicitationBlock()] });
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    const ta = textarea();

    // The lock is the disabled textarea + the placeholder explaining why.
    expect(ta.disabled).toBe(true);
    expect(ta.placeholder).toBe("Respond to the pending request above to continue");

    // Models a draft that existed before the elicitation arrived (drafts
    // persist per session): even with text present, Enter must not send —
    // this exercises the submit() guard, which backstops the disabled
    // attribute for programmatic paths.
    fireEvent.change(ta, { target: { value: "queued while blocked" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();

    // Send button stays off despite the draft — without the elicitation
    // gate, a non-empty draft would enable it.
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("keeps the interrupt button live while an elicitation is pending", () => {
    // Cancelling the turn is the other legitimate way out of a parked
    // elicitation — the lock must not take the stop control with it.
    // Fresh session id: the interrupt button only shows with no draft, and
    // the lock test above left a per-session draft behind for "conv_test".
    useChatStore.setState({ conversationId: "conv_interrupt", blocks: [elicitationBlock()] });
    render(<Composer {...composerProps({ isWorking: true, status: "streaming" })} />);
    expect(screen.getByRole("button", { name: "Interrupt" })).toBeEnabled();
  });

  it("unlocks once the elicitation is responded", () => {
    useChatStore.setState({
      blocks: [elicitationBlock({ status: "responded", response: { action: "accept" } })],
    });
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    const ta = textarea();

    expect(ta.disabled).toBe(false);
    fireEvent.change(ta, { target: { value: "carry on" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    // The verdict is in — the send path must be fully restored, not just
    // the visual disabled state.
    expect(onSend).toHaveBeenCalledWith("carry on", undefined);
  });

  it("ignores mirrored sub-agent elicitations addressed to a child session", () => {
    // A child's prompt mirrored into this chat doesn't park THIS session's
    // turn — inbox talk-back to the parent must keep working.
    useChatStore.setState({ blocks: [elicitationBlock({ targetSessionId: "conv_child" })] });
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    const ta = textarea();

    expect(ta.disabled).toBe(false);
    fireEvent.change(ta, { target: { value: "status update please" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("status update please", undefined);
  });
});

// Clicking the floating "Reply" button adds a quote chip above the composer.
// The caret must follow into the textarea so the user can type the reply
// immediately — without this, the quote appears but focus stays on the page
// and the user has to click the chat box first.
describe("Composer reply-quote focus", () => {
  beforeEach(() => {
    useChatStore.setState({ conversationId: "conv_test", skills: [] });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("focuses the textarea when a reply quote is added", () => {
    const { rerender } = render(<Composer {...composerProps({ replyQuotes: [] })} />);
    const ta = textarea();
    // The mount effect focuses on conversation bind; blur so the assertion
    // proves the quote-add effect re-focused, not the leftover mount focus.
    ta.blur();
    expect(document.activeElement).not.toBe(ta);

    rerender(<Composer {...composerProps({ replyQuotes: ["selected response text"] })} />);
    expect(document.activeElement).toBe(ta);
  });

  it("does not steal focus when a quote is removed", () => {
    // Removing a chip (the X button) shrinks the count — the effect only
    // fires when the count grows, so focus must stay put.
    const { rerender } = render(
      <Composer {...composerProps({ replyQuotes: ["first", "second"] })} />,
    );
    const ta = textarea();
    ta.blur();
    expect(document.activeElement).not.toBe(ta);

    rerender(<Composer {...composerProps({ replyQuotes: ["first"] })} />);
    expect(document.activeElement).not.toBe(ta);
  });
});

// The "Chatting with sub-agent …" tray peeks above the composer only when a
// sub-agent label is passed (the active session is a child). It must name the
// sub-agent so the composer reads as messaging the child, not the orchestrator.
describe("Composer sub-agent tray", () => {
  beforeEach(() => {
    useChatStore.setState({ conversationId: "conv_test", skills: [] });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  /** The sub-agent tray element, or null when not rendered. */
  function tray(): Element | null {
    return document.querySelector('[data-testid="composer-subagent-tray"]');
  }

  it("does not render the tray for a top-level session (no label)", () => {
    render(<Composer {...composerProps()} />);
    expect(tray()).toBeNull();
  });

  it("does not render the tray for an empty label", () => {
    // null is the top-level default; an empty string must also not peek a
    // nameless tray.
    render(<Composer {...composerProps({ subAgentLabel: "" })} />);
    expect(tray()).toBeNull();
  });

  it("renders the sub-agent name when a label is passed", () => {
    render(<Composer {...composerProps({ subAgentLabel: "check-account-eligibility" })} />);
    expect(tray()).not.toBeNull();
    // The name proves the passed label reaches the rendered tray, not just
    // that some tray exists.
    expect(screen.getByText("check-account-eligibility")).toBeTruthy();
    expect(screen.getByText(/Chatting with sub-agent/)).toBeTruthy();
  });
});
