import type * as UseWorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import type * as UseSessionModule from "@/hooks/useSession";
import type * as UseHostsModule from "@/hooks/useHosts";
import type * as RunnerHealthProviderModule from "@/hooks/RunnerHealthProvider";
import type * as AgentLabelsModule from "@/lib/agentLabels";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChatStore } from "@/store/chatStore";
import { clearSessionDrafts, hasSessionDraft } from "@/lib/sessionDrafts";
import { setOmnigentHostConfig } from "@/lib/host";

// Composer reads workspace files via a TanStack query hook (for "@"-file
// mentions). These slash-command tests don't exercise that, so stub the hook
// to avoid needing a QueryClientProvider around every bare render.
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => {
  const actual = await importOriginal<typeof UseWorkspaceChangedFilesModule>();
  return {
    ...actual,
    useWorkspaceAllFiles: () => ({ data: undefined }),
    useWorkspaceDirectory: () => ({ data: undefined }),
  };
});
// HostBadge now renders in the composer's status-line tray and reads the
// session's host binding via TanStack Query. Stub the hooks so it self-hides
// (no host bound) without needing a QueryClient provider around these renders.
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSessionModule>()),
  useSession: () => ({ session: { hostId: null }, isLoading: false, error: null }),
}));
vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof UseHostsModule>()),
  useHosts: () => ({ data: [] }),
}));
vi.mock("@/hooks/RunnerHealthProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof RunnerHealthProviderModule>()),
  useSessionHostOnline: () => undefined,
}));
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabelsModule>()),
  useBrainHarnessLabels: () => ({
    "claude-sdk": "Claude SDK",
    codex: "Codex",
    cursor: "Cursor",
    pi: "Pi",
    antigravity: "Antigravity",
    copilot: "Copilot",
  }),
}));
import type { ElicitationBlock } from "@/lib/blocks";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Composer, isSubagentRoutingEligible, shouldQueueSend } from "./ChatPage";
import type { Session } from "@/lib/types";
import type { QueuedMessage } from "@/store/chatStore";
import {
  BUILTIN_SLASH_COMMANDS,
  rankedSlashCommandNames,
  SlashCommandMenu,
  slashCommandMatches,
} from "@/components/SlashCommandMenu";

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
    selectedAgentId: null,
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

const CLAUDE_MODEL_OPTIONS = [
  { id: "fable", displayName: "Fable" },
  { id: "opus", displayName: "Opus" },
  { id: "sonnet", displayName: "Sonnet 4.6" },
  { id: "sonnet_5", displayName: "Sonnet 5" },
  { id: "haiku", displayName: "Haiku" },
];

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

describe("Composer session drafts", () => {
  beforeEach(() => {
    clearSessionDrafts();
    useChatStore.setState({ conversationId: "conv_draft" });
  });

  afterEach(() => {
    cleanup();
    clearSessionDrafts();
  });

  it("publishes unfinished text for the sidebar and clears it after send", async () => {
    render(<Composer {...composerProps()} />);

    fireEvent.change(textarea(), { target: { value: "unfinished message" } });
    await waitFor(() => expect(hasSessionDraft("conv_draft")).toBe(true));

    fireEvent.submit(textarea().closest("form")!);
    await waitFor(() => expect(hasSessionDraft("conv_draft")).toBe(false));
  });
});

describe("Composer growth layout", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("keeps multiline growth in layout instead of offsetting the form over the transcript", () => {
    render(<Composer {...composerProps()} />);
    const ta = textarea();
    const form = ta.closest("form");
    expect(form).not.toBeNull();

    const originalGetComputedStyle = window.getComputedStyle.bind(window);
    vi.spyOn(window, "getComputedStyle").mockImplementation((element, pseudoElt) => {
      if (element === ta) {
        return {
          lineHeight: "20px",
          paddingTop: "0px",
          paddingBottom: "0px",
          minHeight: "0px",
        } as CSSStyleDeclaration;
      }
      return originalGetComputedStyle(element, pseudoElt);
    });
    Object.defineProperty(ta, "scrollHeight", {
      configurable: true,
      get: () => 220,
    });

    fireEvent.change(ta, { target: { value: "one\ntwo\nthree\nfour" } });

    expect(ta.style.height).toBe("200px");
    expect(form?.style.marginTop).toBe("");
  });

  it("keeps long drafts scrollable without showing a native scrollbar", () => {
    render(<Composer {...composerProps()} />);

    const ta = textarea();
    expect(ta).toHaveClass(
      "overflow-y-auto",
      "[scrollbar-width:none]",
      "[&::-webkit-scrollbar]:hidden",
    );
    expect(ta.parentElement).toHaveClass("overflow-hidden");
  });
});

describe("Composer Claude goal control", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("sends the completion condition as a Claude /goal command", () => {
    const onSend = vi.fn();
    useChatStore.setState({ conversationId: "conv_polly" });
    renderWithTooltips(<Composer {...composerProps({ onSend, showClaudeGoalControl: true })} />);

    fireEvent.click(screen.getByTestId("goal-toggle"));
    fireEvent.change(screen.getByTestId("goal-condition"), {
      target: { value: "  Finish the implementation and pass tests  " },
    });
    fireEvent.click(screen.getByTestId("goal-start"));

    expect(onSend).toHaveBeenCalledWith("/goal Finish the implementation and pass tests");
  });
});

describe("Composer Codex goal control", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("sends the completion condition as a Codex /goal command", () => {
    const onSend = vi.fn();
    useChatStore.setState({ conversationId: "conv_polly" });
    renderWithTooltips(
      <Composer {...composerProps({ onSend, showPollyCodexGoalControl: true })} />,
    );

    fireEvent.click(screen.getByTestId("goal-toggle"));
    expect(screen.getByText(/Codex keeps working until this condition is met/)).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("goal-condition"), {
      target: { value: "  Finish the implementation and pass tests  " },
    });
    fireEvent.click(screen.getByTestId("goal-start"));

    expect(onSend).toHaveBeenCalledWith("/goal Finish the implementation and pass tests");
  });
});

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
    setOmnigentHostConfig({});
  });

  it("highlights the first match as soon as the menu opens", () => {
    // /compact is native-wrapper-only (#1139); render a native session so it
    // appears as the first built-in and is the default highlight.
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

  it("Tab completes a match found only mid-name (exercises menuMatches, not just the render filter)", () => {
    render(<Composer {...composerProps()} />);
    const ta = textarea();
    // "slop" is a substring of "deslop" but a prefix of no command. The menu
    // render filter would show the row either way; Tab-completion reads
    // menuMatches[menuIndex], so this only completes if the keyboard-nav
    // filter is substring-based. Guards menuMatches from silently reverting
    // to prefix matching and diverging from the rendered list.
    fireEvent.change(ta, { target: { value: "/slop" } });
    expect(activeRow()?.textContent).toContain("/deslop");
    fireEvent.keyDown(ta, { key: "Tab" });
    expect(ta.value).toBe("/deslop ");
  });

  it("ranks a prefix built-in ahead of mid-string matches so a short query can't execute the wrong command", () => {
    render(<Composer {...composerProps()} />);
    const ta = textarea();
    // "/e": /effort is a prefix match; /context and /help merely contain "e".
    // Before prefix-priority ranking, /context (a no-arg builtin) was
    // highlighted first and Tab/Enter executed it — a side-effecting
    // regression. /effort must win and Tab fills it (it takes an argument).
    fireEvent.change(ta, { target: { value: "/e" } });
    expect(activeRow()?.textContent).toContain("/effort");
    fireEvent.keyDown(ta, { key: "Tab" });
    expect(ta.value).toBe("/effort ");
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

  it("Enter sends a normal (non-slash) message and reports the send to analytics", () => {
    const onSend = vi.fn();
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    render(<Composer {...composerProps({ onSend })} />);
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "hello there" } });

    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("hello there", undefined);
    // Enter-key sends must emit the same telemetry as clicking Send.
    expect(analytics).toHaveBeenCalledWith({
      type: "click",
      componentId: "chat.composer.send",
      componentKind: "button",
    });
  });

  it("Enter on an empty composer neither sends nor reports a send", () => {
    const onSend = vi.fn();
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    render(<Composer {...composerProps({ onSend })} />);

    // Empty draft: the Send button is disabled, so a click can't fire the
    // event — the guarded Enter path must not fire it either.
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    expect(analytics).not.toHaveBeenCalled();
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
    // /compact is native-wrapper-only (#1139); render a native session so the
    // first built-in is "/compact" and ArrowDown advances to "/context".
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
    useChatStore.setState({ setModel: realSetModel, sessionHarness: null });
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

    expect(setModel).toHaveBeenCalledWith("databricks-gpt-5-4", {
      expectConfirmation: false,
    });
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

    expect(setModel).toHaveBeenCalledWith(null, { expectConfirmation: false });
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

  it("opens the config modal for bare /model when the picker is available", async () => {
    // claude-native (showModels): a plaintext "/model" would open Claude's
    // interactive selector inside the vendor TUI, which the web UI can't
    // render — the session just blocks. The composer must intercept the
    // bare command and open the config gear modal (which owns the Model
    // dropdown) instead of sending.
    const onSend = vi.fn();
    render(
      <Composer
        {...composerProps({
          onSend,
          isTerminalFirst: true,
          isNativeWrapper: true,
          showModels: true,
          modelPickerKind: "claude",
          codexModelOptions: CLAUDE_MODEL_OPTIONS,
        })}
      />,
    );
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/model " } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(onSend).not.toHaveBeenCalled();
    expect(ta.value).toBe("");
    // The config modal is open with the Model control to choose from.
    expect(await screen.findByTestId("composer-config-modal")).toBeTruthy();
    expect(screen.getByTestId("composer-config-model")).toBeTruthy();
  });

  it("does not open an empty Claude config modal while the live catalog loads", () => {
    const onSend = vi.fn();
    render(
      <Composer
        {...composerProps({
          onSend,
          isTerminalFirst: true,
          isNativeWrapper: true,
          showModels: true,
          modelPickerKind: "claude",
          codexModelOptions: [],
        })}
      />,
    );
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/model " } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(onSend).not.toHaveBeenCalled();
    // No catalog yet → fall through to the read-only hint, not an empty modal.
    expect(screen.queryByTestId("composer-config-modal")).toBeNull();
    expect(screen.getByText(/Usage: \/model <name>/)).toBeVisible();
  });

  it("opens the config modal for bare /model on opencode-native", async () => {
    const setModel = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({
      setModel,
      llmModel: "opencode-go/glm-5.2",
    });
    const onSend = vi.fn();
    render(
      <Composer
        {...composerProps({
          onSend,
          isTerminalFirst: true,
          isNativeWrapper: true,
          showModels: true,
          modelPickerKind: "opencode",
          codexModelOptions: [{ id: "opencode-go/glm-5.2", displayName: "opencode-go/glm-5.2" }],
        })}
      />,
    );
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/model " } });
    fireEvent.keyDown(ta, { key: "Enter" });

    // Bare /model opens the modal without sending text or changing the model.
    expect(onSend).not.toHaveBeenCalled();
    expect(setModel).not.toHaveBeenCalled();
    expect(await screen.findByTestId("composer-config-modal")).toBeTruthy();
    expect(screen.getByTestId("composer-config-model")).toBeTruthy();
  });

  it("routes /model <name> to setModel on opencode-native (functional switch)", () => {
    // Even with an empty picker list, "/model <name>" must persist the override
    // via setModel — the opencode executor reads model_override on the next
    // web-injected turn. It must NOT leak to the agent as plaintext "/model …".
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
          modelPickerKind: "opencode",
        })}
      />,
    );
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/model openrouter/llama-3.3-70b" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(setModel).toHaveBeenCalledWith("openrouter/llama-3.3-70b", {
      expectConfirmation: false,
    });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("routes /model <name> to setModel on claude-native sessions", () => {
    // Sent as plaintext, "/model fable" would pop Claude's "Switch model?"
    // dialog inside the vendor TUI with nothing web-side to answer it —
    // the session just blocks. The command must take the picker's path
    // instead: setModel persists the override and the runner injects
    // "/model <name>" into the pane with auto-confirm.
    const setModel = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ setModel, sessionHarness: "claude-native" });
    const onSend = vi.fn();
    render(
      <Composer
        {...composerProps({
          onSend,
          isTerminalFirst: true,
          isNativeWrapper: true,
          showModels: true,
          modelPickerKind: "claude",
          codexModelOptions: CLAUDE_MODEL_OPTIONS,
        })}
      />,
    );
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/model fable" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    // Reported-model session: the ask is marked pending until the
    // harness's own report (or the not-applied error) settles it.
    expect(setModel).toHaveBeenCalledWith("fable", { expectConfirmation: true });
    expect(onSend).not.toHaveBeenCalled();
    // The config modal only opens for the bare command, not the argument form.
    expect(screen.queryByTestId("composer-config-modal")).toBeNull();
  });

  it("routes /model <name> to setModel on codex-native sessions", () => {
    // Codex-native propagates the persisted override via Codex app-server
    // `thread/settings/update`, so it follows the same picker-backed route
    // as claude-native instead of sending plaintext into the terminal.
    const setModel = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ setModel, sessionHarness: "codex-native" });
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

    expect(setModel).toHaveBeenCalledWith("gpt-5.4", { expectConfirmation: true });
    expect(onSend).not.toHaveBeenCalled();
  });
});

describe("Composer model/effort label", () => {
  beforeEach(() => {
    useChatStore.setState({
      conversationId: "conv_test",
      skills: [],
      selectedModel: null,
      selectedEffort: null,
      llmModel: null,
      // Reset the per-session override too: a test that sets it must not leak
      // into the next, which now reads sessionModelOverride first for the label.
      sessionModelOverride: null,
      codexModelOptions: [],
      nativeVendorOwnsModel: false,
      // Identity-fallback inputs: reset so a case that sets one can't leak it
      // into the next (the label reads both when no model/effort resolves).
      sessionHarness: null,
      subAgentName: null,
    });
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  const label = () => screen.getByTestId("composer-model-effort-label");

  it("shows the model in the foreground and effort muted", () => {
    // The chip renders the harness's reported model (`llmModel`), never the
    // sticky preference or the request.
    useChatStore.setState({ llmModel: "opus", selectedEffort: "high" });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "claude" }],
          selectedAgentId: "a1",
          modelPickerKind: "claude",
          showModels: true,
          codexModelOptions: CLAUDE_MODEL_OPTIONS,
        })}
      />,
    );
    expect(label()).toHaveTextContent("Opus");
    expect(label()).toHaveTextContent("High");
    // The harness identity ("Claude") is NOT in the label — it lives in the gear tooltip.
    expect(label()).not.toHaveTextContent("Claude");
    // Model black, effort grey.
    expect(within(label()).getByText("Opus")).toHaveClass("text-foreground");
    expect(within(label()).getByText("High")).toHaveClass("text-muted-foreground");
  });

  it("reads 'Smart Routing' with no model/effort when routing is on", () => {
    // The router picks model + effort per turn, so the label must not surface a
    // stale pinned model/effort — it reads "Smart Routing" instead.
    useChatStore.setState({
      selectedModel: "opus",
      selectedEffort: "high",
      costControlModeOverride: "on",
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "claude" }],
          selectedAgentId: "a1",
          modelPickerKind: "claude",
          showModels: true,
          costRoutingEligible: true,
        })}
      />,
    );
    expect(label()).toHaveTextContent("Smart Routing");
    expect(label()).not.toHaveTextContent("Opus");
    expect(label()).not.toHaveTextContent("High");
  });

  it("renders the reported model, never the request or the sticky", () => {
    useChatStore.setState({
      selectedModel: "opus",
      sessionModelOverride: "sonnet",
      selectedEffort: null,
      llmModel: "haiku",
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "claude" }],
          selectedAgentId: "a1",
          modelPickerKind: "claude",
          showModels: true,
          showEffort: false,
          codexModelOptions: CLAUDE_MODEL_OPTIONS,
        })}
      />,
    );
    // The harness's report ("haiku") is the display authority: neither the
    // pending request ("sonnet") nor the cross-session sticky ("opus") may
    // render as if it were the session's model.
    expect(label()).toHaveTextContent("Haiku");
    expect(label()).not.toHaveTextContent("Opus");
    expect(label()).not.toHaveTextContent("Sonnet");
  });

  const CLAUDE_LIVE_OPTIONS = [
    { id: "opus", model: "system.ai.claude-opus-4-10", displayName: "Opus 4.10", isDefault: false },
    { id: "sonnet", model: "system.ai.claude-sonnet-5", displayName: "Sonnet 5", isDefault: true },
  ];

  it("maps a Claude concrete model to its friendly alias in the read-only label", () => {
    useChatStore.setState({
      selectedModel: null,
      sessionModelOverride: null,
      llmModel: "system.ai.claude-sonnet-5",
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "claude" }],
          selectedAgentId: "a1",
          modelPickerKind: "claude",
          showModels: true,
          showEffort: false,
          codexModelOptions: CLAUDE_LIVE_OPTIONS,
        })}
      />,
    );

    // The read-only label maps the concrete bound model to its friendly alias.
    expect(label()).toHaveTextContent("Sonnet 5");
    expect(label()).not.toHaveTextContent("system.ai.claude-sonnet-5");
    // The modal's catalog-default fallback (isDefault row when no concrete
    // model) is exercised in the real browser by the claude model-picker e2e
    // tests, where the Radix Select actually mounts its option rows.
  });

  it("hides the label when the model/effort is unresolved (nothing to show yet)", () => {
    // A claude-native session before the snapshot fills llmModel/selectedEffort
    // has no model label and no effort label. The read-only label renders
    // nothing rather than a placeholder — the gear still owns the config path.
    useChatStore.setState({ selectedModel: null, selectedEffort: null, llmModel: null });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "claude" }],
          selectedAgentId: "a1",
          modelPickerKind: "claude",
          showModels: true,
          showEffort: false,
          codexModelOptions: CLAUDE_MODEL_OPTIONS,
        })}
      />,
    );
    expect(screen.queryByTestId("composer-model-effort-label")).toBeNull();
    // The gear is still present so the user can open the config modal.
    expect(screen.getByTestId("composer-config-gear")).toBeTruthy();
  });

  it("falls back to the harness identity for an SDK/bundle agent with no model/effort", () => {
    // Polly (claude-sdk/pi bundle) surfaces no model or effort, so the label
    // would be empty. It falls back to the harness identity ("Polly (Pi)") so
    // the slot isn't blank.
    useChatStore.setState({
      selectedModel: null,
      selectedEffort: null,
      llmModel: null,
      sessionHarness: "pi",
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "polly" }],
          selectedAgentId: "a1",
          modelPickerKind: null,
          showModels: false,
          showEffort: false,
        })}
      />,
    );
    expect(label()).toHaveTextContent("Polly (Pi)");
  });

  it("names the vendor, not the Task subagent_type, on a Claude Code sub-agent", () => {
    // A claude-native sub-agent child has no model of its own, so the label
    // takes the identity fallback. Its `subAgentName` is Claude's own
    // `subagent_type` ("general-purpose") and it reuses the parent's
    // claude-native agent row — neither names the product, so the wrapper
    // label decides. The instance itself is named in the sub-agent tray.
    useChatStore.setState({
      selectedModel: null,
      selectedEffort: null,
      llmModel: null,
      sessionHarness: "claude-native",
      subAgentName: "general-purpose",
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "claude-native-ui" }],
          selectedAgentId: "a1",
          // No picker: the sub-agent is read-only, so it has no model control.
          modelPickerKind: null,
          showModels: false,
          showEffort: false,
          wrapperLabel: "claude-code-native-ui-subagent",
          readOnlyReason: "Claude Code sub-agents are read-only",
        })}
      />,
    );
    expect(label()).toHaveTextContent("Claude Code");
    expect(label()).not.toHaveTextContent("General-purpose");
  });

  it("does NOT fall back to the bare vendor name for a native wrapper with no model", () => {
    // A native wrapper's harnessLabel is the bare vendor name ("Claude"), which
    // the gear tooltip owns now — the label must stay empty when unresolved
    // rather than resurrecting it. Only SDK/bundle agents get the fallback.
    useChatStore.setState({ selectedModel: null, selectedEffort: null, llmModel: null });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "claude" }],
          selectedAgentId: "a1",
          modelPickerKind: "claude",
          showModels: true,
          showEffort: false,
          codexModelOptions: CLAUDE_MODEL_OPTIONS,
        })}
      />,
    );
    expect(screen.queryByTestId("composer-model-effort-label")).toBeNull();
  });

  it("surfaces a cursor-native session's model from the override, not the cross-session sticky", () => {
    // cursor-native is a vendor-owns-model wrapper, so `nativeVendorOwnsModel`
    // is true and the bound `llmModel` is a meaningless default. Its live model
    // is mirrored into the session override (`sessionModelOverride`), NOT the
    // cross-session sticky `selectedModel`. The label must read the real
    // session model ("Composer 2.5"), not the stale sticky pick carried over
    // from another session.
    useChatStore.setState({
      nativeVendorOwnsModel: true,
      selectedModel: "opus-4.5", // stale cross-session sticky — must be ignored
      sessionModelOverride: "composer-2.5",
      selectedEffort: "low",
      llmModel: "fable", // meaningless vendor default — must not surface
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "cursor" }],
          selectedAgentId: "a1",
          modelPickerKind: "cursor",
          showModels: true,
          showEffort: false, // cursor effort control is dropped for now
          codexModelOptions: [
            { id: "composer-2.5", displayName: "Composer 2.5" },
            { id: "opus-4.5", displayName: "Opus 4.5" },
          ],
        })}
      />,
    );
    expect(label()).toHaveTextContent("Composer 2.5");
    // Neither the stale sticky, the meaningless vendor default, nor any effort leaks in.
    expect(label()).not.toHaveTextContent("Opus 4.5");
    expect(label()).not.toHaveTextContent("fable");
    expect(label()).not.toHaveTextContent("Low");
    expect(within(label()).getByText("Composer 2.5")).toHaveClass("text-foreground");
  });

  it("surfaces an SDK/bundle session's model from the override, not the cross-session sticky", () => {
    // Polly/Debby (claude-sdk) repro: a model picked in some other (Codex)
    // session lingers in the global sticky `selectedModel`. SDK/bundle sessions
    // (modelPickerKind === null) never have the sticky applied, so the label
    // must read the session's own applied model (`sessionModelOverride`), never
    // the stale sticky — the "gpt-5.5 on a Claude-SDK Polly" report.
    useChatStore.setState({
      selectedModel: "gpt-5.5", // stale cross-session sticky — must be ignored
      sessionModelOverride: "claude-opus-4-8",
      selectedEffort: null,
      llmModel: null,
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "polly" }],
          selectedAgentId: "a1",
          modelPickerKind: null,
        })}
      />,
    );
    expect(label()).toHaveTextContent("claude-opus-4-8");
    expect(label()).not.toHaveTextContent("gpt-5.5");
  });

  it("does not leak the cross-session sticky model on an SDK/bundle session with no applied model", () => {
    // The exact report: a Polly (claude-sdk) session with no override and no
    // bound model, but a `gpt-5.5` left in the sticky from a prior Codex
    // session. The model label stays empty — only the real effort shows.
    useChatStore.setState({
      selectedModel: "gpt-5.5", // stale cross-session sticky — must not surface
      sessionModelOverride: null,
      selectedEffort: "high",
      llmModel: null,
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "polly" }],
          selectedAgentId: "a1",
          modelPickerKind: null,
        })}
      />,
    );
    expect(label()).not.toHaveTextContent("gpt-5.5");
    // The real effort still renders — proving the label is present and only
    // the leaked model was suppressed.
    expect(label()).toHaveTextContent("High");
  });

  it("does not leak the cross-session sticky model on a native session before its catalog lands", () => {
    // The Codex→Claude switch repro: `switchTo` clears the session-scoped model
    // fields but keeps the sticky, so mid-switch a claude-native session has an
    // empty catalog and the `gpt-5.5` the outgoing Codex session left behind.
    // The label must wait for a model this session vouches for rather than
    // paint the previous session's pick for the whole bind round trip.
    useChatStore.setState({
      selectedModel: "gpt-5.5", // outgoing Codex session's pick — must not surface
      sessionModelOverride: null,
      selectedEffort: "high",
      llmModel: null,
      codexModelOptions: [], // cleared by `switchTo`, refilled when the snapshot lands
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "claude" }],
          selectedAgentId: "a1",
          modelPickerKind: "claude",
          showModels: true,
          codexModelOptions: [],
        })}
      />,
    );
    expect(label()).not.toHaveTextContent("gpt-5.5");
    // The real effort still renders — only the leaked model was suppressed.
    expect(label()).toHaveTextContent("High");
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
    // /compact is native-wrapper-only (#1139); render a native session so it
    // stays present as the control row used to anchor this assertion.
    render(<Composer {...composerProps({ showEffort: false, isNativeWrapper: true })} />);
    fireEvent.change(textarea(), { target: { value: "/" } });

    // Row testids — /compact is hidden for non-native-wrapper sessions,
    // so verify /context is present instead.
    expect(screen.queryByTestId("slash-menu-item-effort")).toBeNull();
    expect(screen.getByTestId("slash-menu-item-context")).toBeInTheDocument();
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
          codexModelOptions: CLAUDE_MODEL_OPTIONS,
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

describe("Composer claude-native permission mode", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    useChatStore.setState({ claudePermissionMode: "" });
  });

  it("shows the Permissions row inside the gear modal", async () => {
    useChatStore.setState({ conversationId: "conv_test", claudePermissionMode: "auto" });

    renderWithTooltips(<Composer {...composerProps({ showClaudePermissionMode: true })} />);
    fireEvent.click(screen.getByTestId("composer-config-gear"));

    expect(await screen.findByTestId("composer-config-modal")).toBeTruthy();
    const row = screen.getByTestId("composer-config-permission-mode");
    // Trigger shows the label only — the description belongs to the open list.
    expect(row).toHaveTextContent("Auto");
    expect(row).not.toHaveTextContent("classifier");
  });

  it("omits the Permissions row when the mode could not be determined", async () => {
    // Claude only renders its mode footer in some pane states (a todo list
    // displaces it), so an unknown mode must not be shown as a guess.
    useChatStore.setState({ conversationId: "conv_test", claudePermissionMode: "" });

    renderWithTooltips(<Composer {...composerProps({ showClaudePermissionMode: true })} />);
    fireEvent.click(screen.getByTestId("composer-config-gear"));

    expect(await screen.findByTestId("composer-config-modal")).toBeTruthy();
    expect(screen.queryByTestId("composer-config-permission-mode")).toBeNull();
  });

  it("keeps the gear reachable when the mode is the only config row", () => {
    // A Claude session with no model/effort/routing knobs must still open the
    // gear, since the permission mode lives behind it.
    useChatStore.setState({ conversationId: "conv_test", claudePermissionMode: "auto" });

    renderWithTooltips(
      <Composer
        {...composerProps({
          showClaudePermissionMode: true,
          showModels: false,
          showEffort: false,
          costRoutingEligible: false,
        })}
      />,
    );

    expect(screen.getByTestId("composer-config-gear")).toBeInTheDocument();
  });
});

describe("slashCommandMatches", () => {
  it("matches the leaf segment after a namespace prefix", () => {
    expect(slashCommandMatches("/superpowers:using-superpowers", "using-superpowers")).toBe(true);
  });

  it("matches a substring in the middle of the name", () => {
    expect(slashCommandMatches("/cross-review", "rev")).toBe(true);
  });

  it("does not match a word that only appears in the description", () => {
    // Matching is name-only — the web menu never shows descriptions inline,
    // so a description-driven hit would look unexplained. "window" is in this
    // command's blurb but not its name, so it must NOT match.
    expect(slashCommandMatches("/context", "window")).toBe(false);
  });

  it("is case-insensitive on both name and query", () => {
    expect(slashCommandMatches("/Superpowers:Using", "USING")).toBe(true);
  });

  it("returns false when the query is nowhere in the name", () => {
    expect(slashCommandMatches("/context", "zzz")).toBe(false);
  });
});

describe("rankedSlashCommandNames", () => {
  it("ranks a prefix match ahead of commands that merely contain the query", () => {
    // "/e": /effort is a prefix; /context, /model, /help only contain "e".
    // Prefix-priority keeps /effort first so its auto-highlight + Enter can't
    // execute an unrelated no-arg builtin (/context) as a side effect.
    expect(rankedSlashCommandNames(BUILTIN_SLASH_COMMANDS, "e")[0]).toBe("/effort");
  });

  it("ranks /model ahead of commands that merely contain 'm'", () => {
    // "/m": /model is a prefix; /compact contains "m". Was /compact first.
    expect(rankedSlashCommandNames(BUILTIN_SLASH_COMMANDS, "m")[0]).toBe("/model");
  });

  it("keeps built-ins ahead of skills so the Commands section stays on top", () => {
    const commands = { ...BUILTIN_SLASH_COMMANDS, "/superpowers:effort-helper": "x" };
    const ranked = rankedSlashCommandNames(commands, "effort");
    // Both /effort (builtin, prefix) and the skill (mid-string) match; the
    // builtin must rank first so the render partition stays contiguous.
    expect(ranked[0]).toBe("/effort");
    expect(ranked.indexOf("/effort")).toBeLessThan(ranked.indexOf("/superpowers:effort-helper"));
  });

  it("ranks a prefix skill ahead of a mid-string skill, stably", () => {
    // Insertion order is deep-research, research; ranking promotes the prefix
    // match (research) above the mid-string one (deep-research contains "res").
    const commands = { "/deep-research": "a", "/research": "b" };
    expect(rankedSlashCommandNames(commands, "res")).toEqual(["/research", "/deep-research"]);
  });

  it("returns everything in insertion order for an empty query (lone '/')", () => {
    expect(rankedSlashCommandNames(BUILTIN_SLASH_COMMANDS, "")).toEqual(
      Object.keys(BUILTIN_SLASH_COMMANDS),
    );
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

  it("surfaces a namespaced skill by its leaf name", () => {
    render(
      <SlashCommandMenu
        query="using-superpowers"
        activeIndex={0}
        onSelect={vi.fn()}
        commands={{ "/superpowers:using-superpowers": "Establishes how to find and use skills" }}
      />,
    );
    expect(screen.getByTestId("slash-menu-item-superpowers:using-superpowers")).toBeDefined();
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

    rerender(
      <Composer
        {...composerProps({
          replyQuotes: [{ id: "quote-1", text: "selected response text" }],
        })}
      />,
    );
    expect(document.activeElement).toBe(ta);
  });

  it("does not steal focus when a quote is removed", () => {
    // Removing a chip (the X button) shrinks the count — the effect only
    // fires when the count grows, so focus must stay put.
    const { rerender } = render(
      <Composer
        {...composerProps({
          replyQuotes: [
            { id: "quote-1", text: "first" },
            { id: "quote-2", text: "second" },
          ],
        })}
      />,
    );
    const ta = textarea();
    ta.blur();
    expect(document.activeElement).not.toBe(ta);

    rerender(<Composer {...composerProps({ replyQuotes: [{ id: "quote-1", text: "first" }] })} />);
    expect(document.activeElement).not.toBe(ta);
  });
});

// Attaching a file via the paperclip button routes through the hidden file
// <input>, whose click (and the OS file dialog) pulls focus off the composer.
// The change handler must hand focus back so the user can keep typing the
// message that goes with the attachment — without this the caret is lost and
// the next keystroke does nothing until the chat box is clicked again.
describe("Composer file-attachment focus", () => {
  beforeEach(() => {
    useChatStore.setState({ conversationId: "conv_test", skills: [] });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  /** The hidden attachment file <input> (the paperclip button proxies to it). */
  function fileInput(): HTMLInputElement {
    const el = document.querySelector('input[type="file"]') as HTMLInputElement | null;
    if (!el) throw new Error("file input not found");
    return el;
  }

  it("focuses the textarea after a file is attached", () => {
    render(<Composer {...composerProps()} />);
    const ta = textarea();
    // The mount effect focuses on conversation bind; blur so the assertion
    // proves the attach handler re-focused, not the leftover mount focus.
    ta.blur();
    expect(document.activeElement).not.toBe(ta);

    const file = new File([new Uint8Array(10)], "shot.png", { type: "image/png" });
    fireEvent.change(fileInput(), { target: { files: [file] } });

    expect(document.activeElement).toBe(ta);
  });

  it("does not focus the textarea when the attachment is rejected", () => {
    // An unsupported type is dropped by validateAttachments, so no file is
    // added — and with nothing attached there's no reason to yank focus back.
    render(<Composer {...composerProps()} />);
    const ta = textarea();
    ta.blur();
    expect(document.activeElement).not.toBe(ta);

    const bad = new File([new Uint8Array(10)], "clip.mp4", { type: "video/mp4" });
    fireEvent.change(fileInput(), { target: { files: [bad] } });

    expect(document.activeElement).not.toBe(ta);
  });

  it("clears the rejection notice once the user types", () => {
    // The rejected file is never attached, so there is no chip to remove and
    // nothing else clears the notice. Left sticky it reads as a blocker on a
    // composer that can actually be submitted.
    render(<Composer {...composerProps()} />);
    const bad = new File([new Uint8Array(10)], "clip.mp4", { type: "video/mp4" });
    fireEvent.change(fileInput(), { target: { files: [bad] } });
    expect(screen.getByText(/can't be attached/)).toBeTruthy();

    fireEvent.change(textarea(), { target: { value: "never mind, just a question" } });

    expect(screen.queryByText(/can't be attached/)).toBeNull();
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

describe("Composer — queued-message flush gating", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    useChatStore.setState({ queuedMessages: [] });
  });

  // Regression (Polly review 3a): the level-triggered flush effect must NOT
  // drain the queue while the session is unreachable — flushing would POST
  // into a void, bypassing onSend's reconnect dialog. It must drain once
  // reachable again.
  it("holds the queue while unreachable, then flushes when reachable", async () => {
    const sendSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({
      conversationId: "conv_test",
      boundAgentId: "agent_xyz",
      status: "idle",
      sessionStatus: "idle",
      send: sendSpy,
      queuedMessages: [{ queueId: "q_1", text: "held", conversationId: "conv_test" }],
    });

    // Idle + a waiting head, but unreachable → the effect must not flush.
    // Wrapped in TooltipProvider since the queued-message strip renders the
    // steer button's tooltip.
    const { rerender } = renderWithTooltips(<Composer {...composerProps({ unreachable: true })} />);
    await waitFor(() => expect(sendSpy).not.toHaveBeenCalled());
    expect(useChatStore.getState().queuedMessages).toHaveLength(1);

    // Becomes reachable → the effect re-fires and drains the head.
    rerender(
      <TooltipProvider>
        <Composer {...composerProps({ unreachable: false })} />
      </TooltipProvider>,
    );
    await waitFor(() => expect(sendSpy).toHaveBeenCalledTimes(1));
    expect(sendSpy.mock.calls[0]!.slice(0, 2)).toEqual(["held", "agent_xyz"]);
    expect(useChatStore.getState().queuedMessages).toHaveLength(0);
  });
});

describe("Composer config gear", () => {
  beforeEach(() => {
    useChatStore.setState({
      conversationId: "conv_test",
      skills: [],
      selectedModel: null,
      sessionModelOverride: null,
      llmModel: null,
      nativeVendorOwnsModel: false,
      selectedEffort: null,
      costControlModeOverride: null,
      // Opening the gear re-reads the routing switches; stub the fetch away.
      refreshSessionOverrides: vi.fn().mockResolvedValue(undefined),
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  const gear = () => document.querySelector('[data-testid="composer-config-gear"]');

  it("renders when the session has a switchable knob (effort)", () => {
    renderWithTooltips(<Composer {...composerProps({ showEffort: true })} />);
    expect(gear()).not.toBeNull();
  });

  it("does not render when there is nothing to configure", () => {
    // No models, no effort, not routable → nothing to configure.
    renderWithTooltips(
      <Composer
        {...composerProps({ showEffort: false, showModels: false, costRoutingEligible: false })}
      />,
    );
    expect(gear()).toBeNull();
  });

  it("soft-disables the gear on a read-only session (aria-disabled, click no-ops)", () => {
    renderWithTooltips(<Composer {...composerProps({ showEffort: true, permissionLevel: 1 })} />);
    expect(gear()).toHaveAttribute("aria-disabled", "true");
    // Soft-disable, not native `disabled`: the click is guarded but the button
    // stays hover-able so its config tooltip still shows.
    expect(gear()).toHaveProperty("disabled", false);
    fireEvent.click(gear()!);
    expect(screen.queryByTestId("composer-config-modal")).toBeNull();
  });

  it("soft-disables the gear when the session is unreachable (host offline / stranded)", () => {
    // No message can wake an unreachable session, so a config change has
    // nothing to apply to — the gear is inert like the composer.
    renderWithTooltips(<Composer {...composerProps({ showEffort: true, unreachable: true })} />);
    expect(gear()).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(gear()!);
    expect(screen.queryByTestId("composer-config-modal")).toBeNull();
  });

  it("keeps the gear live on an asleep session (change persists and applies on wake)", () => {
    // Asleep/starting/unknown sessions accept sends (which wake the runner),
    // and a config PATCH persists server-side and applies on the next
    // wake/turn — so the gear stays live wherever the composer does.
    renderWithTooltips(<Composer {...composerProps({ showEffort: true })} />);
    expect(gear()).toHaveAttribute("aria-disabled", "false");
    fireEvent.click(gear()!);
    expect(screen.queryByTestId("composer-config-modal")).not.toBeNull();
  });

  it("still shows the config tooltip on a disabled gear (soft-disable preserves hover)", async () => {
    useChatStore.setState({ selectedEffort: "high", sessionHarness: "claude-sdk" });
    renderWithTooltips(
      <Composer
        {...composerProps({
          showEffort: true,
          showModels: true,
          modelPickerKind: "claude",
          unreachable: true,
        })}
      />,
    );
    // Even soft-disabled, the read-only summary must remain visible on hover.
    fireEvent.focus(gear()!);
    const tip = await screen.findByTestId("composer-config-gear-tooltip");
    expect(tip.textContent).toContain("Model:");
  });

  it("shows a hover summary with Harness/Model/Effort rows and no Permissions row", async () => {
    useChatStore.setState({ selectedEffort: "high", sessionHarness: "claude-sdk" });
    renderWithTooltips(
      <Composer
        {...composerProps({
          showEffort: true,
          showModels: true,
          modelPickerKind: "claude",
          selectedAgentId: "a1",
          agents: [{ id: "a1", name: "claude-native-ui" } as never],
        })}
      />,
    );
    // Radix tooltips open on focus; focusing the trigger reveals the content.
    fireEvent.focus(gear()!);
    const tip = await screen.findByTestId("composer-config-gear-tooltip");
    expect(tip.textContent).toContain("Harness:");
    expect(tip.textContent).toContain("Model:");
    expect(tip.textContent).toContain("Effort:");
    // Effort is switchable in-session; permission mode is not, so it must be absent.
    expect(tip.textContent).not.toContain("Permissions");
  });

  it("reflects Smart Routing in the Model row of the summary when routing is on", async () => {
    useChatStore.setState({ costControlModeOverride: "on" });
    renderWithTooltips(
      <Composer
        {...composerProps({
          showModels: true,
          modelPickerKind: "claude",
          costRoutingEligible: true,
        })}
      />,
    );
    fireEvent.focus(gear()!);
    const tip = await screen.findByTestId("composer-config-gear-tooltip");
    expect(tip.textContent).toContain("Model: Smart Routing");
  });

  it("omits the Effort row from the summary when routing is on (router owns effort)", async () => {
    useChatStore.setState({ costControlModeOverride: "on", selectedEffort: "high" });
    renderWithTooltips(
      <Composer
        {...composerProps({
          showModels: true,
          showEffort: true,
          modelPickerKind: "claude",
          costRoutingEligible: true,
        })}
      />,
    );
    fireEvent.focus(gear()!);
    const tip = await screen.findByTestId("composer-config-gear-tooltip");
    expect(tip.textContent).toContain("Model: Smart Routing");
    // The router picks effort per turn, so a pinned effort must not show.
    expect(tip.textContent).not.toContain("Effort:");
  });

  it("opens the config modal on click", async () => {
    renderWithTooltips(
      <Composer
        {...composerProps({ showEffort: true, showModels: true, modelPickerKind: "claude" })}
      />,
    );
    fireEvent.click(gear()!);
    expect(await screen.findByTestId("composer-config-modal")).toBeTruthy();
    // Claude native → Model + Effort selects present.
    expect(screen.getByTestId("composer-config-model")).toBeTruthy();
    expect(screen.getByTestId("composer-config-effort")).toBeTruthy();
  });

  it("uses the Default sentinel when Kiro marks no catalog row as default", async () => {
    const options = [
      { id: "auto", displayName: "Automatic", isDefault: false },
      { id: "provider-latest", displayName: "Latest", isDefault: false },
    ];
    renderWithTooltips(
      <Composer
        {...composerProps({
          showEffort: false,
          showModels: true,
          modelPickerKind: "kiro",
          codexModelOptions: options,
        })}
      />,
    );

    fireEvent.click(gear()!);
    await screen.findByTestId("composer-config-modal");
    expect(screen.getByTestId("composer-config-model")).toHaveTextContent("Default");
  });

  it("names the model Codex's Default resolves to, like the new-session gear", async () => {
    const options = [
      { id: "gpt-5.6-sol", displayName: "GPT-5.6-Sol" },
      { id: "gpt-5.6-luna", displayName: "GPT-5.6-Luna", isDefault: true },
    ];
    renderWithTooltips(
      <Composer
        {...composerProps({
          showEffort: false,
          showModels: true,
          modelPickerKind: "codex",
          codexModelOptions: options,
        })}
      />,
    );

    fireEvent.click(gear()!);
    await screen.findByTestId("composer-config-modal");
    fireEvent.click(screen.getByTestId("composer-config-model"));
    // A bare "Default" was the bug: this gear and the new-session gear named
    // the same unpinned session's model differently, so neither told the user
    // which model Codex would actually run.
    expect(screen.getByRole("option", { name: "Default (GPT-5.6-Luna)" })).toBeTruthy();
  });

  it("names the model Claude's Default resolves to, like Codex", async () => {
    // The claude branch used to discard the catalog's isDefault marker, so
    // its gear read a bare "Default" while codex named the model — the same
    // shared labeling now serves both harnesses.
    const options = [
      { id: "sonnet", model: "claude-sonnet-5", displayName: "Sonnet 5" },
      {
        id: "opus[1m]",
        model: "claude-opus-4-8[1m]",
        displayName: "Opus 4.8 (1M context)",
        isDefault: true,
      },
    ];
    renderWithTooltips(
      <Composer
        {...composerProps({
          showEffort: false,
          showModels: true,
          modelPickerKind: "claude",
          codexModelOptions: options,
        })}
      />,
    );

    fireEvent.click(gear()!);
    await screen.findByTestId("composer-config-modal");
    fireEvent.click(screen.getByTestId("composer-config-model"));
    expect(screen.getByRole("option", { name: "Default (Opus 4.8 (1M context))" })).toBeTruthy();
  });

  it("does not open the modal via bare /model when the gear is disabled (unreachable)", async () => {
    // Bare /model bumps the open nonce; on an unreachable session the gear is
    // inert, so the nonce must NOT open a modal that can't apply a change.
    const options = [{ id: "opus", model: "opus", displayName: "Opus" }] as never;
    useChatStore.setState({ codexModelOptions: options });
    renderWithTooltips(
      <Composer
        {...composerProps({
          showModels: true,
          modelPickerKind: "claude",
          unreachable: true,
          codexModelOptions: options,
        })}
      />,
    );
    const modelTextarea = document.querySelector("textarea") as HTMLTextAreaElement;
    fireEvent.change(modelTextarea, { target: { value: "/model" } });
    fireEvent.keyDown(modelTextarea, { key: "Enter", code: "Enter" });
    // Give the nonce effect a tick; the modal must stay closed.
    await waitFor(() => expect(gear()).toHaveAttribute("aria-disabled", "true"));
    expect(screen.queryByTestId("composer-config-modal")).toBeNull();
  });

  it("gives a routable agent with no Model dropdown the subagent row, not a Smart Routing switch", async () => {
    // An SDK/bundle agent (Polly) has no Model dropdown and no in-session Smart
    // Routing switch: its own routing is a create-time choice, so Subagent
    // routing is the only routing control the gear offers.
    renderWithTooltips(
      <Composer
        {...composerProps({
          showModels: false,
          modelPickerKind: null,
          costRoutingEligible: true,
          subagentRoutingEligible: true,
        })}
      />,
    );
    fireEvent.click(gear()!);
    await screen.findByTestId("composer-config-modal");
    expect(screen.getByTestId("composer-config-subagent-routing")).toBeTruthy();
    expect(screen.queryByTestId("composer-config-smart-routing")).toBeNull();
  });

  it("folds Smart Routing into the Codex Model dropdown (no standalone switch)", async () => {
    // Regression: Codex has a Model dropdown, so Smart Routing must be an option
    // inside it (like Claude) — NOT a separate switch alongside the dropdown.
    renderWithTooltips(
      <Composer
        {...composerProps({
          showModels: true,
          modelPickerKind: "codex",
          costRoutingEligible: true,
        })}
      />,
    );
    fireEvent.click(gear()!);
    await screen.findByTestId("composer-config-modal");
    expect(screen.getByTestId("composer-config-model")).toBeTruthy();
    expect(screen.queryByTestId("composer-config-smart-routing")).toBeNull();
  });

  it("applies drafted model + effort only on Save, model before effort", async () => {
    // Claude-native types /model and /effort as separate terminal commands, so
    // Save must await the model PATCH before firing effort — otherwise the two
    // injections interleave into one bad line. This pins that ordering.
    const calls: string[] = [];
    let resolveModel: () => void = () => {};
    const setModel = vi.fn().mockImplementation(() => {
      calls.push("model");
      return new Promise<void>((r) => {
        resolveModel = r;
      });
    });
    const setEffort = vi.fn().mockImplementation(() => {
      calls.push("effort");
      return Promise.resolve();
    });
    const options = [
      { id: "opus", model: "opus", displayName: "Opus" },
      { id: "sonnet", model: "sonnet", displayName: "Sonnet" },
    ] as never;
    useChatStore.setState({
      setModel,
      setEffort,
      selectedEffort: "high",
      codexModelOptions: options,
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          showModels: true,
          showEffort: true,
          modelPickerKind: "claude",
          codexModelOptions: options,
        })}
      />,
    );
    fireEvent.click(gear()!);
    await screen.findByTestId("composer-config-modal");
    // Draft a new model and a new effort.
    fireEvent.click(document.querySelector('[data-testid="composer-config-model"]') as Element);
    fireEvent.click(document.querySelector('[data-model-id="sonnet"]') as Element);
    fireEvent.click(document.querySelector('[data-testid="composer-config-effort"]') as Element);
    fireEvent.click(document.querySelector('[data-effort-level="low"]') as Element);
    // Draft only — no live commit yet.
    expect(setModel).not.toHaveBeenCalled();
    expect(setEffort).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("composer-config-save"));
    // Model fires first and effort waits for its promise to resolve.
    await waitFor(() =>
      expect(setModel).toHaveBeenCalledWith("sonnet", { expectConfirmation: true }),
    );
    expect(setEffort).not.toHaveBeenCalled();
    resolveModel();
    await waitFor(() => expect(setEffort).toHaveBeenCalledWith("low"));
    expect(calls).toEqual(["model", "effort"]);
  });

  it("recomputes the Codex effort ladder for the drafted model and drops an unsupported level", async () => {
    // Codex advertises a per-model effort ladder. Drafting a lower-ceiling
    // model (Luna, no "ultra") must refresh the dropdown to that model's levels
    // and drop a picked level it can't run — else Save would send Sol's "ultra"
    // to Luna, and the dropdown would show a rung Luna rejects.
    const codexOptions = [
      {
        id: "gpt-5.6-sol",
        model: "gpt-5.6-sol",
        displayName: "GPT-5.6-Sol",
        isDefault: true,
        supportedReasoningEfforts: [
          { reasoningEffort: "low" },
          { reasoningEffort: "medium" },
          { reasoningEffort: "high" },
          { reasoningEffort: "xhigh" },
          { reasoningEffort: "max" },
          { reasoningEffort: "ultra" },
        ],
      },
      {
        id: "gpt-5.6-luna",
        model: "gpt-5.6-luna",
        displayName: "GPT-5.6-Luna",
        supportedReasoningEfforts: [
          { reasoningEffort: "low" },
          { reasoningEffort: "medium" },
          { reasoningEffort: "high" },
          { reasoningEffort: "xhigh" },
          { reasoningEffort: "max" },
        ],
      },
    ] as never;
    useChatStore.setState({
      setModel: vi.fn().mockResolvedValue(undefined),
      setEffort: vi.fn().mockResolvedValue(undefined),
      selectedEffort: "ultra",
      llmModel: "gpt-5.6-sol",
      codexModelOptions: codexOptions,
      refreshSessionOverrides: vi.fn().mockResolvedValue(undefined),
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          showModels: true,
          showEffort: true,
          modelPickerKind: "codex",
          effortLevels: ["low", "medium", "high", "xhigh", "max", "ultra"],
          codexModelOptions: codexOptions,
        })}
      />,
    );
    fireEvent.click(gear()!);
    await screen.findByTestId("composer-config-modal");

    // Sol starts on ultra.
    expect(screen.getByTestId("composer-config-effort")).toHaveTextContent("ultra");

    // Draft a switch to Luna, whose ceiling is "max".
    fireEvent.click(document.querySelector('[data-testid="composer-config-model"]') as Element);
    fireEvent.click(document.querySelector('[data-model-id="gpt-5.6-luna"]') as Element);

    // The picked ultra is dropped (back to Default) and no longer offered,
    // while Luna's own max stays.
    expect(screen.getByTestId("composer-config-effort")).toHaveTextContent("Default");
    fireEvent.click(document.querySelector('[data-testid="composer-config-effort"]') as Element);
    expect(document.querySelector('[data-effort-level="ultra"]')).toBeNull();
    expect(document.querySelector('[data-effort-level="max"]')).not.toBeNull();
  });

  it("skips unchanged knobs on Save (no spurious slash-command injection)", async () => {
    const setModel = vi.fn().mockResolvedValue(undefined);
    const setEffort = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ setModel, setEffort, selectedEffort: "medium" });
    renderWithTooltips(
      <Composer
        {...composerProps({ showEffort: true, showModels: true, modelPickerKind: "claude" })}
      />,
    );
    fireEvent.click(gear()!);
    await screen.findByTestId("composer-config-modal");
    // Save with nothing changed — no setter should fire.
    fireEvent.click(screen.getByTestId("composer-config-save"));
    await waitFor(() => expect(screen.queryByTestId("composer-config-modal")).toBeNull());
    expect(setModel).not.toHaveBeenCalled();
    expect(setEffort).not.toHaveBeenCalled();
  });

  it("re-pins the model when turning Smart Routing off, even if the shown model is unchanged", async () => {
    // Routing-on clears the applied override but keeps the cross-session sticky
    // (selectedModel), so the modal shows that model as "resolved". Turning
    // routing off by re-picking that same model must still PATCH setModel —
    // otherwise the pin is silently dropped and the session falls back to
    // default. Regression for the resolvedModelId short-circuit false-negative.
    const setModel = vi.fn().mockResolvedValue(undefined);
    const setCostControlMode = vi.fn().mockResolvedValue(undefined);
    const options = [
      { id: "opus", model: "opus", displayName: "Opus" },
      { id: "sonnet", model: "sonnet", displayName: "Sonnet" },
    ] as never;
    useChatStore.setState({
      setModel,
      setCostControlMode,
      // Routing on + leftover sticky "opus", no applied override.
      costControlModeOverride: "on",
      selectedModel: "opus",
      sessionModelOverride: null,
      codexModelOptions: options,
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          showModels: true,
          modelPickerKind: "claude",
          costRoutingEligible: true,
          codexModelOptions: options,
        })}
      />,
    );
    fireEvent.click(gear()!);
    await screen.findByTestId("composer-config-modal");
    // Turn routing off by picking the same model the modal already shows.
    fireEvent.click(document.querySelector('[data-testid="composer-config-model"]') as Element);
    fireEvent.click(document.querySelector('[data-model-id="opus"]') as Element);
    fireEvent.click(screen.getByTestId("composer-config-save"));
    // The pin must be re-applied AND routing cleared.
    await waitFor(() =>
      expect(setModel).toHaveBeenCalledWith("opus", { expectConfirmation: true }),
    );
    expect(setCostControlMode).toHaveBeenCalledWith("off");
  });

  it("discards drafted changes on Cancel", async () => {
    const setModel = vi.fn().mockResolvedValue(undefined);
    const setCostControlMode = vi.fn().mockResolvedValue(undefined);
    const options = [
      { id: "opus", model: "opus", displayName: "Opus" },
      { id: "sonnet", model: "sonnet", displayName: "Sonnet" },
    ] as never;
    useChatStore.setState({ setModel, setCostControlMode, codexModelOptions: options });
    renderWithTooltips(
      <Composer
        {...composerProps({
          // Smart Routing lives in the Model dropdown, so draft it there.
          showModels: true,
          modelPickerKind: "claude",
          costRoutingEligible: true,
          codexModelOptions: options,
        })}
      />,
    );
    fireEvent.click(gear()!);
    await screen.findByTestId("composer-config-modal");
    fireEvent.click(screen.getByTestId("composer-config-model"));
    fireEvent.click(screen.getByRole("option", { name: "Smart Routing" }));
    fireEvent.click(screen.getByTestId("composer-config-cancel"));
    expect(setCostControlMode).not.toHaveBeenCalled();
    expect(setModel).not.toHaveBeenCalled();
  });

  it("folds Smart Routing into the Claude Model dropdown (no standalone switch)", async () => {
    renderWithTooltips(
      <Composer
        {...composerProps({
          showModels: true,
          modelPickerKind: "claude",
          costRoutingEligible: true,
        })}
      />,
    );
    fireEvent.click(gear()!);
    await screen.findByTestId("composer-config-modal");
    // Claude gets the Model select instead of a standalone routing switch.
    expect(screen.getByTestId("composer-config-model")).toBeTruthy();
    expect(screen.queryByTestId("composer-config-smart-routing")).toBeNull();
  });

  // A routed session is pinned to the router's fully-qualified pick, which the
  // harness catalog carries only under an alias — the Model row used to render
  // blank because no option declared that value.
  describe("routed model not in the harness catalog", () => {
    const ROUTED = "databricks-claude-opus-4-8";
    const options = [
      { id: "opus", model: "opus", displayName: "Opus" },
      { id: "sonnet", model: "sonnet", displayName: "Sonnet" },
    ] as never;

    async function openModalOnRoutedSession(setModel = vi.fn().mockResolvedValue(undefined)) {
      useChatStore.setState({
        setModel,
        codexModelOptions: options,
        sessionModelOverride: ROUTED,
        // The forwarder reported the routed model — the display authority.
        llmModel: ROUTED,
        // Routing pinned the model; the session's own routing switch is unset.
        costControlModeOverride: null,
      });
      renderWithTooltips(
        <Composer
          {...composerProps({
            showModels: true,
            modelPickerKind: "claude",
            costRoutingEligible: true,
            codexModelOptions: options,
          })}
        />,
      );
      fireEvent.click(gear()!);
      await screen.findByTestId("composer-config-modal");
      return setModel;
    }

    it("names the model the session is on instead of rendering blank", async () => {
      await openModalOnRoutedSession();
      expect(screen.getByTestId("composer-config-model")).toHaveTextContent(ROUTED);
    });

    it("pins nothing when Save runs without touching the Model row", async () => {
      const setModel = await openModalOnRoutedSession();
      fireEvent.click(screen.getByTestId("composer-config-save"));
      expect(setModel).not.toHaveBeenCalled();
    });

    it("still commits a real pick made from the catalog", async () => {
      const setModel = await openModalOnRoutedSession();
      fireEvent.click(screen.getByTestId("composer-config-model"));
      fireEvent.click(screen.getByRole("option", { name: "Sonnet" }));
      fireEvent.click(screen.getByTestId("composer-config-save"));
      await waitFor(() =>
        expect(setModel).toHaveBeenCalledWith("sonnet", { expectConfirmation: true }),
      );
    });
  });
});

// The gear modal's "Subagent routing" row — the only in-session routing control,
// for native Claude/Codex and SDK/bundle agents alike. Session-shape eligibility
// is pinned in CostRoutingControl.test.tsx (`isSubagentRoutingSession`); these
// tests pin the row's rendering, its effective-value display, and its PATCH on
// Save.
describe("Composer config gear — subagent routing", () => {
  beforeEach(() => {
    useChatStore.setState({
      conversationId: "conv_test",
      skills: [],
      selectedModel: null,
      sessionModelOverride: null,
      llmModel: null,
      nativeVendorOwnsModel: false,
      selectedEffort: null,
      costControlModeOverride: null,
      subagentRoutingOverride: null,
      // Opening the gear re-reads the switches from the server; stub it so
      // these renders don't reach the network.
      refreshSessionOverrides: vi.fn().mockResolvedValue(undefined),
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  const gear = () => document.querySelector('[data-testid="composer-config-gear"]');
  const row = () => screen.queryByTestId("composer-config-subagent-routing");

  /** Open the gear modal for a native Claude session (no in-session IR control). */
  async function openNativeModal(overrides: Record<string, unknown> = {}) {
    renderWithTooltips(
      <Composer
        {...composerProps({
          showModels: true,
          modelPickerKind: "claude",
          // Native terminal sessions keep main-model IR hidden.
          costRoutingEligible: false,
          subagentRoutingEligible: true,
          ...overrides,
        })}
      />,
    );
    fireEvent.click(gear()!);
    await screen.findByTestId("composer-config-modal");
  }

  it("renders the row when the session is subagent-routing eligible", async () => {
    await openNativeModal();
    expect(row()).not.toBeNull();
  });

  it("hides the row when the session is not eligible (routing disabled or wrong harness)", async () => {
    await openNativeModal({ subagentRoutingEligible: false });
    expect(row()).toBeNull();
  });

  // Only the smart-routing flag matters to `isSubagentRoutingEligible`.
  const smartRoutingInfo = { smart_routing_enabled: true } as unknown as Parameters<
    typeof isSubagentRoutingEligible
  >[0];

  it.each([
    ["routed claude-native", "claude-native", "on", {}, true],
    [
      "auto-harness codex-native",
      "codex-native",
      null,
      { "omnigent.routing.auto_harness": "1" },
      true,
    ],
    ["pinned codex-native", "codex-native", "on", {}, true],
    ["plain claude-native", "claude-native", null, {}, false],
    ["plain codex-native", "codex-native", null, {}, false],
  ] as const)(
    "row visibility follows the session's routing class: %s",
    async (_case, harness, costControlModeOverride, extraLabels, visible) => {
      const session = {
        agentName: "coder",
        parentSessionId: null,
        harness,
        costControlModeOverride,
        labels: { "omnigent.wrapper": "claude-code", ...extraLabels },
      } as unknown as Session;
      await openNativeModal({
        subagentRoutingEligible: isSubagentRoutingEligible(smartRoutingInfo, session),
      });
      expect(row() === null).toBe(!visible);
    },
  );

  it("renders the gear for a native session whose only knob is subagent routing", () => {
    renderWithTooltips(
      <Composer
        {...composerProps({
          showEffort: false,
          showModels: false,
          costRoutingEligible: false,
          subagentRoutingEligible: true,
        })}
      />,
    );
    expect(gear()).not.toBeNull();
  });

  it("offers exactly the two states, with no inherit option", async () => {
    await openNativeModal();
    fireEvent.click(row()!);
    expect(document.querySelectorAll("[data-subagent-routing]")).toHaveLength(2);
    expect(document.querySelector('[data-subagent-routing="inherit"]')).toBeNull();
    expect(document.querySelector('[data-subagent-routing="on"]')).not.toBeNull();
    expect(document.querySelector('[data-subagent-routing="off"]')).not.toBeNull();
  });

  it("keeps reading Default while the Model row drafts Smart Routing", async () => {
    // An SDK session CAN switch its own model to Smart Routing, but that says
    // nothing about the stored sub-agent value — previewing the draft here is
    // what made picking the shown value a silent no-op.
    useChatStore.setState({ costControlModeOverride: null, subagentRoutingOverride: null });
    await openNativeModal({ costRoutingEligible: true });
    expect(row()!.textContent).toContain("Default");
    fireEvent.click(screen.getByTestId("composer-config-model"));
    fireEvent.click(screen.getByRole("option", { name: "Smart Routing" }));
    expect(row()!.textContent).toContain("Default");
  });

  // Both directions must PATCH, and neither commits before Save.
  it.each([
    { stored: null, pick: "on" },
    { stored: "on", pick: "off" },
  ] as const)(
    "PATCHes $pick on Save from stored $stored (drafted until then)",
    async ({ stored, pick }) => {
      const setSubagentRouting = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ setSubagentRouting, subagentRoutingOverride: stored });
      await openNativeModal();
      // The description is unconditional now that the row has no third state.
      expect(screen.getByText("Model routing for subagents this session spawns")).toBeTruthy();
      fireEvent.click(row()!);
      fireEvent.click(document.querySelector(`[data-subagent-routing="${pick}"]`) as Element);
      // Drafted only — nothing commits until Save.
      expect(setSubagentRouting).not.toHaveBeenCalled();
      fireEvent.click(screen.getByTestId("composer-config-save"));
      await waitFor(() => expect(setSubagentRouting).toHaveBeenCalledWith(pick));
    },
  );

  it("does not PATCH when the row is left untouched", async () => {
    const setSubagentRouting = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ setSubagentRouting, subagentRoutingOverride: "on" });
    await openNativeModal();
    fireEvent.click(screen.getByTestId("composer-config-save"));
    await waitFor(() => expect(screen.queryByTestId("composer-config-modal")).toBeNull());
    expect(setSubagentRouting).not.toHaveBeenCalled();
  });

  it("writes nothing when an unset session is drafted to Smart Routing and back", async () => {
    // An unset store value already means Default, so returning to it is not a
    // change — the row must not PATCH "off" onto a session that reads Default.
    const setSubagentRouting = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ setSubagentRouting, subagentRoutingOverride: null });
    await openNativeModal();
    fireEvent.click(row()!);
    fireEvent.click(document.querySelector('[data-subagent-routing="on"]') as Element);
    expect(row()!.textContent).toContain("Smart Routing");
    fireEvent.click(row()!);
    fireEvent.click(document.querySelector('[data-subagent-routing="off"]') as Element);
    fireEvent.click(screen.getByTestId("composer-config-save"));
    await waitFor(() => expect(screen.queryByTestId("composer-config-modal")).toBeNull());
    expect(setSubagentRouting).not.toHaveBeenCalled();
  });

  it("discards a drafted pick on Cancel", async () => {
    const setSubagentRouting = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ setSubagentRouting });
    await openNativeModal();
    fireEvent.click(row()!);
    fireEvent.click(document.querySelector('[data-subagent-routing="on"]') as Element);
    fireEvent.click(screen.getByTestId("composer-config-cancel"));
    expect(setSubagentRouting).not.toHaveBeenCalled();
  });

  // Display side of the round-trip: `null` (a session stored before the switch
  // became explicit) collapses onto Default, the same place "off" lands.
  it.each([
    { stored: null, shown: "Default" },
    { stored: "on", shown: "Smart Routing" },
    { stored: "off", shown: "Default" },
  ] as const)("shows $shown for stored $stored", async ({ stored, shown }) => {
    useChatStore.setState({ subagentRoutingOverride: stored });
    await openNativeModal();
    expect(row()!.textContent).toContain(shown);
  });

  // Write side of the same round-trip: every (stored, picked) pair. A pick that
  // changes what the session reads persists exactly itself; re-picking the state
  // it already reads writes nothing (`null` and "off" both read as Default).
  it.each([
    { stored: null, option: "on", written: "on" },
    { stored: null, option: "off", written: null },
    { stored: "on", option: "on", written: null },
    { stored: "on", option: "off", written: "off" },
    { stored: "off", option: "on", written: "on" },
    { stored: "off", option: "off", written: null },
  ] as const)(
    "stored $stored + pick $option writes $written",
    async ({ stored, option, written }) => {
      const setSubagentRouting = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ setSubagentRouting, subagentRoutingOverride: stored });
      await openNativeModal();
      fireEvent.click(row()!);
      fireEvent.click(document.querySelector(`[data-subagent-routing="${option}"]`) as Element);
      fireEvent.click(screen.getByTestId("composer-config-save"));
      await waitFor(() => expect(screen.queryByTestId("composer-config-modal")).toBeNull());
      if (written === null) expect(setSubagentRouting).not.toHaveBeenCalled();
      else expect(setSubagentRouting).toHaveBeenCalledWith(written);
    },
  );

  // The mismatch this pins: the row must not keep showing the value the session
  // had when the modal opened. Nothing pushes a routing-switch change to the
  // client, so the stored value can land (bind snapshot, another tab's PATCH)
  // while the modal sits open — and a Save then wrote the stale value back.
  it("follows the stored value when it changes under an open, untouched row", async () => {
    const setSubagentRouting = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ setSubagentRouting, subagentRoutingOverride: null });
    await openNativeModal();
    expect(row()!.textContent).toContain("Default");

    // The real stored value arrives (the snapshot the store was missing).
    useChatStore.setState({ subagentRoutingOverride: "on" });
    await waitFor(() => expect(row()!.textContent).toContain("Smart Routing"));

    // Untouched → Save writes nothing; the session keeps what it has.
    fireEvent.click(screen.getByTestId("composer-config-save"));
    await waitFor(() => expect(screen.queryByTestId("composer-config-modal")).toBeNull());
    expect(setSubagentRouting).not.toHaveBeenCalled();
  });

  it("keeps the user's pick when the stored value changes under it", async () => {
    const setSubagentRouting = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ setSubagentRouting, subagentRoutingOverride: null });
    await openNativeModal();
    fireEvent.click(row()!);
    fireEvent.click(document.querySelector('[data-subagent-routing="on"]') as Element);

    // A late snapshot must not overwrite what the user just chose.
    useChatStore.setState({ subagentRoutingOverride: "off" });
    expect(row()!.textContent).toContain("Smart Routing");

    fireEvent.click(screen.getByTestId("composer-config-save"));
    await waitFor(() => expect(setSubagentRouting).toHaveBeenCalledWith("on"));
  });

  it("re-reads the stored switches when the modal opens", async () => {
    const refreshSessionOverrides = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ refreshSessionOverrides });
    await openNativeModal();
    expect(refreshSessionOverrides).toHaveBeenCalled();
  });

  // An SDK/bundle agent (Polly, Debby) has no Model dropdown and no in-session
  // Smart Routing switch — its own routing is chosen once at create. Subagent
  // routing is its ONLY gear knob, and it must be the same row native sessions
  // get: same copy, same options, same PATCH, and never a cost-control write.
  describe("SDK/bundle sessions", () => {
    /** Open the gear modal for a bundle agent whose only knob is subagent routing. */
    async function openBundleModal(overrides: Record<string, unknown> = {}) {
      renderWithTooltips(
        <Composer
          {...composerProps({
            showEffort: false,
            showModels: false,
            modelPickerKind: null,
            // Routing-eligible for its own turns, but the switch is create-time only.
            costRoutingEligible: true,
            subagentRoutingEligible: true,
            ...overrides,
          })}
        />,
      );
      fireEvent.click(gear()!);
      await screen.findByTestId("composer-config-modal");
    }

    /** The modal's config rows (direct children of the rows container). */
    function configRows(): Element[] {
      const modal = screen.getByTestId("composer-config-modal");
      return Array.from(modal.querySelectorAll(":scope > div.flex.flex-col.gap-5 > div"));
    }

    it("renders the gear when subagent routing is the only knob", () => {
      renderWithTooltips(
        <Composer
          {...composerProps({
            showEffort: false,
            showModels: false,
            costRoutingEligible: true,
            subagentRoutingEligible: true,
          })}
        />,
      );
      expect(gear()).not.toBeNull();
    });

    it("renders exactly one row — the subagent row, with no switch/Model/Effort", async () => {
      await openBundleModal();
      expect(configRows()).toHaveLength(1);
      expect(row()).not.toBeNull();
      expect(screen.queryByTestId("composer-config-smart-routing")).toBeNull();
      expect(screen.queryByTestId("composer-config-model")).toBeNull();
      expect(screen.queryByTestId("composer-config-effort")).toBeNull();
    });

    it("uses the same copy as a native session", async () => {
      await openBundleModal();
      expect(screen.getByText("Subagent routing")).toBeTruthy();
      expect(screen.getByText("Model routing for subagents this session spawns")).toBeTruthy();
    });

    it("offers exactly the two states, same option values as native", async () => {
      await openBundleModal();
      fireEvent.click(row()!);
      expect(document.querySelectorAll("[data-subagent-routing]")).toHaveLength(2);
      expect(document.querySelector('[data-subagent-routing="on"]')).not.toBeNull();
      expect(document.querySelector('[data-subagent-routing="off"]')).not.toBeNull();
    });

    it.each([
      { stored: null, shown: "Default" },
      { stored: "on", shown: "Smart Routing" },
      { stored: "off", shown: "Default" },
    ] as const)("shows $shown for stored $stored", async ({ stored, shown }) => {
      useChatStore.setState({ subagentRoutingOverride: stored });
      await openBundleModal();
      expect(row()!.textContent).toContain(shown);
    });

    it("PATCHes subagent routing on Save and never touches cost control", async () => {
      const setSubagentRouting = vi.fn().mockResolvedValue(undefined);
      const setCostControlMode = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({
        setSubagentRouting,
        setCostControlMode,
        subagentRoutingOverride: null,
      });
      await openBundleModal();
      fireEvent.click(row()!);
      fireEvent.click(document.querySelector('[data-subagent-routing="on"]') as Element);
      expect(setSubagentRouting).not.toHaveBeenCalled();
      fireEvent.click(screen.getByTestId("composer-config-save"));
      await waitFor(() => expect(setSubagentRouting).toHaveBeenCalledWith("on"));
      // The removed switch was the only thing that wrote cost control here.
      expect(setCostControlMode).not.toHaveBeenCalled();
    });

    it("PATCHes off from stored on, and nothing else", async () => {
      const setSubagentRouting = vi.fn().mockResolvedValue(undefined);
      const setCostControlMode = vi.fn().mockResolvedValue(undefined);
      const setModel = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({
        setSubagentRouting,
        setCostControlMode,
        setModel,
        subagentRoutingOverride: "on",
      });
      await openBundleModal();
      fireEvent.click(row()!);
      fireEvent.click(document.querySelector('[data-subagent-routing="off"]') as Element);
      fireEvent.click(screen.getByTestId("composer-config-save"));
      await waitFor(() => expect(setSubagentRouting).toHaveBeenCalledWith("off"));
      expect(setCostControlMode).not.toHaveBeenCalled();
      expect(setModel).not.toHaveBeenCalled();
    });

    it("writes nothing when Save is pressed untouched", async () => {
      const setSubagentRouting = vi.fn().mockResolvedValue(undefined);
      const setCostControlMode = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({
        setSubagentRouting,
        setCostControlMode,
        subagentRoutingOverride: "on",
      });
      await openBundleModal();
      fireEvent.click(screen.getByTestId("composer-config-save"));
      await waitFor(() => expect(screen.queryByTestId("composer-config-modal")).toBeNull());
      expect(setSubagentRouting).not.toHaveBeenCalled();
      expect(setCostControlMode).not.toHaveBeenCalled();
    });
  });
});

describe("shouldQueueSend", () => {
  const q = (conversationId: string): QueuedMessage => ({
    queueId: `q_${conversationId}`,
    text: "queued",
    conversationId,
  });

  it("sends directly (no queue) for a brand-new chat with no conversation", () => {
    expect(shouldQueueSend(null, "streaming", "running", [])).toBe(false);
  });

  it("queues while the session is busy (streaming or running)", () => {
    expect(shouldQueueSend("conv_a", "streaming", "idle", [])).toBe(true);
    expect(shouldQueueSend("conv_a", "idle", "running", [])).toBe(true);
  });

  it("sends directly when idle and nothing is queued for this conversation", () => {
    expect(shouldQueueSend("conv_a", "idle", "idle", [])).toBe(false);
  });

  it("sends directly on `waiting` (turn ended, only background work remains)", () => {
    // A background shell / still-running sub-agent keeps the session in
    // `waiting`, but the server's turn gate is already free — a new message
    // must start a fresh turn rather than stalling in the client queue.
    expect(shouldQueueSend("conv_a", "idle", "waiting", [])).toBe(false);
  });

  it("queues when idle but this conversation already has a queued message", () => {
    // The ordering fix: an idle flicker must not let a later send overtake the
    // still-queued earlier one.
    expect(shouldQueueSend("conv_a", "idle", "idle", [q("conv_a")])).toBe(true);
  });

  it("ignores queued messages belonging to a different conversation", () => {
    expect(shouldQueueSend("conv_a", "idle", "idle", [q("conv_b")])).toBe(false);
  });
});
