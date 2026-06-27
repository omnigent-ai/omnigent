import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import type { Host } from "@/hooks/useHosts";
import { useHosts } from "@/hooks/useHosts";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { NewChatLandingScreen, sanitizeInitialPrompt } from "./NewChatDialog";

// The landing screen drives the real Web-start flow end to end: the host and
// first agent auto-select, the working directory seeds from the host's most-
// recent path, the composer message is the first prompt, and hitting send
// POSTs /v1/sessions then navigates. The branches under test are the request
// body the screen builds (host_id + workspace + agent_id), the terminal-
// wrapper labels for the claude-native agent, the permission-mode
// terminal_launch_args, the git worktree fields, and the sanitized prompt
// handoff. The host list, agent catalog, conflict hooks, navigation and HTTP
// layers are stubbed so the test isolates that wiring.
const navigateMock = vi.fn();
const setPendingInitialPromptMock = vi.fn();

const RECENT_KEY = "omnigent:recent-workspaces";
// Prompt history is scoped per conversation; the landing composer writes under
// the newly created session id (``conv_new`` in these tests), so the recall
// stack lives at the prefixed key, not the bare one.
const PROMPT_HISTORY_KEY = "omnigent:prompt-history:conv_new";
// The seeded working directory (from the host's persisted recent) that the
// create body must carry through.
const SEEDED_WORKSPACE = "/Users/corey/universe/src/foo";

// The landing screen navigates via the embed-aware routing abstraction
// (`@/lib/routing`), not react-router directly — mock that so the create
// flow's navigate() lands on our spy regardless of router/provider setup.
vi.mock("@/lib/routing", () => ({
  useNavigate: () => navigateMock,
  // The landing screen reads `?project=` to pre-fill the project chip; this
  // flow suite never sets one, so an empty params object is enough.
  useSearchParams: () => [new URLSearchParams(), vi.fn()],
}));

// The screen hands the first message to ChatPage through the chatStore
// (keyed by conversation id), not router state — assert on that call.
vi.mock("@/store/chatStore", () => ({
  setPendingInitialPrompt: (...args: unknown[]) => setPendingInitialPromptMock(...args),
}));

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/hooks/useHosts", () => ({ useHosts: vi.fn() }));
vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
// The home listing is only consulted when there's no recent; the recent is
// always set here, so keep this inert (returns no listing).
vi.mock("@/hooks/useHostFilesystem", () => ({
  useHostFilesystem: () => ({ data: undefined }),
  // WorkspacePicker reads this on mount when the file browser opens;
  // an idle mutation keeps it inert for these tests.
  useCreateHostDirectory: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));
// No other sessions in scope — keep the conflict hooks inert so they don't
// issue their own /health fetch or surface a warning. The warning is covered
// in NewChatDialog.test.tsx.
vi.mock("@/hooks/useDirectorySessions", () => ({
  useDirectorySessions: () => ({ data: [] }),
}));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useRunnerHealthRegistration: () => new Map<string, boolean>(),
}));
// The composer's project chip lists projects via useProjects; stub it to an
// empty list so it doesn't fire its own authenticatedFetch (which would land
// at mock.calls[0] and skew these create-POST call assertions).
vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useConversations")>()),
  useProjects: () => ({ data: [] }),
}));

function host(overrides: Partial<Host> = {}): Host {
  return {
    host_id: "host_1",
    name: "corey-laptop",
    owner: "corey",
    status: "online",
    ...overrides,
  };
}

function agent(overrides: Partial<AvailableAgent> = {}): AvailableAgent {
  return {
    id: "ag_hello",
    name: "hello_world",
    display_name: "Hello World",
    description: null,
    harness: null,
    skills: [],
    ...overrides,
  };
}

function setHosts(hosts: Host[]): void {
  vi.mocked(useHosts).mockReturnValue({ data: hosts } as ReturnType<typeof useHosts>);
}

function setAgents(agents: AvailableAgent[]): void {
  vi.mocked(useAvailableAgents).mockReturnValue({ data: agents } as ReturnType<
    typeof useAvailableAgents
  >);
}

function renderLanding(): void {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  }
  render(<NewChatLandingScreen />, { wrapper: Wrapper });
}

/**
 * Type the composer message that doubles as the first prompt. Submit is
 * disabled until this is non-empty, so every create path needs it.
 */
function typeMessage(text: string): void {
  fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
    target: { value: text },
  });
}

/** Wait for the working directory to seed from the recent before submitting. */
async function waitForWorkspaceSeed(): Promise<void> {
  // The chip shows the basename ("foo") once the seed effect runs.
  await waitFor(() =>
    expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("foo"),
  );
}

/** Open the git-worktree popover so its branch fields mount. */
function openWorktree(): void {
  fireEvent.click(screen.getByTestId("new-chat-landing-branch-chip"));
}

/**
 * Open the agent/harness picker and open <agentId>'s config submenu via
 * keyboard (ArrowRight). A plain click on a knobbed row instead COMMITS the
 * pick and closes the menu, so config flows use the keyboard to drill in.
 */
function openAgentConfig(agentId: string): void {
  fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
  fireEvent.keyDown(screen.getByTestId(`new-chat-landing-agent-${agentId}`), { key: "ArrowRight" });
}

/** Open the picker and commit (select + close) an agent by clicking its row. */
function selectAgent(agentId: string): void {
  fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
  fireEvent.click(screen.getByTestId(`new-chat-landing-agent-${agentId}`));
}

/** Dismiss any open menu so a subsequent submit click isn't swallowed. */
function closeMenu(): void {
  fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
}

beforeEach(() => {
  navigateMock.mockReset();
  setPendingInitialPromptMock.mockReset();
  vi.mocked(authenticatedFetch).mockReset();
  localStorage.clear();
  // Seed host_1's recent so the working directory pre-fills deterministically
  // (the create body must carry SEEDED_WORKSPACE through).
  localStorage.setItem(RECENT_KEY, JSON.stringify({ host_1: [SEEDED_WORKSPACE] }));
  setHosts([host()]);
  setAgents([agent()]);
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("NewChatLandingScreen create flow", () => {
  it("posts host_id, workspace and agent_id to /v1/sessions and navigates", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("inspect the repo");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));

    const [url, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions");
    expect(init.method).toBe("POST");
    // The host (auto-selected), seeded workspace and default agent must all
    // reach the server. A missing host_id/workspace would create an unbound
    // session; a wrong agent_id would launch the wrong assistant.
    const body = JSON.parse(init.body as string);
    expect(body).toMatchObject({
      agent_id: "ag_hello",
      host_id: "host_1",
      workspace: SEEDED_WORKSPACE,
    });
    // A plain YAML agent carries no terminal-wrapper labels.
    expect(body.labels).toBeUndefined();

    // On success the screen routes to the freshly created session.
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_new"));
  });

  it("keeps the seeded working directory when the already-selected host is re-picked", async () => {
    renderLanding();
    await waitForWorkspaceSeed();

    // The first online host auto-selects, so the menu row the user is most
    // likely to click is the one that's already active. Re-picking it must
    // not clear the seeded directory: selectHost used to setWorkspace("")
    // unconditionally, and on a same-host pick none of the seeding effect's
    // inputs (host id, recents, derived home) change, so nothing ever
    // re-filled the field — the chip dropped back to its empty placeholder.
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    fireEvent.click(screen.getByRole("menuitem", { name: /corey-laptop/ }));

    expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("foo");
  });

  it("does not create a session when Enter is pressed with an empty message", async () => {
    // Host, agent and workspace all seed automatically, so the only thing
    // gating submit is a non-empty message. The Send button is disabled in
    // this state, but Enter calls handleCreate() directly — its guard must
    // mirror canSubmit (the disabled condition) or this path POSTs a
    // blank-prompt session behind the disabled button. Regression for the
    // empty-message bug.
    renderLanding();
    await waitForWorkspaceSeed();

    // Submit button reflects the gate: disabled while the message is empty.
    expect(screen.getByTestId("new-chat-landing-submit")).toBeDisabled();

    // Enter on the empty textarea must be a no-op, not a create.
    fireEvent.keyDown(screen.getByTestId("new-chat-landing-input"), { key: "Enter" });

    // No POST fired and no navigation happened — the guard short-circuited.
    // Before the fix the old guard (host/agent/workspace/creating only) let
    // this through and created an unintended empty session.
    expect(authenticatedFetch).not.toHaveBeenCalled();
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it("does not create a session when Enter confirms active IME composition", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    const input = screen.getByTestId("new-chat-landing-input");
    fireEvent.compositionStart(input);
    fireEvent.change(input, { target: { value: "オムニジェント" } });

    fireEvent.keyDown(input, { key: "Enter" });
    expect(authenticatedFetch).not.toHaveBeenCalled();
    expect(navigateMock).not.toHaveBeenCalled();

    fireEvent.compositionEnd(input);
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_new"));
  });

  it("does not create a session when Enter carries the IME keyCode 229 fallback", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    const input = screen.getByTestId("new-chat-landing-input");
    fireEvent.change(input, { target: { value: "omnigent" } });

    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
    expect(authenticatedFetch).not.toHaveBeenCalled();
    expect(navigateMock).not.toHaveBeenCalled();

    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_new"));
  });

  it("hands the sanitized message to the chatStore, not the create body", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Surrounding whitespace + an embedded control char (\x07 bell) prove the
    // screen sanitizes the message before handing it off.
    typeMessage("  read the README\x07 and refactor  ");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));

    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // Anchor on a required field so the absence checks below can't pass
    // vacuously against a malformed/empty body.
    expect(body.agent_id).toBe("ag_hello");
    // The prompt must NOT ride in the create body: for host sessions
    // initial_items are persisted history-only and never fire a turn, so the
    // agent would never respond. It goes through the normal message path from
    // ChatPage instead.
    expect(body.initialPrompt).toBeUndefined();
    expect(body.initial_items).toBeUndefined();

    // It's stashed in the chatStore (keyed by the new conversation id),
    // trimmed + control-char-stripped, for ChatPage to auto-send. Plain
    // text (no leading "/") carries no skill invocation.
    await waitFor(() =>
      expect(setPendingInitialPromptMock).toHaveBeenCalledWith("conv_new", {
        text: "read the README and refactor",
        skill: null,
        files: [],
      }),
    );
    expect(navigateMock).toHaveBeenCalledWith("/c/conv_new");
  });

  it("carries attached files into the chatStore handoff", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    const file = new File(["x"], "diagram.png", { type: "image/png" });
    fireEvent.change(screen.getByTestId("new-chat-landing-file-input"), {
      target: { files: [file] },
    });
    typeMessage("what is in this image?");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    // The picked File rides the pending-prompt handoff so ChatPage's
    // auto-dispatched first turn sends it — files never go in the create
    // body (same reason as the prompt text: initial_items never fire a turn).
    await waitFor(() =>
      expect(setPendingInitialPromptMock).toHaveBeenCalledWith("conv_new", {
        text: "what is in this image?",
        skill: null,
        files: [file],
      }),
    );
  });

  it("hands a bundled-skill first message off as a structured invocation", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);
    setAgents([
      agent({
        skills: [{ name: "review-pr", description: "Review a pull request" }],
      }),
    ]);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("/review-pr 123 focus on auth");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    // The skill payload is what ChatPage's auto-send keys off to post a
    // slash_command instead of a plain message. If matching regressed (or
    // the handoff dropped the skill), the agent would receive literal
    // "/review-pr 123 focus on auth" text — the original bug.
    await waitFor(() =>
      expect(setPendingInitialPromptMock).toHaveBeenCalledWith("conv_new", {
        text: "/review-pr 123 focus on auth",
        skill: { name: "review-pr", args: "123 focus on auth" },
        files: [],
      }),
    );
  });

  it("keeps an unknown slash command as plain text (no skill payload)", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);
    setAgents([
      agent({
        skills: [{ name: "review-pr", description: "Review a pull request" }],
      }),
    ]);

    renderLanding();
    await waitForWorkspaceSeed();
    // Not a bundled skill — e.g. a typo or a host-discovered skill the
    // server can't know pre-session. Falls through to plain text, same as
    // the in-session composer's unknown-command path.
    typeMessage("/typo do something");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() =>
      expect(setPendingInitialPromptMock).toHaveBeenCalledWith("conv_new", {
        text: "/typo do something",
        skill: null,
        files: [],
      }),
    );
  });

  it("keeps slash text plain for native terminal agents", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);
    // A native agent with a (hypothetical) bundled skill of the same name:
    // the vendor CLI interprets slash commands itself, so the handoff must
    // not intercept them even when the name would match.
    setAgents([
      agent({
        id: "ag_claude",
        name: "claude-native-ui",
        display_name: "Claude Code",
        harness: "claude-native",
        skills: [{ name: "review-pr", description: "Review a pull request" }],
      }),
    ]);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("/review-pr 123");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() =>
      expect(setPendingInitialPromptMock).toHaveBeenCalledWith("conv_new", {
        text: "/review-pr 123",
        skill: null,
        files: [],
      }),
    );
  });

  it("records the sanitized prompt in composer history for ArrowUp recall in the new chat", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Same sanitization vehicle as the chatStore handoff test — the history
    // entry must be the SENT prompt (control-char stripped, trimmed), so a
    // recall + resend reproduces exactly what was sent.
    typeMessage("  read the README\x07 and refactor  ");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_new"));
    // appendPromptHistoryEntry is unmocked, so it really wrote to conv_new's
    // scoped key — the one the chat composer reads once bound to that session.
    const history = JSON.parse(localStorage.getItem(PROMPT_HISTORY_KEY) ?? "[]");
    // The stored entry is the SANITIZED prompt: the \x07 bell is gone (proving
    // sanitizeInitialPrompt ran — a bare trim would have kept it) and the
    // surrounding whitespace is trimmed. So a recall + resend reproduces
    // exactly what was sent, not the raw keystrokes.
    expect(history[0]).not.toContain("\x07");
    expect(history).toEqual(["read the README and refactor"]);
  });

  it("attaches terminal-wrapper labels when the claude-native agent is chosen", async () => {
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_native" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("do the thing");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));

    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // The claude-native session opens terminal-first; these labels are what
    // the UI keys off to render the terminal wrapper. Dropping them would make
    // a native Claude Code session render as a plain chat.
    expect(body.labels).toEqual({
      "omnigent.ui": "terminal",
      "omnigent.wrapper": "claude-code-native-ui",
    });
  });

  it("attaches terminal-wrapper labels when the antigravity-native agent is chosen", async () => {
    setAgents([
      agent({ id: "ag_agy", name: "antigravity-native-ui", display_name: "Antigravity" }),
    ]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_agy" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("do the thing");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));

    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // antigravity-native opens terminal-first too; the wrapper value is the
    // agent name (unlike claude, whose wrapper is "claude-code-native-ui").
    // The runner/server key off exactly this value to boot the agy terminal.
    expect(body.labels).toEqual({
      "omnigent.ui": "terminal",
      "omnigent.wrapper": "antigravity-native-ui",
    });
  });

  it("posts --permission-mode <mode> when a non-default mode is picked for claude-native", async () => {
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_native" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Open Claude Code's config submenu (ArrowRight) and pick a non-default
    // permission mode. The create call proves the choice travels as a
    // `--permission-mode <mode>` pair in terminal_launch_args.
    openAgentConfig("ag_native");
    fireEvent.click(screen.getByTestId("new-chat-landing-permission-bypassPermissions"));
    // The trigger label stays the bare agent name (the pick lives in the submenu).
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).not.toContain("(");
    closeMenu();
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // Exactly the two-token flag pair Claude expects. A wrong value (or a
    // bare single token) means the runner would launch claude with the wrong
    // permission mode.
    expect(body.terminal_launch_args).toEqual(["--permission-mode", "bypassPermissions"]);
  });

  it("seeds the permission mode from the last pick for claude-native on a new session", async () => {
    // A returning user's last pick for this harness is on record; the new
    // session must auto-fill it (the "Mode:" pill reflects it) and post it
    // WITHOUT the user re-opening the pill.
    localStorage.setItem(
      "omnigent:last-mode-by-harness",
      JSON.stringify({ "claude-native": "plan" }),
    );
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_native" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Seeded without opening the picker — submitting proves the state was
    // pre-filled from storage and rides along to the create.
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.terminal_launch_args).toEqual(["--permission-mode", "plan"]);
  });

  it("persists the picked permission mode for claude-native so the next session seeds it", async () => {
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_native" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    openAgentConfig("ag_native");
    fireEvent.click(screen.getByTestId("new-chat-landing-permission-acceptEdits"));

    // The pick is snapshotted under the harness key immediately, so the next
    // visit can seed from it.
    await waitFor(() =>
      expect(JSON.parse(localStorage.getItem("omnigent:last-mode-by-harness") ?? "{}")).toEqual({
        "claude-native": "acceptEdits",
      }),
    );
  });

  it("does not leak one harness's mode onto another harness", async () => {
    // Codex has a pick on record; selecting Claude Code (no pick) must stay on
    // its default — modes are keyed per harness, not shared.
    localStorage.setItem(
      "omnigent:last-mode-by-harness",
      JSON.stringify({ "codex-native": "full-access" }),
    );
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);

    renderLanding();
    await waitForWorkspaceSeed();
    // Open Claude Code's submenu: its permission mode is at "Default" (the
    // checked radio), and Codex's "Full access" approval preset doesn't even
    // exist in this submenu — no cross-harness bleed.
    openAgentConfig("ag_native");
    await waitFor(() =>
      expect(
        screen.getByTestId("new-chat-landing-permission-default").getAttribute("aria-checked"),
      ).toBe("true"),
    );
    expect(screen.queryByTestId("new-chat-landing-approval-full-access")).toBeNull();
  });

  it("keeps Codex's approval preset out of OpenCode's submenu (no cross-harness bleed)", async () => {
    // codex-native uses approvalMode (CLI-flag presets); opencode-native uses
    // its own opencodeMode (a session label — `opencode attach` has no
    // permission CLI flag). A codex pick must NOT bleed into OpenCode's launch:
    // OpenCode's submenu has its own modes, and its default posts no flags.
    setAgents([
      agent({ id: "ag_codex", name: "codex-native-ui", display_name: "Codex" }),
      agent({ id: "ag_opencode", name: "opencode-native-ui", display_name: "OpenCode" }),
    ]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_opencode" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Pick "Full access" for Codex (single-section submenu → closes on pick).
    openAgentConfig("ag_codex");
    fireEvent.click(screen.getByTestId("new-chat-landing-approval-full-access"));

    // Open OpenCode's submenu: it shows the opencode permission modes, and
    // Codex's "Full access" approval preset doesn't exist here.
    openAgentConfig("ag_opencode");
    expect(screen.getByTestId("new-chat-landing-opencode-mode-read-only")).toBeTruthy();
    expect(screen.queryByTestId("new-chat-landing-approval-full-access")).toBeNull();

    // Commit OpenCode at its default and submit: no codex sandbox/approval
    // flags, and no opencode mode label (default needs no label).
    selectAgent("ag_opencode");
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.labels?.["omnigent.wrapper"]).toBe("opencode-native-ui");
    expect(body.labels?.["omnigent.opencode_native.permission_mode"]).toBeUndefined();
    expect(body.terminal_launch_args).toBeUndefined();
  });

  it("posts the read-only label (no CLI flags) when read-only is picked for opencode-native", async () => {
    setAgents([agent({ id: "ag_opencode", name: "opencode-native-ui", display_name: "OpenCode" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_opencode" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Open OpenCode's submenu and pick "Read only".
    openAgentConfig("ag_opencode");
    fireEvent.click(screen.getByTestId("new-chat-landing-opencode-mode-read-only"));

    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // The mode rides as a label, NOT terminal_launch_args (opencode attach has
    // no permission CLI flag — that was the original terminal-crash bug).
    expect(body.labels?.["omnigent.opencode_native.permission_mode"]).toBe("read-only");
    expect(body.labels?.["omnigent.wrapper"]).toBe("opencode-native-ui");
    expect(body.terminal_launch_args).toBeUndefined();
  });

  it("omits terminal_launch_args when permission mode is left at default for claude-native", async () => {
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_native" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Untouched default mode → the pill reads as just the agent name, with
    // no "(Default)" suffix.
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).not.toContain("(");
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // Anchor on the wrapper label so the absence check below isn't vacuous
    // against a malformed body.
    expect(body.labels?.["omnigent.wrapper"]).toBe("claude-code-native-ui");
    // "Default" → no flag persisted (undefined is dropped by JSON.stringify),
    // so the runner launches claude with its own default.
    expect(body.terminal_launch_args).toBeUndefined();
  });

  it("rides the default model + effort along to create for claude-native", async () => {
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_native" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Claude Code's effective defaults (Sonnet / Medium) ride along on the
    // create without the user opening the picker — the runner reads them as
    // --model / --effort at terminal launch.
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.model_override).toBe("sonnet");
    expect(body.reasoning_effort).toBe("medium");
  });

  it("rides a picked model + effort along to create for claude-native", async () => {
    setAgents([agent({ id: "ag_native", name: "claude-native-ui", display_name: "Claude Code" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_native" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Model, effort and permission mode share Claude Code's one config submenu;
    // it stays open across picks (multi-section) so both can be set in one visit.
    openAgentConfig("ag_native");
    fireEvent.click(screen.getByTestId("new-chat-landing-model-opus"));
    fireEvent.click(screen.getByTestId("new-chat-landing-effort-high"));
    closeMenu();
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.model_override).toBe("opus");
    expect(body.reasoning_effort).toBe("high");
  });

  it("omits model_override / reasoning_effort for a non-claude-native agent", async () => {
    // hello_world (harness null) has no permission-mode capability, so the
    // model/effort picker never renders and the create carries no model/effort.
    setAgents([agent()]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_x" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    expect(screen.queryByTestId("new-chat-landing-model-trigger")).toBeNull();
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.model_override).toBeUndefined();
    expect(body.reasoning_effort).toBeUndefined();
  });

  it("posts sandbox + approval args when a non-default preset is picked for codex-native", async () => {
    setAgents([agent({ id: "ag_codex", name: "codex-native-ui", display_name: "Codex" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_codex" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Open Codex's config submenu and pick "Full access" (single section →
    // selecting it also commits and closes the menu).
    openAgentConfig("ag_codex");
    fireEvent.click(screen.getByTestId("new-chat-landing-approval-full-access"));
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.terminal_launch_args).toEqual([
      "--sandbox",
      "danger-full-access",
      "--ask-for-approval",
      "never",
    ]);
  });

  it("omits terminal_launch_args when approval mode is left at default for codex-native", async () => {
    setAgents([agent({ id: "ag_codex", name: "codex-native-ui", display_name: "Codex" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_codex" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).not.toContain("(");
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.labels?.["omnigent.wrapper"]).toBe("codex-native-ui");
    expect(body.terminal_launch_args).toBeUndefined();
  });

  it("posts harness_override when a brain harness is picked from the harness menu", async () => {
    // polly's spec declares claude-sdk; the harness dropdown offers the
    // override set.
    setAgents([
      agent({ id: "ag_polly", name: "polly", display_name: "Polly", harness: "claude-sdk" }),
    ]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Open Polly's config submenu and pick the Pi harness (single section →
    // selecting it commits the agent pick and closes the menu).
    openAgentConfig("ag_polly");
    fireEvent.click(screen.getByTestId("new-chat-landing-harness-pi"));
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).not.toContain("(");
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // The pick must travel at create time — the harness spawns on the first
    // turn, so there is no later surface to apply it.
    expect(body.harness_override).toBe("pi");
    expect(body.agent_id).toBe("ag_polly");
  });

  it("omits harness_override and shows the spec default when no harness is picked", async () => {
    setAgents([
      agent({ id: "ag_polly", name: "polly", display_name: "Polly", harness: "claude-sdk" }),
    ]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // With no explicit pick the pill shows just the agent name — the spec
    // default is not suffixed (it lives in the Advanced menu's radios).
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain("Polly");
    expect(screen.getByTestId("new-chat-landing-agent-select").textContent).not.toContain(
      "Claude SDK",
    );
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // Default kept → no override sent, so the session tracks the agent
    // spec's declared harness even if the bundle updates later.
    expect(body.harness_override).toBeUndefined();
  });

  it("re-picking the spec default clears a previous harness override", async () => {
    setAgents([
      agent({ id: "ag_polly", name: "polly", display_name: "Polly", harness: "claude-sdk" }),
    ]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Pick Pi, then change mind back to the spec default (Claude SDK). Each
    // pick closes the single-section submenu, so reopen between the two.
    openAgentConfig("ag_polly");
    fireEvent.click(screen.getByTestId("new-chat-landing-harness-pi"));
    openAgentConfig("ag_polly");
    fireEvent.click(screen.getByTestId("new-chat-landing-harness-claude-sdk"));
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // Re-picking the default must CLEAR the override (not post it
    // explicitly) so the session tracks the spec like an untouched one.
    expect(body.harness_override).toBeUndefined();
  });

  // Skipped while the toggle is hidden behind the false-gate in NewChatDialog; un-skip when re-enabling.
  it.skip("posts cost_control_mode_override when the intelligent-model toggle is flipped on (polly)", async () => {
    // Cost control is a polly-only feature, so the toggle only renders when
    // the selected agent is polly. Seed polly as the sole (auto-selected) agent.
    setAgents([agent({ id: "ag_polly", name: "polly", display_name: "Polly" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    // Click the sparkle toggle — unset flips straight to "on"; the choice
    // must travel in the create body so the switch is persisted before the
    // session's first turn.
    fireEvent.click(screen.getByTestId("cost-toggle-trigger"));
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body.cost_control_mode_override).toBe("on");
  });

  it("hides the Cost Optimized pill for non-polly agents", async () => {
    // The default seeded agent is a plain YAML agent (hello_world), not polly,
    // so the cost pill must not render at all — cost control is polly-only.
    renderLanding();
    await waitForWorkspaceSeed();
    expect(screen.queryByTestId("cost-toggle-trigger")).toBeNull();
  });

  it("omits cost_control_mode_override when the pill is left at spec default (polly)", async () => {
    setAgents([agent({ id: "ag_polly", name: "polly", display_name: "Polly" })]);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("go");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // Anchor on a required field so the absence check can't pass vacuously.
    expect(body.agent_id).toBe("ag_polly");
    // Unset = defer to the spec default; the field must be absent (an
    // explicit null at create would be a pointless write, and "off" here
    // would wrongly disable a spec-configured mode).
    expect(body.cost_control_mode_override).toBeUndefined();
  });

  it("reveals the base-branch field only after a branch name is entered", () => {
    renderLanding();
    openWorktree();
    // Base ref is meaningless without a worktree, so it stays hidden until the
    // user names a branch — then it appears.
    expect(screen.queryByTestId("new-chat-landing-base-branch-input")).toBeNull();
    fireEvent.change(screen.getByTestId("new-chat-landing-branch-input"), {
      target: { value: "feature/login" },
    });
    expect(screen.getByTestId("new-chat-landing-base-branch-input")).toBeInTheDocument();
  });

  it("posts git.branch_name and git.base_branch when both are provided", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    openWorktree();
    fireEvent.change(screen.getByTestId("new-chat-landing-branch-input"), {
      target: { value: "feature/login" },
    });
    fireEvent.change(screen.getByTestId("new-chat-landing-base-branch-input"), {
      target: { value: "main" },
    });
    typeMessage("start the branch");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    // Both the new branch and its base must reach the server so the host
    // creates the worktree off the requested ref, not HEAD.
    const body = JSON.parse(init.body as string);
    expect(body.git).toEqual({ branch_name: "feature/login", base_branch: "main" });
  });

  it("omits base_branch when blank so the host branches from current HEAD", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    openWorktree();
    fireEvent.change(screen.getByTestId("new-chat-landing-branch-input"), {
      target: { value: "feature/login" },
    });
    typeMessage("start the branch");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    // No base_branch key (undefined is dropped by JSON.stringify) → the host
    // falls back to the source repo's current HEAD.
    const body = JSON.parse(init.body as string);
    expect(body.git).toEqual({ branch_name: "feature/login" });
  });

  it("surfaces the server's reason and does not navigate on a failed create", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: false,
      status: 409,
      json: async () => ({ detail: "host is offline" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("inspect the repo");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    // The error message is shown inline, and we stay on the landing page (no
    // navigation to a session that wasn't created).
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-error").textContent).toContain("host is offline"),
    );
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it("remembers the picked agent and preselects it on the next visit", async () => {
    setAgents([agent(), agent({ id: "ag_two", name: "second_agent", display_name: "Second" })]);

    renderLanding();
    await waitForWorkspaceSeed();
    // Pick the non-default agent (Radix opens on pointerdown).
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-agent-ag_two"));
    // The explicit pick persists immediately — no session has to be created
    // for the preference to stick.
    expect(localStorage.getItem("omnigent:last-agent-id")).toBe("ag_two");

    // A fresh mount (the "next visit") must start on the remembered agent:
    // submitting without touching the picker posts ag_two, not the
    // catalog-default ag_hello.
    cleanup();
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);
    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("again");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string).agent_id).toBe("ag_two");
  });

  it("falls back to the default agent when the remembered id is no longer listed", async () => {
    // A persisted pick can outlive its agent (unregistered between visits).
    // The stale id must lose to the catalog default — not yield an unusable
    // composer or post a dangling agent_id.
    localStorage.setItem("omnigent:last-agent-id", "ag_gone");
    vi.mocked(authenticatedFetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: "conv_new" }),
    } as unknown as Response);

    renderLanding();
    await waitForWorkspaceSeed();
    typeMessage("inspect the repo");
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(1));
    const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string).agent_id).toBe("ag_hello");
  });
});

describe("sanitizeInitialPrompt", () => {
  it.each([
    ["trims surrounding whitespace", "  hello  ", "hello"],
    // \n and \t must survive — multi-line prompts depend on it.
    ["preserves newlines and tabs", "line1\n\tline2", "line1\n\tline2"],
    // C0/C1 controls (bell \x07, NUL \x00, DEL \x7f) corrupt tmux
    // send-keys for native terminal agents, so they're stripped.
    ["strips embedded control chars", "a\x07b\x00c\x7fd", "abcd"],
    // Whitespace-only must collapse so the caller sends nothing.
    ["collapses whitespace-only to empty", "  \n\t ", ""],
    ["returns empty for empty input", "", ""],
  ])("%s", (_label, input, expected) => {
    expect(sanitizeInitialPrompt(input)).toBe(expected);
  });
});
