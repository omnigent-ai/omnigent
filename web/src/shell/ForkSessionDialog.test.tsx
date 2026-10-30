import { testAgent } from "@/test/agentFixtures";
import type * as ReactRouterDomModule from "react-router-dom";
import type * as WorkspacePickerModule from "./WorkspacePicker";

import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { TooltipProvider } from "@/components/ui/tooltip";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import { SANDBOX_REPO_LABEL_KEY } from "./NewChatDialog";
import { ForkSessionDialog, ForkSessionForm } from "./ForkSessionDialog";
import { beginClone, cloneKey, pendingClones } from "./cloneRequests";
import { fetchSessionRepositoryBranches, forkSession, launchRunner } from "@/lib/sessionsApi";
import {
  useAvailableAgents,
  prefetchAvailableAgentDetails,
  type AvailableAgent,
} from "@/hooks/useAvailableAgents";
import { useSessionAgent } from "@/hooks/useAgents";
import { useSession } from "@/hooks/useSession";
import { useHosts, useHostModelOptions, type Host } from "@/hooks/useHosts";
import { useDirectorySessions } from "@/hooks/useDirectorySessions";
import { useRunnerHealthRegistration } from "@/hooks/RunnerHealthProvider";
import {
  checkHostDirectory,
  hostDirectoryMissing,
  useHostFilesystem,
} from "@/hooks/useHostFilesystem";
import { toast } from "sonner";

const navigateMock = vi.fn();
vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof ReactRouterDomModule>();
  return { ...actual, useNavigate: () => navigateMock };
});
vi.mock("@/lib/sessionsApi", () => ({
  fetchSessionRepositoryBranches: vi.fn(),
  forkSession: vi.fn(),
  launchRunner: vi.fn(),
}));
vi.mock("sonner", () => ({
  toast: Object.assign(
    vi.fn(() => "cloning"),
    { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() },
  ),
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: vi.fn(),
  prefetchAvailableAgentDetails: vi.fn(),
}));
vi.mock("@/hooks/useAgents", () => ({ useSessionAgent: vi.fn() }));
vi.mock("@/hooks/useSession", () => ({ useSession: vi.fn() }));
vi.mock("@/hooks/useHosts", () => ({ useHosts: vi.fn(), useHostModelOptions: vi.fn() }));
vi.mock("@/hooks/useDirectorySessions", () => ({ useDirectorySessions: vi.fn() }));
vi.mock("@/hooks/RunnerHealthProvider", () => ({ useRunnerHealthRegistration: vi.fn() }));
vi.mock("@/hooks/useHostFilesystem", () => ({
  useHostFilesystem: vi.fn(),
  checkHostDirectory: vi.fn(),
  hostDirectoryMissing: vi.fn(),
}));
// The tree browser only mounts when browsing; coding-fork tests rely on the
// directory being prefilled from the source, so the real picker never opens —
// stub it anyway to keep its filesystem fetch out of the test. Its "Select"
// button commits an absolute path (onSelect), the only way browsing feeds the
// form now that typed ~-paths resolve directly.
vi.mock("./WorkspacePicker", async (importActual) => ({
  ...(await importActual<typeof WorkspacePickerModule>()),
  WorkspacePicker: ({ onSelect }: { onSelect: (p: string) => void }) => (
    <div data-testid="mock-workspace-picker">
      <button
        type="button"
        data-testid="mock-pick-workspace"
        onClick={() => onSelect("/Users/a/git/omnigent")}
      >
        pick
      </button>
    </div>
  ),
}));

const forkSessionMock = vi.mocked(forkSession);
const fetchSessionRepositoryBranchesMock = vi.mocked(fetchSessionRepositoryBranches);
const launchRunnerMock = vi.mocked(launchRunner);
const useAvailableAgentsMock = vi.mocked(useAvailableAgents);
const useSessionAgentMock = vi.mocked(useSessionAgent);
const useHostsMock = vi.mocked(useHosts);
const useHostModelOptionsMock = vi.mocked(useHostModelOptions);
const useSessionMock = vi.mocked(useSession);
const useDirectorySessionsMock = vi.mocked(useDirectorySessions);
const useRunnerHealthMock = vi.mocked(useRunnerHealthRegistration);
const useHostFilesystemMock = vi.mocked(useHostFilesystem);
const checkHostDirectoryMock = vi.mocked(checkHostDirectory);
const hostDirectoryMissingMock = vi.mocked(hostDirectoryMissing);
const prefetchAvailableAgentDetailsMock = vi.mocked(prefetchAvailableAgentDetails);
const toastMock = vi.mocked(toast);

function host(overrides: Partial<Host> = {}): Host {
  return {
    host_id: "host_1",
    name: "serena-laptop",
    owner: "serena",
    status: "online",
    ...overrides,
  } as Host;
}

function setHosts(hosts: Host[]): void {
  useHostsMock.mockReturnValue({ data: hosts } as ReturnType<typeof useHosts>);
}

// Source session runs claude-sdk (anthropic). The picker should keep all
// SDK targets plus same-family native (claude-native) and hide the
// cross-family native target (codex-native).
const AVAILABLE_AGENTS: AvailableAgent[] = [
  testAgent("ag_claude_sdk", "claude", { display_name: "Claude", harness: "claude-sdk" }),
  testAgent("ag_claude_native", "claude-native-ui", {
    display_name: "Claude Code",
    harness: "claude-native",
  }),
  testAgent("ag_codex_native", "codex-native-ui", {
    display_name: "Codex",
    harness: "codex-native",
  }),
  testAgent("ag_openai", "gpt", { display_name: "GPT", harness: "openai-agents" }),
];

function setAgents(available: AvailableAgent[], sourceHarness: string | null): void {
  useAvailableAgentsMock.mockReturnValue({
    data: available,
  } as unknown as ReturnType<typeof useAvailableAgents>);
  useSessionAgentMock.mockReturnValue({
    data: { id: "ag_source", name: "source", harness: sourceHarness },
  } as unknown as ReturnType<typeof useSessionAgent>);
}

function renderDialog(
  props: {
    sourceTitle?: string | null;
    sourceWorkspace?: string | null;
    sourceHostId?: string | null;
    sourceGitBranch?: string | null;
    upToResponseId?: string | null;
    workspaceRecovery?: boolean;
    // Server capabilities the dialog reads. Omitted leaves the context on
    // "loading", which fails the sandbox gate closed — the world every
    // pre-sandbox case here was written against.
    info?: Partial<ServerInfo>;
    // Renders the form without its dialog, so hiding it keeps it mounted
    // (as the dialog's close animation does).
    bareForm?: boolean;
    strict?: boolean;
  } = { sourceTitle: "My session" },
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidateSpy = vi.spyOn(client, "invalidateQueries");
  const onClose = vi.fn();
  const form = { visible: true, upToResponseId: props.upToResponseId };
  const bareForm = () => (
    <ForkSessionForm
      sourceSessionId="conv_src"
      sourceTitle={props.sourceTitle}
      sourceWorkspace={props.sourceWorkspace}
      sourceHostId={props.sourceHostId}
      upToResponseId={form.upToResponseId}
      visible={form.visible}
      onClose={onClose}
    />
  );
  const dialog = () => (
    <ForkSessionDialog
      sourceSessionId="conv_src"
      sourceTitle={props.sourceTitle}
      sourceWorkspace={props.sourceWorkspace}
      sourceHostId={props.sourceHostId}
      sourceGitBranch={props.sourceGitBranch}
      upToResponseId={props.upToResponseId}
      workspaceRecovery={props.workspaceRecovery}
      open
      onOpenChange={vi.fn()}
    />
  );
  const shown = () => (props.bareForm ? bareForm() : dialog());
  const content = () => (
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter>
          {props.info === undefined ? (
            shown()
          ) : (
            <CapabilitiesProvider
              info={{ ...FALLBACK_SERVER_INFO, session_fork_workspaces: true, ...props.info }}
            >
              {shown()}
            </CapabilitiesProvider>
          )}
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>
  );
  const strict = () => (props.strict ? <StrictMode>{content()}</StrictMode> : content());
  const utils = render(strict());
  const update = (next: Partial<typeof form>) => {
    Object.assign(form, next);
    utils.rerender(strict());
  };
  return { ...utils, invalidateSpy, onClose, update, refresh: () => utils.rerender(strict()) };
}

type ForkResponse = Awaited<ReturnType<typeof forkSession>>;

/** Holds the next fork call open until the test settles it. */
function pendingFork(): { succeed: () => void; fail: (message: string) => void } {
  let succeed = () => {};
  let fail = (_message: string) => {};
  forkSessionMock.mockReturnValue(
    new Promise<ForkResponse>((resolve, reject) => {
      succeed = () => resolve({ id: "conv_fork" } as ForkResponse);
      fail = (message) => reject(new Error(message));
    }),
  );
  return { succeed: () => succeed(), fail: (message) => fail(message) };
}

/** Holds the directory pre-flight open until the test settles it with `problem`. */
function pendingPreflight(problem: string | null = null): () => void {
  let pass = () => {};
  checkHostDirectoryMock.mockReturnValue(
    new Promise<string | null>((resolve) => {
      pass = () => resolve(problem);
    }),
  );
  return () => pass();
}

const IDLE_LISTENER = { onSuccess: () => {}, onError: () => {} };

/** Open the Radix host <Select> (hosts + sandbox rows). */
function openHostSelect(): void {
  const trigger = screen.getByTestId("fork-session-host-select");
  fireEvent.pointerDown(trigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
  fireEvent.click(trigger);
}

/** Open the Radix agent <Select> (mirrors NewChatDialog.test). */
function openAgentSelect(): void {
  const trigger = screen.getByTestId("fork-session-agent-select");
  fireEvent.pointerDown(trigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
  fireEvent.click(trigger);
}

/** Expand the collapsed "Advanced settings" (working dir + git worktree). */
function openAdvanced(): void {
  fireEvent.click(screen.getByTestId("fork-session-advanced-toggle"));
}

beforeEach(() => {
  fetchSessionRepositoryBranchesMock.mockReset().mockResolvedValue({ repositories: [] });
  forkSessionMock.mockReset();
  launchRunnerMock.mockReset();
  navigateMock.mockReset();
  // The submit pre-flight passes by default; the nonexistent-directory
  // test overrides it with a failure message.
  checkHostDirectoryMock.mockReset();
  hostDirectoryMissingMock.mockReset();
  checkHostDirectoryMock.mockResolvedValue(null);
  prefetchAvailableAgentDetailsMock.mockReset();
  pendingClones.clear();
  toastMock.mockClear();
  toastMock.success.mockClear();
  toastMock.error.mockClear();
  toastMock.dismiss.mockClear();
  setAgents(AVAILABLE_AGENTS, "claude-sdk");
  // The run-config section reads the source snapshot (to seed a same-harness
  // fork) and, for a native target, the host model catalog. Default both to
  // empty so the section renders nothing for the SDK source these tests use
  // and stays inert until a test opts into a native switch.
  useSessionMock.mockReturnValue({
    session: null,
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof useSession>);
  useHostModelOptionsMock.mockReturnValue({
    data: [],
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof useHostModelOptions>);
  // Defaults for the coding-fork wiring; the non-coding tests don't render
  // these fields but the hooks still run (with isCodingSource false).
  setHosts([host()]);
  useDirectorySessionsMock.mockReturnValue({ data: [] } as unknown as ReturnType<
    typeof useDirectorySessions
  >);
  useRunnerHealthMock.mockReturnValue(new Map());
  useHostFilesystemMock.mockReturnValue({
    data: undefined,
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof useHostFilesystem>);
});

afterEach(cleanup);

describe("ForkSessionDialog", () => {
  it.each<[string, string | null, Partial<ServerInfo> | undefined]>([
    ["managed snapshot", null, undefined],
    ["Databricks Sandbox host", "lakebox", undefined],
    [
      "Arclet host without an Arclet to fork onto",
      "arclet",
      { managed_sandboxes_enabled: true, sandbox_providers: ["lakebox"] },
    ],
  ])(
    "blocks the whole form for an unsupported source detected by %s",
    async (source, provider, info) => {
      useSessionMock.mockReturnValue({
        session: {
          hostId: "host_1",
          labels: source === "managed snapshot" ? { "omnigent.host_type": "managed" } : {},
        },
        isLoading: false,
        error: null,
      } as ReturnType<typeof useSession>);
      setHosts([host({ sandbox_provider: provider })]);
      renderDialog({ sourceHostId: "host_1", sourceWorkspace: "/Users/a/repo", info });

      const submit = screen.getByTestId("fork-session-submit");
      expect(submit).toBeDisabled();
      expect(screen.getByRole("group", { name: "Clone session" })).toBe(submit.parentElement);
      expect(screen.getByTestId("fork-session-unavailable")).toHaveTextContent(
        "Forking this sandbox session is not supported yet.",
      );
      expect(screen.queryByTestId("fork-session-host-select")).not.toBeInTheDocument();
      expect(screen.queryByTestId("fork-session-advanced-toggle")).not.toBeInTheDocument();
      fireEvent.click(submit);
      fireEvent.focus(submit.parentElement!);
      fireEvent.keyDown(submit.parentElement!, { key: "Enter" });
      await waitFor(() =>
        expect(screen.getByRole("tooltip")).toHaveTextContent(
          "Forking this sandbox session is not supported yet.",
        ),
      );
      expect(submit.parentElement).toHaveAccessibleDescription(
        "Forking this sandbox session is not supported yet.",
      );
      expect(forkSessionMock).not.toHaveBeenCalled();
      expect(launchRunnerMock).not.toHaveBeenCalled();
      expect(checkHostDirectoryMock).not.toHaveBeenCalled();
    },
  );

  it("does not expose an actionable form before the source snapshot resolves", () => {
    useSessionMock.mockReturnValue({ session: null, isLoading: true, error: null });
    renderDialog({ sourceHostId: "host_1", sourceWorkspace: "/Users/a/repo" });
    expect(screen.getByRole("status")).toHaveTextContent("Checking session capabilities…");
    expect(screen.queryByTestId("fork-session-submit")).not.toBeInTheDocument();
    expect(screen.queryByTestId("fork-session-host-select")).not.toBeInTheDocument();
  });

  it("moves keyboard focus into the form when source capabilities finish loading", async () => {
    const user = userEvent.setup();
    useSessionMock.mockReturnValue({ session: null, isLoading: true, error: null });
    const { refresh } = renderDialog();
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();

    useSessionMock.mockReturnValue({
      session: null,
      isLoading: false,
      error: new Error("Session unavailable"),
    });
    refresh();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Unable to check session capabilities. Reload to try again.",
    );
    expect(screen.getByTestId("fork-session-submit")).toBeDisabled();

    useSessionMock.mockReturnValue({ session: null, isLoading: false, error: null });
    refresh();
    expect(screen.getByTestId("fork-session-agent-select")).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("listbox")).toBeVisible();
    expect(forkSessionMock).not.toHaveBeenCalled();
  });

  it("leaves the name optional, suggesting 'Fork of <title>' as the placeholder", () => {
    renderDialog({ sourceTitle: "My session" });
    // Name lives under Advanced now (optional, prefilled-by-placeholder).
    openAdvanced();
    const input = screen.getByTestId("fork-session-title-input");
    expect(input).toHaveValue("");
    expect(input).toHaveAttribute("placeholder", "Fork of My session");
  });

  it("falls back to a generic placeholder when the source has no title", () => {
    renderDialog({ sourceTitle: null });
    openAdvanced();
    const input = screen.getByTestId("fork-session-title-input");
    expect(input).toHaveValue("");
    expect(input).toHaveAttribute("placeholder", "Name the cloned session");
  });

  it("forks with the edited title, refreshes the list, and navigates into the fork", async () => {
    forkSessionMock.mockResolvedValue({
      id: "conv_fork",
    } as unknown as Awaited<ReturnType<typeof forkSession>>);

    const { invalidateSpy } = renderDialog();

    openAdvanced();
    fireEvent.change(screen.getByTestId("fork-session-title-input"), {
      target: { value: "My clone" },
    });
    fireEvent.click(screen.getByTestId("fork-session-submit"));

    await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
    // No agent switch → agent_id omitted (undefined) so the server keeps
    // the source's agent.
    // A non-native (SDK) source with no agent switch renders no run-config
    // section, so the config arg is an empty object (no run overrides sent).
    expect(forkSessionMock).toHaveBeenCalledWith("conv_src", {
      title: "My clone",
      agentId: undefined,
      upToResponseId: undefined,
      config: {},
      sandbox: undefined,
    });
    // Session list refreshed so the fork shows in the sidebar, then navigated.
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["conversations"] });
    // A fork inherits the source's project, so the project-folder lists must
    // refetch too — otherwise a filed fork stays missing from its folder.
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["project-sessions"] });
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_fork"));
    expect(toastMock).not.toHaveBeenCalled();
    expect(toastMock.success).not.toHaveBeenCalled();
  });

  it("spins the submit button while the fork is in flight", async () => {
    // The fork call can take seconds. Without the spinner the button only
    // fades (disabled), which reads as a hang rather than work in progress.
    type Fork = Awaited<ReturnType<typeof forkSession>>;
    let settle: (value: Fork) => void = () => {};
    forkSessionMock.mockReturnValue(
      new Promise<Fork>((resolve) => {
        settle = resolve;
      }),
    );

    renderDialog();
    const submit = screen.getByTestId("fork-session-submit");
    fireEvent.click(submit);

    await waitFor(() => expect(submit).toHaveAttribute("aria-busy", "true"));
    expect(submit).toBeDisabled();
    expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument();

    settle({ id: "conv_fork" } as Fork);
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_fork"));
  });

  it("passes the truncation point through on a 'fork from here' and retitles the dialog", async () => {
    forkSessionMock.mockResolvedValue({
      id: "conv_fork",
    } as unknown as Awaited<ReturnType<typeof forkSession>>);
    renderDialog({ sourceTitle: "My session", upToResponseId: "resp_cut" });

    // A truncated fork renames the dialog so the user knows history after
    // the selected response won't be carried over.
    expect(screen.getByText("Fork from this response")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("fork-session-submit"));

    await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
    // The 4th arg is the truncation point — undefined here would mean the
    // dialog dropped it and the fork silently copied the full history.
    expect(forkSessionMock).toHaveBeenCalledWith("conv_src", {
      title: undefined,
      agentId: undefined,
      upToResponseId: "resp_cut",
      config: {},
      sandbox: undefined,
    });
  });

  it("omits the title (server derives it) when the field is cleared", async () => {
    forkSessionMock.mockResolvedValue({
      id: "conv_fork",
    } as unknown as Awaited<ReturnType<typeof forkSession>>);
    renderDialog();

    openAdvanced();
    fireEvent.change(screen.getByTestId("fork-session-title-input"), {
      target: { value: "   " },
    });
    fireEvent.click(screen.getByTestId("fork-session-submit"));

    await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
    // Whitespace-only → undefined so the server applies "Fork of <title>".
    expect(forkSessionMock).toHaveBeenCalledWith("conv_src", {
      title: undefined,
      agentId: undefined,
      upToResponseId: undefined,
      config: {},
      sandbox: undefined,
    });
  });

  it("pressing Enter in the title input submits the fork", async () => {
    forkSessionMock.mockResolvedValue({
      id: "conv_fork",
    } as unknown as Awaited<ReturnType<typeof forkSession>>);

    renderDialog();

    openAdvanced();
    fireEvent.keyDown(screen.getByTestId("fork-session-title-input"), {
      key: "Enter",
    });

    await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_fork"));
  });

  it("surfaces the server error inline on failure and does not navigate", async () => {
    forkSessionMock.mockRejectedValue(new Error("403 forbidden"));
    renderDialog();

    fireEvent.click(screen.getByTestId("fork-session-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("fork-session-error")).toHaveTextContent("403 forbidden"),
    );
    // A failed fork must not navigate the user away from the source session.
    expect(navigateMock).not.toHaveBeenCalled();
    expect(toastMock.error).not.toHaveBeenCalled();
    expect(screen.getByTestId("fork-session-submit")).not.toHaveAttribute("aria-busy");
  });

  describe("clone outliving its form", () => {
    const CODING = {
      sourceTitle: "My session",
      sourceWorkspace: "/Users/a/repo",
      sourceHostId: "host_1",
      bareForm: true,
    };

    it("cancels a directory preflight when the form hides before it resolves", async () => {
      const pass = pendingPreflight();
      const { update } = renderDialog(CODING);
      fireEvent.click(screen.getByTestId("fork-session-submit"));
      update({ visible: false });
      pass();
      await waitFor(() =>
        expect(screen.getByTestId("fork-session-submit")).not.toHaveAttribute("aria-busy"),
      );
      expect(forkSessionMock).not.toHaveBeenCalled();
      expect(toastMock).not.toHaveBeenCalled();
    });

    it("keeps a closed directory preflight cancelled if the form reopens before it resolves", async () => {
      const pass = pendingPreflight();
      const { update } = renderDialog(CODING);
      fireEvent.click(screen.getByTestId("fork-session-submit"));
      update({ visible: false });
      update({ visible: true });
      pass();
      await waitFor(() =>
        expect(screen.getByTestId("fork-session-submit")).not.toHaveAttribute("aria-busy"),
      );
      expect(forkSessionMock).not.toHaveBeenCalled();
      expect(toastMock).not.toHaveBeenCalled();
    });

    it("keeps a cancelled directory preflight's problem off the reopened form", async () => {
      const fail = pendingPreflight("Directory not found");
      const { update } = renderDialog(CODING);
      fireEvent.click(screen.getByTestId("fork-session-submit"));
      update({ visible: false });
      update({ visible: true });
      fail();
      await waitFor(() =>
        expect(screen.getByTestId("fork-session-submit")).not.toHaveAttribute("aria-busy"),
      );
      expect(screen.queryByTestId("fork-session-error")).not.toBeInTheDocument();
    });

    it("reports a clone that succeeds after its form hides with an Open toast", async () => {
      const fork = pendingFork();
      const { update, onClose } = renderDialog({ bareForm: true });
      fireEvent.click(screen.getByTestId("fork-session-submit"));
      await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
      update({ visible: false });
      await waitFor(() =>
        expect(toastMock).toHaveBeenCalledWith(
          "Creating session…",
          expect.objectContaining({ duration: Infinity, closeButton: true }),
        ),
      );
      fork.succeed();
      await waitFor(() => expect(toastMock.success).toHaveBeenCalledTimes(1));
      const [title, options] = toastMock.success.mock.calls[0];
      expect(title).toBe("Session created");
      expect(options).toMatchObject({ id: "cloning", duration: 10_000 });
      expect(onClose).not.toHaveBeenCalled();
      expect(navigateMock).not.toHaveBeenCalled();
      (options as unknown as { action: { onClick: () => void } }).action.onClick();
      expect(navigateMock).toHaveBeenCalledWith("/c/conv_fork");
    });

    it("reports a clone that fails after its form hides with a persistent error toast", async () => {
      const fork = pendingFork();
      const { update } = renderDialog({ bareForm: true });
      fireEvent.click(screen.getByTestId("fork-session-submit"));
      await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
      update({ visible: false });
      await waitFor(() => expect(toastMock).toHaveBeenCalledTimes(1));
      fork.fail("disk full");
      await waitFor(() =>
        expect(toastMock.error).toHaveBeenCalledWith("Couldn't create the session", {
          id: "cloning",
          description: "disk full",
          duration: Infinity,
          closeButton: true,
        }),
      );
      expect(screen.queryByTestId("fork-session-error")).not.toBeInTheDocument();
    });

    it("keeps a clone running after its dialog unmounts", async () => {
      const fork = pendingFork();
      const { unmount } = renderDialog();
      fireEvent.click(screen.getByTestId("fork-session-submit"));
      await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
      unmount();
      await waitFor(() => expect(toastMock).toHaveBeenCalledTimes(1));
      fork.succeed();
      await waitFor(() => expect(toastMock.success).toHaveBeenCalledTimes(1));
      expect(navigateMock).not.toHaveBeenCalled();
    });

    it("takes a running clone back inline when the form shows again", async () => {
      const fork = pendingFork();
      const { update } = renderDialog({ bareForm: true });
      fireEvent.click(screen.getByTestId("fork-session-submit"));
      await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
      update({ visible: false });
      await waitFor(() => expect(toastMock).toHaveBeenCalledTimes(1));
      update({ visible: true });
      expect(toastMock.dismiss).toHaveBeenCalledWith("cloning");
      fork.fail("disk full");
      await waitFor(() =>
        expect(screen.getByTestId("fork-session-error")).toHaveTextContent("disk full"),
      );
      expect(toastMock.error).not.toHaveBeenCalled();
    });

    it("shows a running clone on a new form instead of starting another", async () => {
      const fork = pendingFork();
      renderDialog();
      fireEvent.click(screen.getByTestId("fork-session-submit"));
      await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
      cleanup();
      renderDialog();
      const submit = screen.getByTestId("fork-session-submit");
      expect(submit).toHaveAttribute("aria-busy", "true");
      expect(screen.getByText("Close", { selector: "button" })).toBeEnabled();
      expect(screen.getByText(/You can close this, and it continues/)).toBeInTheDocument();
      fireEvent.click(submit);
      openAdvanced();
      fireEvent.keyDown(screen.getByTestId("fork-session-title-input"), { key: "Enter" });
      expect(forkSessionMock).toHaveBeenCalledTimes(1);
      fork.fail("disk full");
      await waitFor(() =>
        expect(screen.getByTestId("fork-session-error")).toHaveTextContent("disk full"),
      );
    });

    it("does not reattach a clone that settled while the closing form remained mounted", async () => {
      const fork = pendingFork();
      const { update } = renderDialog({ bareForm: true });
      fireEvent.click(screen.getByTestId("fork-session-submit"));
      await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
      update({ visible: false });
      await waitFor(() => expect(toastMock).toHaveBeenCalledTimes(1));
      fork.succeed();
      await waitFor(() => expect(toastMock.success).toHaveBeenCalledTimes(1));
      update({ visible: true });
      expect(screen.getByTestId("fork-session-submit")).not.toHaveAttribute("aria-busy");
      expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
      expect(toastMock.dismiss).not.toHaveBeenCalled();
      expect(navigateMock).not.toHaveBeenCalled();
    });

    it("shows no loading toast when a StrictMode form mounts over a running clone", async () => {
      beginClone(cloneKey("conv_src"), IDLE_LISTENER, vi.fn());
      renderDialog({ bareForm: true, strict: true });
      await Promise.resolve();
      expect(screen.getByTestId("fork-session-submit")).toHaveAttribute("aria-busy", "true");
      expect(toastMock).not.toHaveBeenCalled();
    });

    it("follows the running clone for the current cutoff and kind", () => {
      beginClone(cloneKey("conv_src", "resp_cut"), IDLE_LISTENER, vi.fn());
      beginClone(cloneKey("conv_src", null, true), IDLE_LISTENER, vi.fn());
      const { update } = renderDialog({ bareForm: true });
      expect(screen.getByTestId("fork-session-submit")).not.toHaveAttribute("aria-busy");
      update({ upToResponseId: "resp_cut" });
      expect(screen.getByTestId("fork-session-submit")).toHaveAttribute("aria-busy", "true");
    });

    it("allows a new clone once the user dismisses the running one's toast", async () => {
      pendingFork();
      const { update } = renderDialog({ bareForm: true });
      fireEvent.click(screen.getByTestId("fork-session-submit"));
      await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
      update({ visible: false });
      await waitFor(() => expect(toastMock).toHaveBeenCalledTimes(1));
      act(() => (toastMock.mock.calls[0][1] as { onDismiss: () => void }).onDismiss());
      update({ visible: true });
      fireEvent.click(screen.getByTestId("fork-session-submit"));
      await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(2));
    });

    it("reports the outcome once after the form hides and shows twice", async () => {
      const fork = pendingFork();
      const { update, onClose } = renderDialog({ bareForm: true });
      fireEvent.click(screen.getByTestId("fork-session-submit"));
      await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
      update({ visible: false });
      await waitFor(() => expect(toastMock).toHaveBeenCalledTimes(1));
      update({ visible: true });
      update({ visible: false });
      await waitFor(() => expect(toastMock).toHaveBeenCalledTimes(2));
      update({ visible: true });
      fork.succeed();
      await waitFor(() => expect(navigateMock).toHaveBeenCalledTimes(1));
      expect(navigateMock).toHaveBeenCalledWith("/c/conv_fork");
      expect(onClose).toHaveBeenCalledTimes(1);
      expect(toastMock.success).not.toHaveBeenCalled();
    });
  });

  it("offers history-preserving targets including cross-family codex-native", () => {
    // Source is claude-sdk (anthropic). Every classifiable target carries
    // history: SDK targets replay the transcript as context, and native
    // targets (claude-native AND codex-native) rebuild their on-disk
    // transcript from the copied Omnigent items — the codex rollout
    // synthesizer writes the session_meta + event_msg records codex
    // ≥ 0.133 needs (verified on 0.136.0), so cross-family codex-native
    // is offered too.
    renderDialog();
    openAgentSelect();

    expect(screen.getByTestId("fork-session-agent-option-same")).toBeInTheDocument();
    expect(screen.getByTestId("fork-session-agent-option-ag_claude_sdk")).toBeInTheDocument();
    // Same-family native (claude-native): rebuild-from-items carries history.
    expect(screen.getByTestId("fork-session-agent-option-ag_claude_native")).toBeInTheDocument();
    // SDK target of a different family still carries history as context.
    expect(screen.getByTestId("fork-session-agent-option-ag_openai")).toBeInTheDocument();
    // Cross-family codex-native: rebuild-from-items carries history.
    expect(screen.getByTestId("fork-session-agent-option-ag_codex_native")).toBeInTheDocument();
  });

  it("excludes the source's own agent so it doesn't duplicate 'Same as source'", () => {
    // Regression: a Claude Code (claude-native) session listed both
    // "Same as source" AND "Claude Code" (the claude-native-ui built-in
    // it's bound to) — the same agent twice. The source's own agent must
    // be hidden from the switch list.
    setAgents(AVAILABLE_AGENTS, "claude-native");
    useSessionAgentMock.mockReturnValue({
      data: { id: "ag_claude_native", name: "claude-native-ui", harness: "claude-native" },
    } as unknown as ReturnType<typeof useSessionAgent>);
    renderDialog();
    openAgentSelect();

    expect(screen.getByTestId("fork-session-agent-option-same")).toBeInTheDocument();
    // The source's own agent (claude-native-ui → "Claude Code") is hidden.
    expect(
      screen.queryByTestId("fork-session-agent-option-ag_claude_native"),
    ).not.toBeInTheDocument();
    // Other same-family targets remain: the claude-sdk agent is still offered.
    expect(screen.getByTestId("fork-session-agent-option-ag_claude_sdk")).toBeInTheDocument();
  });

  it("excludes the source's agent even when it's a '(fork …)' clone", () => {
    // A fork-of-a-fork: the source's bound agent is a session-scoped clone
    // named "<builtin> (fork ag_…)". The dedup must strip that suffix so the
    // built-in it derives from is still hidden (no duplicate of "Same as
    // source"). Source here is databricks_coding_agent (openai-agents).
    const agents = [
      ...AVAILABLE_AGENTS,
      testAgent("ag_dbx", "databricks_coding_agent", {
        display_name: "databricks_coding_agent",
        harness: "openai-agents",
      }),
    ];
    setAgents(agents, "openai-agents");
    useSessionAgentMock.mockReturnValue({
      data: {
        id: "ag_dbx_fork",
        name: "databricks_coding_agent (fork ag_5c78e6a)",
        harness: "openai-agents",
      },
    } as unknown as ReturnType<typeof useSessionAgent>);
    renderDialog();
    openAgentSelect();

    // The built-in the source forked from is hidden despite the name suffix.
    expect(screen.queryByTestId("fork-session-agent-option-ag_dbx")).not.toBeInTheDocument();
    // A same-family SDK target (openai gpt) is still offered.
    expect(screen.getByTestId("fork-session-agent-option-ag_openai")).toBeInTheDocument();
    // Cross-family claude-native is offered for an openai source — the
    // runner rebuilds its transcript from the copied Omnigent items.
    expect(screen.getByTestId("fork-session-agent-option-ag_claude_native")).toBeInTheDocument();
  });

  it("excludes the source's agent and resolves its label for a '(switch …)' clone", () => {
    // The in-place Switch Agent flow names its clone "<builtin> (switch <id>)"
    // (server: cloned_agent_name = f"{name} (switch …)"). The previous
    // single-layer, fork-only strip left that suffix in place, so forking a
    // switched session showed the raw slug as the "same as source" label and
    // re-listed the current built-in as a switch target. agentRootName peels
    // the "(switch …)" layer too.
    setAgents(AVAILABLE_AGENTS, "claude-native");
    useSessionAgentMock.mockReturnValue({
      data: {
        id: "ag_claude_switch",
        name: "claude-native-ui (switch ag_c537b7b)",
        harness: "claude-native",
      },
    } as unknown as ReturnType<typeof useSessionAgent>);
    renderDialog();
    openAgentSelect();

    // Label resolves to the built-in's display name, not the raw suffixed slug.
    expect(screen.getByTestId("fork-session-agent-option-same")).toHaveTextContent("Claude Code");
    // The built-in the source was switched to is hidden (no duplicate).
    expect(
      screen.queryByTestId("fork-session-agent-option-ag_claude_native"),
    ).not.toBeInTheDocument();
    // Other same-family targets remain.
    expect(screen.getByTestId("fork-session-agent-option-ag_claude_sdk")).toBeInTheDocument();
  });

  it("excludes the source's agent for a nested fork-of-a-fork clone", () => {
    // Nested clones accumulate suffixes: "<builtin> (fork a) (fork b)". A
    // single-layer strip would leave "<builtin> (fork a)", miss the match, and
    // re-list the built-in. agentRootName recurses to the root.
    setAgents(AVAILABLE_AGENTS, "claude-native");
    useSessionAgentMock.mockReturnValue({
      data: {
        id: "ag_claude_nested",
        name: "claude-native-ui (fork ag_b629fd6) (fork ag_a286ffe)",
        harness: "claude-native",
      },
    } as unknown as ReturnType<typeof useSessionAgent>);
    renderDialog();
    openAgentSelect();

    expect(screen.getByTestId("fork-session-agent-option-same")).toHaveTextContent("Claude Code");
    expect(
      screen.queryByTestId("fork-session-agent-option-ag_claude_native"),
    ).not.toBeInTheDocument();
  });

  it("offers fork-only preamble (opencode) and rebuild (hermes) native targets", () => {
    // OpenCode carries fork history as a text preamble; Hermes via native
    // rebuild. Both must appear in the FORK picker — unlike the switch picker,
    // where the fork-only preamble target (opencode) is hidden.
    setAgents(
      [
        testAgent("ag_opencode", "opencode-native-ui", {
          display_name: "OpenCode",
          harness: "opencode-native",
        }),
        testAgent("ag_hermes", "hermes-native-ui", {
          display_name: "Hermes",
          harness: "hermes-native",
        }),
      ],
      "claude-sdk",
    );
    renderDialog();
    openAgentSelect();

    expect(screen.getByTestId("fork-session-agent-option-ag_opencode")).toBeInTheDocument();
    expect(screen.getByTestId("fork-session-agent-option-ag_hermes")).toBeInTheDocument();
  });

  it("prefetches harness details for all agents on mount so custom agents appear", async () => {
    // Custom agents discovered from session scans start with harness=null and
    // a sessionId. Without eager prefetch, forkTargetCarriesHistory(null)
    // returns false and they never appear in the fork picker.
    const customAgent: AvailableAgent = testAgent("ag_custom", "my-agent", {
      display_name: "My Agent",
      harness: null,
      sessionId: "conv_custom",
    });
    setAgents([...AVAILABLE_AGENTS, customAgent], "claude-sdk");

    renderDialog();

    await waitFor(() =>
      expect(prefetchAvailableAgentDetailsMock).toHaveBeenCalledWith(
        expect.objectContaining({ id: "ag_custom" }),
        expect.anything(),
      ),
    );
  });

  it("passes the chosen agent_id when switching agent", async () => {
    forkSessionMock.mockResolvedValue({
      id: "conv_fork",
    } as unknown as Awaited<ReturnType<typeof forkSession>>);
    renderDialog();

    openAgentSelect();
    fireEvent.click(screen.getByTestId("fork-session-agent-option-ag_claude_native"));
    fireEvent.click(screen.getByTestId("fork-session-submit"));

    await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
    // Switching to a same-family native target forwards agent_id so the
    // server clones that agent and marks the fork for native rebuild. The
    // name was left blank (optional) → undefined so the server derives it.
    // Switching reveals the run-config section, but no control was touched, so
    // it emits `{}` — the server inherits/resets per its own family rule
    // rather than the dialog racing the async model catalog and sending an
    // explicit "default" that would clear the source's model.
    expect(forkSessionMock).toHaveBeenCalledWith("conv_src", {
      title: undefined,
      agentId: "ag_claude_native",
      upToResponseId: undefined,
      config: {},
      sandbox: undefined,
    });
  });

  it("emits only the run-config fields the user actually changed", async () => {
    forkSessionMock.mockResolvedValue({
      id: "conv_fork",
    } as unknown as Awaited<ReturnType<typeof forkSession>>);
    renderDialog();

    // Switch to claude-native so the run-config section renders.
    openAgentSelect();
    fireEvent.click(screen.getByTestId("fork-session-agent-option-ag_claude_native"));

    // Touch ONLY the permission control (pick "Plan"); leave model + effort
    // untouched. The fork must carry the permission launch args and NOTHING
    // for model/effort — so the server inherits those instead of the dialog
    // sending a catalog-racing "default" that clears them.
    fireEvent.click(screen.getByTestId("fork-session-config-permission"));
    fireEvent.click(screen.getByRole("option", { name: "Plan" }));
    fireEvent.click(screen.getByTestId("fork-session-submit"));

    await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
    expect(forkSessionMock).toHaveBeenCalledWith("conv_src", {
      title: undefined,
      agentId: "ag_claude_native",
      upToResponseId: undefined,
      config: { terminalLaunchArgs: ["--permission-mode", "plan"] },
      sandbox: undefined,
    });
  });

  it("arms Codex bypass only on an explicit pick, with a danger banner", async () => {
    forkSessionMock.mockResolvedValue({
      id: "conv_fork",
    } as unknown as Awaited<ReturnType<typeof forkSession>>);
    renderDialog();

    // Switch to codex-native so the Approval row (with the 4th bypass option)
    // renders. No banner until the dangerous option is actually chosen.
    openAgentSelect();
    fireEvent.click(screen.getByTestId("fork-session-agent-option-ag_codex_native"));
    expect(screen.queryByTestId("fork-session-codex-bypass-banner")).not.toBeInTheDocument();

    // Pick "Bypass approvals & sandbox" → danger banner appears and the fork
    // carries the dedicated opt-in (as a label server-side), NOT launch args.
    fireEvent.click(screen.getByTestId("fork-session-config-approval"));
    fireEvent.click(screen.getByRole("option", { name: "Bypass approvals & sandbox" }));
    expect(screen.getByTestId("fork-session-codex-bypass-banner")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("fork-session-submit"));

    await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
    expect(forkSessionMock).toHaveBeenCalledWith("conv_src", {
      title: undefined,
      agentId: "ag_codex_native",
      upToResponseId: undefined,
      config: { terminalLaunchArgs: [], codexBypassSandbox: true },
      sandbox: undefined,
    });
  });

  it("labels the keep-current option with the source agent's name, not generic text", () => {
    // The source is bound to the claude-sdk built-in ("Claude"). The
    // keep-current option reads "<agent> (same as original session)" so the
    // user sees exactly which agent they're keeping, not opaque "Same as
    // source". The display name is resolved from the catalog by agent id.
    setAgents(AVAILABLE_AGENTS, "claude-sdk");
    useSessionAgentMock.mockReturnValue({
      data: { id: "ag_claude_sdk", name: "claude", harness: "claude-sdk" },
    } as unknown as ReturnType<typeof useSessionAgent>);
    renderDialog();
    openAgentSelect();

    const sameOption = screen.getByTestId("fork-session-agent-option-same");
    expect(sameOption).toHaveTextContent("Claude");
    expect(sameOption).toHaveTextContent("(same as original session)");
  });

  describe("missing workspace recovery", () => {
    const RECOVERY = {
      sourceTitle: "My session",
      sourceWorkspace: "/Users/a/repo-worktrees/deleted-branch",
      sourceGitBranch: "deleted-branch",
      sourceHostId: "host_1",
      workspaceRecovery: true,
    };

    it.each([
      ["plain directory", "/Users/a/repo", null],
      ["worktree", RECOVERY.sourceWorkspace, RECOVERY.sourceGitBranch],
      ["worktree without a recorded branch", RECOVERY.sourceWorkspace, null],
    ])("requires a directory instead of prefilling the missing %s", (_name, workspace, branch) => {
      renderDialog({ ...RECOVERY, sourceWorkspace: workspace, sourceGitBranch: branch });

      expect(screen.getByRole("heading", { name: "Continue in another directory" })).toBeVisible();
      expect(
        screen.getByText(
          "Choose an existing directory to continue this conversation in a new session, optionally in a new git worktree. Files from the missing workspace won’t be restored.",
        ),
      ).toBeVisible();
      expect(screen.getByTestId("fork-session-host-select")).toHaveTextContent("serena-laptop");
      expect(screen.getByTestId("workspace-path-input")).toBeVisible();
      expect(screen.getByTestId("workspace-path-input")).toHaveValue("");
      // Offered up front, but never prefilled with the missing worktree's branch.
      expect(screen.getByTestId("fork-session-branch-input")).toBeVisible();
      expect(screen.getByTestId("fork-session-branch-input")).toHaveValue("");
      expect(screen.queryByTestId("fork-session-reuse-dir-hint")).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Continue" })).toHaveAttribute(
        "aria-disabled",
        "true",
      );
      expect(forkSessionMock).not.toHaveBeenCalled();
    });

    it("rejects a missing chosen path without recreating its worktree or creating a session", async () => {
      checkHostDirectoryMock.mockResolvedValue("The working directory doesn't exist on this host.");
      hostDirectoryMissingMock.mockResolvedValue(true);
      renderDialog(RECOVERY);

      fireEvent.change(screen.getByTestId("workspace-path-input"), {
        target: { value: RECOVERY.sourceWorkspace },
      });
      fireEvent.click(screen.getByTestId("fork-session-submit"));

      await waitFor(() =>
        expect(screen.getByTestId("fork-session-error")).toHaveTextContent("doesn't exist"),
      );
      expect(checkHostDirectoryMock).toHaveBeenCalledExactlyOnceWith(
        "host_1",
        RECOVERY.sourceWorkspace,
      );
      expect(hostDirectoryMissingMock).not.toHaveBeenCalled();
      expect(forkSessionMock).not.toHaveBeenCalled();
      expect(launchRunnerMock).not.toHaveBeenCalled();
      expect(navigateMock).not.toHaveBeenCalled();
    });

    it("waits for validation, then starts the full conversation in the chosen existing repository", async () => {
      let finishPreflight!: (problem: string | null) => void;
      checkHostDirectoryMock.mockReturnValue(
        new Promise((resolve) => {
          finishPreflight = resolve;
        }),
      );
      forkSessionMock.mockResolvedValue({
        id: "conv_fork",
      } as Awaited<ReturnType<typeof forkSession>>);
      launchRunnerMock.mockResolvedValue({ runnerId: "r1" });
      renderDialog(RECOVERY);

      fireEvent.change(screen.getByTestId("workspace-path-input"), {
        target: { value: "/Users/a/repo" },
      });
      fireEvent.click(screen.getByTestId("fork-session-submit"));

      expect(checkHostDirectoryMock).toHaveBeenCalledWith("host_1", "/Users/a/repo");
      expect(forkSessionMock).not.toHaveBeenCalled();
      expect(launchRunnerMock).not.toHaveBeenCalled();
      finishPreflight(null);

      await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(1));
      expect(forkSessionMock).toHaveBeenCalledWith("conv_src", {
        title: undefined,
        agentId: undefined,
        upToResponseId: undefined,
        config: {},
        sandbox: undefined,
      });
      expect(launchRunnerMock).toHaveBeenCalledWith(
        "host_1",
        "conv_fork",
        "/Users/a/repo",
        undefined,
      );
      expect(navigateMock).toHaveBeenCalledWith("/c/conv_fork");
    });

    it("accepts a folder from the directory browser", async () => {
      forkSessionMock.mockResolvedValue({
        id: "conv_fork",
      } as Awaited<ReturnType<typeof forkSession>>);
      launchRunnerMock.mockResolvedValue({ runnerId: "r1" });
      renderDialog(RECOVERY);

      fireEvent.click(screen.getByTestId("workspace-browse-toggle"));
      fireEvent.click(screen.getByTestId("mock-pick-workspace"));
      expect(screen.getByTestId("workspace-path-input")).toHaveValue("/Users/a/git/omnigent");
      fireEvent.click(screen.getByTestId("fork-session-submit"));

      await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(1));
      expect(launchRunnerMock).toHaveBeenCalledWith(
        "host_1",
        "conv_fork",
        "/Users/a/git/omnigent",
        undefined,
      );
    });

    it("continues in a new git worktree of the chosen repository", async () => {
      forkSessionMock.mockResolvedValue({
        id: "conv_fork",
      } as Awaited<ReturnType<typeof forkSession>>);
      launchRunnerMock.mockResolvedValue({ runnerId: "r1" });
      renderDialog(RECOVERY);

      fireEvent.change(screen.getByTestId("workspace-path-input"), {
        target: { value: "/Users/a/repo" },
      });
      fireEvent.change(screen.getByTestId("fork-session-branch-input"), {
        target: { value: "recovered" },
      });
      fireEvent.click(screen.getByTestId("fork-session-submit"));

      await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(1));
      expect(checkHostDirectoryMock).toHaveBeenCalledExactlyOnceWith("host_1", "/Users/a/repo");
      // Even when the chosen repo is the one the missing worktree came from,
      // the new branch is not based on the missing worktree's branch.
      expect(launchRunnerMock).toHaveBeenCalledWith("host_1", "conv_fork", "/Users/a/repo", {
        branchName: "recovered",
        baseBranch: undefined,
      });
      expect(navigateMock).toHaveBeenCalledWith("/c/conv_fork");
    });

    it("points a directory conflict at the worktree field", () => {
      useDirectorySessionsMock.mockReturnValue({
        data: [{ id: "conv_other", host_id: "host_1", workspace: "/Users/a/repo" }],
      } as unknown as ReturnType<typeof useDirectorySessions>);
      useRunnerHealthMock.mockReturnValue(new Map([["conv_other", true]]));
      renderDialog(RECOVERY);

      fireEvent.change(screen.getByTestId("workspace-path-input"), {
        target: { value: "/Users/a/repo" },
      });
      expect(screen.getByTestId("fork-session-conflict-hint")).toHaveTextContent(
        "1 other agent is working in this directory, so writes may conflict. Name a git branch to work in an isolated copy.",
      );

      fireEvent.change(screen.getByTestId("fork-session-branch-input"), {
        target: { value: "recovered" },
      });
      expect(screen.queryByTestId("fork-session-conflict-hint")).not.toBeInTheDocument();
    });

    it("requires an online host instead of falling back to a fresh sandbox", () => {
      setHosts([host({ status: "offline" })]);
      renderDialog({
        ...RECOVERY,
        info: { managed_sandboxes_enabled: true, sandbox_provider: "arclet" },
      });

      expect(screen.getByTestId("fork-session-submit")).toHaveAttribute("aria-disabled", "true");
      expect(screen.getByTestId("connect-host-command")).toBeVisible();
      expect(screen.queryByTestId("fork-session-sandbox-hint")).not.toBeInTheDocument();
    });

    it("explains that a sandbox-only source can't continue in a host directory", () => {
      useSessionMock.mockReturnValue({
        session: { hostId: "host_1", labels: {} },
        isLoading: false,
        error: null,
      } as unknown as ReturnType<typeof useSession>);
      setHosts([host({ sandbox_provider: "arclet" })]);
      renderDialog({
        ...RECOVERY,
        info: { managed_sandboxes_enabled: true, sandbox_providers: ["arclet"] },
      });

      expect(screen.getByTestId("fork-session-unavailable")).toHaveTextContent(
        "This sandbox session can only be cloned onto a new sandbox.",
      );
      expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
      expect(screen.queryByTestId("fork-session-host-select")).not.toBeInTheDocument();
    });
  });

  describe("coding source (fork + start)", () => {
    const CODING = {
      sourceTitle: "My session",
      sourceWorkspace: "/repo",
      sourceHostId: "host_1",
    };

    it("hides the host/directory fields for a non-coding source", () => {
      // No source workspace → nothing to bind. Advanced still holds the
      // optional name, but there's no host, working dir, or worktree.
      renderDialog({ sourceTitle: "My session" });
      expect(screen.queryByTestId("fork-session-host-select")).not.toBeInTheDocument();
      expect(screen.queryByTestId("fork-session-reuse-dir-hint")).not.toBeInTheDocument();
      openAdvanced();
      expect(screen.getByTestId("fork-session-title-input")).toBeInTheDocument();
      expect(screen.queryByTestId("fork-session-branch-input")).not.toBeInTheDocument();
    });

    it("shows the host + reuse-dir hint up front, keeping the rest behind Advanced", async () => {
      renderDialog(CODING);
      // Host is top-level (offline host = nothing to run on, surfaced early).
      expect(screen.getByTestId("fork-session-host-select")).toBeInTheDocument();
      // The default — reusing the source directory — is announced inline;
      // hovering/focusing "working directory" reveals the full path in a tooltip.
      expect(screen.getByTestId("fork-session-reuse-dir-hint")).toBeInTheDocument();
      fireEvent.focus(screen.getByTestId("fork-session-reuse-dir-path"));
      expect(await screen.findAllByText("/repo")).not.toHaveLength(0);
      // Name, working directory + git worktree are collapsed until expanded.
      expect(screen.queryByTestId("fork-session-title-input")).not.toBeInTheDocument();
      expect(screen.queryByTestId("fork-session-branch-input")).not.toBeInTheDocument();
      openAdvanced();
      expect(screen.getByTestId("fork-session-title-input")).toBeInTheDocument();
      expect(screen.getByTestId("fork-session-branch-input")).toBeInTheDocument();
    });

    it("on a different host (e.g. non-owner), asks for a directory up front", () => {
      // Source ran on a host the caller doesn't have (their useHosts only
      // returns host_1) — the cross-host / non-owner case.
      renderDialog({
        sourceTitle: "My session",
        sourceWorkspace: "/owners/repo",
        sourceHostId: "host_other",
      });
      // Defaults to the caller's own online host, not the source's.
      // A different machine → no "reuses the working directory" hint, and the
      // source path isn't prefilled (it's on someone else's box).
      expect(screen.queryByTestId("fork-session-reuse-dir-hint")).not.toBeInTheDocument();
      // The required directory sits at the top level, outside collapsed Advanced.
      expect(screen.queryByTestId("fork-session-advanced-content")).not.toBeInTheDocument();
      expect(screen.getByTestId("workspace-path-input")).toHaveValue("");
      expect(screen.getByTestId("fork-session-pick-dir-hint")).toBeInTheDocument();
      // Can't start until a directory on this host is chosen.
      const submit = screen.getByTestId("fork-session-submit");
      expect(submit).toHaveAttribute("aria-disabled", "true");

      // A blocked click names the missing field and focuses it.
      fireEvent.click(submit);
      expect(screen.getByTestId("fork-session-field-error")).toHaveTextContent(
        "Pick a working directory.",
      );
      expect(screen.getByTestId("workspace-path-input")).toHaveFocus();
      expect(screen.getByTestId("workspace-path-input")).toHaveAttribute("aria-invalid", "true");
      expect(forkSessionMock).not.toHaveBeenCalled();

      // Fixing the field clears the error.
      fireEvent.change(screen.getByTestId("workspace-path-input"), {
        target: { value: "/mine/repo" },
      });
      expect(screen.queryByTestId("fork-session-field-error")).not.toBeInTheDocument();
      expect(submit).not.toHaveAttribute("aria-disabled");
    });

    it("opens Advanced and focuses a cleared source-host directory on submit", () => {
      renderDialog(CODING);
      openAdvanced();
      fireEvent.change(screen.getByTestId("workspace-path-input"), { target: { value: "" } });
      openAdvanced(); // collapse again: the blocked click must re-reveal it

      fireEvent.click(screen.getByTestId("fork-session-submit"));
      expect(screen.getByTestId("workspace-path-input")).toHaveFocus();
      expect(screen.getByTestId("fork-session-field-error")).toBeInTheDocument();
      expect(forkSessionMock).not.toHaveBeenCalled();
    });

    it("surfaces a directory conflict inline WITHOUT auto-expanding on the source host", () => {
      // A connected session already in the source directory → conflict. Cloning
      // a running session always trips this (the original is still there), so it
      // must NOT force Advanced open — the warning shows at the top instead.
      useDirectorySessionsMock.mockReturnValue({
        data: [{ id: "conv_other", host_id: "host_1", workspace: "/repo" }],
      } as unknown as ReturnType<typeof useDirectorySessions>);
      useRunnerHealthMock.mockReturnValue(new Map([["conv_other", true]]));
      renderDialog(CODING);

      // Warning is visible up front…
      expect(screen.getByTestId("fork-session-conflict-hint")).toBeInTheDocument();
      // …but Advanced stays collapsed (same host → no forced expand).
      expect(screen.queryByTestId("fork-session-advanced-content")).not.toBeInTheDocument();
    });

    it("forks, navigates immediately, and fires the runner launch in the background", async () => {
      forkSessionMock.mockResolvedValue({
        id: "conv_fork",
      } as unknown as Awaited<ReturnType<typeof forkSession>>);
      // Launch never resolves: proves navigation does NOT await it (the old
      // awaited flow blocked the modal here — the freeze report).
      launchRunnerMock.mockReturnValue(new Promise(() => {}));

      const { invalidateSpy } = renderDialog(CODING);

      // Host (source host, online) and directory (source workspace) are
      // prefilled, so the button is enabled without further input.
      fireEvent.click(screen.getByTestId("fork-session-submit"));

      await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
      // Name left blank (optional) → undefined so the server derives it.
      // Coding SDK source, no agent switch → no run-config section, empty config.
      expect(forkSessionMock).toHaveBeenCalledWith("conv_src", {
        title: undefined,
        agentId: undefined,
        upToResponseId: undefined,
        config: {},
        sandbox: undefined,
      });
      // Navigation happens even though the launch promise is still pending.
      await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_fork"));
      // The launch was kicked off (in the background) on the prefilled host/dir.
      expect(launchRunnerMock).toHaveBeenCalledWith("host_1", "conv_fork", "/repo", undefined);
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["conversations"] });
    });

    it("forwards git worktree options when a branch is named", async () => {
      forkSessionMock.mockResolvedValue({
        id: "conv_fork",
      } as unknown as Awaited<ReturnType<typeof forkSession>>);
      launchRunnerMock.mockResolvedValue({ runnerId: "r1" });

      renderDialog({ ...CODING, sourceGitBranch: "main" });

      // The worktree field lives under Advanced (collapsed by default).
      openAdvanced();
      fireEvent.change(screen.getByTestId("fork-session-branch-input"), {
        target: { value: "feature/x" },
      });
      fireEvent.click(screen.getByTestId("fork-session-submit"));

      await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(1));
      // The base ref is sent automatically from the source's branch ("main");
      // the named branch makes the host create an isolated worktree.
      expect(launchRunnerMock).toHaveBeenCalledWith("host_1", "conv_fork", "/repo", {
        branchName: "feature/x",
        baseBranch: "main",
      });
    });

    // Source session that ran in a server-created worktree: its workspace is
    // the worktree dir and gitBranch the branch checked out there.
    const WORKTREE_CODING = {
      sourceTitle: "My session",
      sourceWorkspace: "/Users/a/repo-worktrees/fix-1",
      sourceHostId: "host_1",
      sourceGitBranch: "fix-1",
    };

    it("lets the user clear the prefilled directory without re-seeding it", () => {
      renderDialog(WORKTREE_CODING);
      openAdvanced();
      fireEvent.change(screen.getByTestId("fork-session-branch-input"), {
        target: { value: "edited" },
      });
      fireEvent.change(screen.getByTestId("workspace-path-input"), { target: { value: "" } });
      // Neither the directory nor the edited branch springs back to the source's.
      expect(screen.getByTestId("workspace-path-input")).toHaveValue("");
      expect(screen.getByTestId("fork-session-branch-input")).toHaveValue("edited");
    });

    it("prefills a worktree-backed source as original repo + source branch", () => {
      renderDialog(WORKTREE_CODING);
      openAdvanced();
      // The working directory shows the repo the worktree came from, and the
      // worktree field carries the source branch — not the worktree path as
      // the directory with a blank branch.
      expect(screen.getByTestId("workspace-path-input")).toHaveValue("/Users/a/repo");
      expect(screen.getByTestId("fork-session-branch-input")).toHaveValue("fix-1");
    });

    it("binds the clone to the source's existing worktree when left untouched", async () => {
      forkSessionMock.mockResolvedValue({
        id: "conv_fork",
      } as unknown as Awaited<ReturnType<typeof forkSession>>);
      launchRunnerMock.mockResolvedValue({ runnerId: "r1" });
      renderDialog(WORKTREE_CODING);

      fireEvent.click(screen.getByTestId("fork-session-submit"));

      await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(1));
      // The prefilled branch already exists, so no git options are sent —
      // the clone binds straight to the source's worktree directory. The
      // pre-flight probed that same directory.
      expect(launchRunnerMock).toHaveBeenCalledWith(
        "host_1",
        "conv_fork",
        "/Users/a/repo-worktrees/fix-1",
        undefined,
      );
      expect(checkHostDirectoryMock).toHaveBeenCalledWith(
        "host_1",
        "/Users/a/repo-worktrees/fix-1",
      );
    });

    it("recreates the source worktree when its directory was deleted and the name is untouched", async () => {
      forkSessionMock.mockResolvedValue({
        id: "conv_fork",
      } as unknown as Awaited<ReturnType<typeof forkSession>>);
      launchRunnerMock.mockResolvedValue({ runnerId: "r1" });
      // The worktree pre-flight fails — the directory is gone — but the
      // miss is a 404, so the fork recreates the worktree at the same
      // branch instead of erroring. The repo path itself is intact.
      checkHostDirectoryMock.mockImplementation(async (_hostId: string, path: string) =>
        path === "/Users/a/repo"
          ? null
          : "The working directory /Users/a/repo-worktrees/fix-1 doesn't exist on this host (or isn't a directory).",
      );
      hostDirectoryMissingMock.mockResolvedValue(true);
      renderDialog(WORKTREE_CODING);

      fireEvent.click(screen.getByTestId("fork-session-submit"));

      await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(1));
      // Launched from the REPO path with the prefilled branch (which already
      // exists) — the host adds the worktree back at the conventional path.
      // existingBranch (not baseBranch): the branch survives its deleted
      // directory, so it is checked out rather than re-created.
      expect(launchRunnerMock).toHaveBeenCalledWith("host_1", "conv_fork", "/Users/a/repo", {
        branchName: "fix-1",
        existingBranch: true,
      });
      // Both the worktree path and the repo fallback path were pre-flighted.
      expect(checkHostDirectoryMock).toHaveBeenCalledWith("host_1", "/Users/a/repo");
    });

    it("still errors when the repo path is also missing (nothing to recreate from)", async () => {
      forkSessionMock.mockResolvedValue({
        id: "conv_fork",
      } as unknown as Awaited<ReturnType<typeof forkSession>>);
      // Worktree gone (404 miss) AND the repo itself gone: the recreate
      // fallback has nothing to launch from, so the fork must abort.
      checkHostDirectoryMock.mockResolvedValue(
        "The working directory doesn't exist on this host (or isn't a directory).",
      );
      hostDirectoryMissingMock.mockResolvedValue(true);
      renderDialog(WORKTREE_CODING);

      fireEvent.click(screen.getByTestId("fork-session-submit"));

      await waitFor(() => expect(screen.getByText(/doesn't exist on this host/i)).toBeTruthy());
      expect(launchRunnerMock).not.toHaveBeenCalled();
    });

    it("still errors when the pre-flight fails for a reason other than a missing directory", async () => {
      forkSessionMock.mockResolvedValue({
        id: "conv_fork",
      } as unknown as Awaited<ReturnType<typeof forkSession>>);
      // Host unreachable: NOT a 404 miss, so no worktree-recreate fallback.
      checkHostDirectoryMock.mockResolvedValue(
        "Couldn't verify the working directory. Check your connection and try again.",
      );
      hostDirectoryMissingMock.mockResolvedValue(false);
      renderDialog(WORKTREE_CODING);

      fireEvent.click(screen.getByTestId("fork-session-submit"));

      await waitFor(() =>
        expect(screen.getByText(/Couldn't verify the working directory/i)).toBeTruthy(),
      );
      expect(launchRunnerMock).not.toHaveBeenCalled();
    });

    it("creates a fresh worktree off the source branch when the branch is renamed", async () => {
      forkSessionMock.mockResolvedValue({
        id: "conv_fork",
      } as unknown as Awaited<ReturnType<typeof forkSession>>);
      launchRunnerMock.mockResolvedValue({ runnerId: "r1" });
      renderDialog(WORKTREE_CODING);

      openAdvanced();
      fireEvent.change(screen.getByTestId("fork-session-branch-input"), {
        target: { value: "feature/x" },
      });
      fireEvent.click(screen.getByTestId("fork-session-submit"));

      await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(1));
      // A NEW branch name → worktree created off the original repo, based on
      // the source's branch so the clone starts from where it left off.
      expect(launchRunnerMock).toHaveBeenCalledWith("host_1", "conv_fork", "/Users/a/repo", {
        branchName: "feature/x",
        baseBranch: "fix-1",
      });
    });

    it("recognizes a worktree source without gitBranch (fork-of-fork) via the path", async () => {
      // A fork bound into an existing worktree carries NO git_branch (the
      // bind sends no git options), so forking the fork must recover both
      // the repo and the branch from the worktree path convention — the
      // branch falls back to the worktree's directory name.
      forkSessionMock.mockResolvedValue({
        id: "conv_fork",
      } as unknown as Awaited<ReturnType<typeof forkSession>>);
      launchRunnerMock.mockResolvedValue({ runnerId: "r1" });
      renderDialog({ ...WORKTREE_CODING, sourceGitBranch: null });

      openAdvanced();
      expect(screen.getByTestId("workspace-path-input")).toHaveValue("/Users/a/repo");
      expect(screen.getByTestId("fork-session-branch-input")).toHaveValue("fix-1");

      fireEvent.click(screen.getByTestId("fork-session-submit"));

      await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(1));
      // Untouched → same aliasing as a gitBranch-carrying source: bind the
      // exact source worktree directory with no git options.
      expect(launchRunnerMock).toHaveBeenCalledWith(
        "host_1",
        "conv_fork",
        "/Users/a/repo-worktrees/fix-1",
        undefined,
      );
    });

    it("enables the submit for a typed tilde path without opening the browser", async () => {
      // The reported journey: type "~/git/omnigent" into the field and stop —
      // no Enter, no browsing. A tilde path reads like a real directory but
      // the server never expands ~, so the submit used to stay greyed unless
      // the user discovered the tree browser. The dialog now resolves ~
      // against the host's home (from the home listing) so the typed path is
      // directly submittable.
      forkSessionMock.mockResolvedValue({
        id: "conv_fork",
      } as unknown as Awaited<ReturnType<typeof forkSession>>);
      launchRunnerMock.mockResolvedValue({ runnerId: "r1" });
      // Home listing: the first entry's parent (/Users/a) resolves the host's
      // home, so "~/git/omnigent" expands to "/Users/a/git/omnigent".
      useHostFilesystemMock.mockReturnValue({
        data: { entries: [{ name: "git", path: "/Users/a/git", type: "directory" }] },
        isPlaceholderData: false,
        isLoading: false,
        error: null,
      } as unknown as ReturnType<typeof useHostFilesystem>);
      renderDialog(CODING);

      openAdvanced();
      const input = screen.getByTestId("workspace-path-input");
      fireEvent.change(input, { target: { value: "~/git/omnigent" } });

      // No Enter, no browser — the submit enables purely from the ~-resolve.
      expect(screen.queryByTestId("mock-workspace-picker")).not.toBeInTheDocument();
      await waitFor(() =>
        expect(screen.getByTestId("fork-session-submit")).not.toHaveAttribute("aria-disabled"),
      );

      fireEvent.click(screen.getByTestId("fork-session-submit"));
      await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(1));
      // Launched with the resolved absolute path, not the raw tilde.
      expect(launchRunnerMock).toHaveBeenCalledWith(
        "host_1",
        "conv_fork",
        "/Users/a/git/omnigent",
        undefined,
      );
    });

    it("adopts an absolute path picked from the tree browser", async () => {
      // The browse route: open the tree browser and click "Select". The
      // picker commits an absolute path (onSelect), which enables the submit
      // and launches on it. (Typed ~-paths resolve directly, tested above —
      // this covers the still-live browser path.)
      forkSessionMock.mockResolvedValue({
        id: "conv_fork",
      } as unknown as Awaited<ReturnType<typeof forkSession>>);
      launchRunnerMock.mockResolvedValue({ runnerId: "r1" });
      renderDialog(CODING);

      openAdvanced();
      const input = screen.getByTestId("workspace-path-input");
      // A tilde value alone is not submittable (the server never expands ~).
      fireEvent.change(input, { target: { value: "~/git/omnigent" } });
      expect(screen.getByTestId("fork-session-submit")).toHaveAttribute("aria-disabled", "true");

      // Enter commits the typed path and opens the tree browser at it.
      fireEvent.keyDown(input, { key: "Enter" });
      expect(screen.getByTestId("mock-workspace-picker")).toBeInTheDocument();

      // "Select" commits the browser's absolute path into the form.
      fireEvent.click(screen.getByTestId("mock-pick-workspace"));
      expect(screen.getByTestId("workspace-path-input")).toHaveValue("/Users/a/git/omnigent");
      expect(screen.getByTestId("fork-session-submit")).not.toHaveAttribute("aria-disabled");

      fireEvent.click(screen.getByTestId("fork-session-submit"));
      await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(1));
      expect(launchRunnerMock).toHaveBeenCalledWith(
        "host_1",
        "conv_fork",
        "/Users/a/git/omnigent",
        undefined,
      );
    });

    it("refuses to create the fork when the directory doesn't exist", async () => {
      checkHostDirectoryMock.mockResolvedValue(
        "The working directory /repo doesn't exist on this host.",
      );
      renderDialog(CODING);

      fireEvent.click(screen.getByTestId("fork-session-submit"));

      await waitFor(() =>
        expect(screen.getByTestId("fork-session-error")).toHaveTextContent("doesn't exist"),
      );
      // Nothing was created: no fork, no launch, no navigation — the inputs
      // stay editable so the user can fix the path and resubmit.
      expect(forkSessionMock).not.toHaveBeenCalled();
      expect(launchRunnerMock).not.toHaveBeenCalled();
      expect(navigateMock).not.toHaveBeenCalled();
    });

    it("disables the fork button and shows connect-host instructions when no host is online", () => {
      setHosts([host({ status: "offline" })]);
      renderDialog(CODING);

      // Can't start with nothing to run on, and (mirroring NewChatDialog) the
      // CLI reconnect command is surfaced so the user can unblock.
      expect(screen.getByTestId("fork-session-submit")).toHaveAttribute("aria-disabled", "true");
      expect(screen.queryByTestId("fork-session-host-select")).not.toBeInTheDocument();
      expect(screen.getByTestId("connect-host-command")).toBeInTheDocument();
    });

    it("greys submit when the selected host goes offline on a later refetch", () => {
      // Source host is online and auto-selected on mount → submit enabled.
      setHosts([host({ host_id: "host_1", status: "online" })]);
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      // Fresh element each call so rerender doesn't bail on referential equality.
      const tree = () => (
        <QueryClientProvider client={client}>
          <TooltipProvider>
            <MemoryRouter>
              <ForkSessionDialog
                sourceSessionId="conv_src"
                sourceTitle="My session"
                sourceWorkspace="/repo"
                sourceHostId="host_1"
                open
                onOpenChange={vi.fn()}
              />
            </MemoryRouter>
          </TooltipProvider>
        </QueryClientProvider>
      );
      const { rerender } = render(tree());
      expect(screen.getByTestId("fork-session-submit")).not.toHaveAttribute("aria-disabled");

      // useHosts refetches and the still-selected host flips offline. canSubmit
      // checks online-ness (not just "a host is selected"), so submit re-greys
      // rather than attempting a launchRunner that would fail server-side.
      setHosts([host({ host_id: "host_1", status: "offline" })]);
      rerender(tree());
      expect(screen.getByTestId("fork-session-submit")).toHaveAttribute("aria-disabled", "true");
    });

    it("clears the worktree branch when the host changes (no stale source branch)", () => {
      // Two online hosts; source ran on host_1 with branch "main" (prefills
      // the base ref). Switching to host_2 must reset the worktree fields so a
      // base ref from the source machine can't launch a worktree on another.
      setHosts([host({ host_id: "host_1" }), host({ host_id: "host_2", name: "other-laptop" })]);
      renderDialog({ ...CODING, sourceGitBranch: "main" });

      openAdvanced();
      fireEvent.change(screen.getByTestId("fork-session-branch-input"), {
        target: { value: "feature/x" },
      });
      expect(screen.getByTestId("fork-session-branch-input")).toHaveValue("feature/x");

      // Switch host via the Radix Select (mirrors openAgentSelect's gesture).
      const trigger = screen.getByTestId("fork-session-host-select");
      fireEvent.pointerDown(trigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
      fireEvent.click(trigger);
      fireEvent.click(screen.getByTestId("fork-session-host-option-host_2"));

      expect(screen.getByTestId("fork-session-branch-input")).toHaveValue("");
    });

    it("still navigates when the background launch rejects (failure doesn't block)", async () => {
      forkSessionMock.mockResolvedValue({
        id: "conv_fork",
      } as unknown as Awaited<ReturnType<typeof forkSession>>);
      // The launch fails in the background; the dialog must NOT surface it
      // (it has closed + navigated) — recovery is the session page's picker.
      launchRunnerMock.mockRejectedValue(new Error("host busy"));
      renderDialog(CODING);

      fireEvent.click(screen.getByTestId("fork-session-submit"));

      // We navigated into the clone regardless of the launch outcome.
      await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_fork"));
      // Forked exactly once; the dialog showed no inline error (it handed off).
      expect(forkSessionMock).toHaveBeenCalledTimes(1);
      expect(screen.queryByTestId("fork-session-error")).not.toBeInTheDocument();
    });

    it("selects a machine the desktop app connects over SSH", async () => {
      setHosts([host({ host_id: "host_1" }), host({ host_id: "host_2", name: "devbox" })]);
      const desktop = {
        kind: "electron",
        connectSshHost: vi.fn().mockResolvedValue({
          ok: true,
          identity: { serverUrl: "https://example.com", hostId: "host_2" },
          shownInConsole: true,
        }),
      };
      (window as { omnigentDesktop?: unknown }).omnigentDesktop = desktop;
      try {
        renderDialog(CODING);
        fireEvent.click(screen.getByTestId("fork-session-connect-host-toggle"));
        fireEvent.change(await screen.findByTestId("ssh-connect-destination"), {
          target: { value: "devbox" },
        });
        fireEvent.click(screen.getByTestId("ssh-connect-submit"));

        await waitFor(() =>
          expect(screen.getByTestId("fork-session-host-select")).toHaveTextContent("devbox"),
        );
        expect(desktop.connectSshHost).toHaveBeenCalledWith("devbox");
        expect(screen.queryByTestId("ssh-connect-form")).not.toBeInTheDocument();
      } finally {
        delete (window as { omnigentDesktop?: unknown }).omnigentDesktop;
      }
    });

    it("reveals connect-host instructions via the collapsible toggle when a host is online", () => {
      // Mirrors NewChatDialog: with a host online the picker shows, and the
      // CLI command for connecting ANOTHER host hides behind a toggle.
      renderDialog(CODING);

      expect(screen.queryByTestId("connect-host-command")).not.toBeInTheDocument();
      fireEvent.click(screen.getByTestId("fork-session-connect-host-toggle"));
      expect(screen.getByTestId("connect-host-command")).toBeInTheDocument();
    });
  });

  describe("managed sandbox target", () => {
    const CODING = {
      sourceTitle: "My session",
      sourceWorkspace: "/repo",
      sourceHostId: "host_1",
    };

    /** Pick the (first) sandbox row in the host <Select>. */
    function selectSandbox(): void {
      openHostSelect();
      fireEvent.click(screen.getByTestId("fork-session-sandbox-option"));
    }

    it("hides the sandbox option on a server that can't provision one", () => {
      // Fails closed, mirroring the new-session picker: a server with no
      // launch-capable sandbox config must not offer a target it will reject.
      renderDialog({ ...CODING, info: { managed_sandboxes_enabled: false } });

      openHostSelect();
      expect(screen.queryByTestId("fork-session-sandbox-option")).not.toBeInTheDocument();
    });

    it("offers a sandbox even when no host is online, labelled per provider", () => {
      // The reported gap: with every host offline the picker used to collapse
      // into "reconnect from your terminal", so a clone could never reach a
      // managed sandbox from the web UI at all.
      setHosts([host({ status: "offline" })]);
      renderDialog({
        ...CODING,
        info: { managed_sandboxes_enabled: true, sandbox_provider: "modal" },
      });

      openHostSelect();
      expect(screen.getByTestId("fork-session-sandbox-option")).toHaveTextContent("Modal Sandbox");
      // No dead-end: a sandbox is a usable target, so connecting a host is a
      // collapsed, optional step rather than an always-shown "no hosts
      // connected" banner.
      expect(screen.queryByTestId("connect-host-command")).not.toBeInTheDocument();
      expect(screen.getByTestId("fork-session-connect-host-toggle")).toBeInTheDocument();
    });

    it("defaults to the sandbox when no host is online (sandbox-only deployment)", () => {
      // With no host to reproduce the source on, the sandbox is the only usable
      // target: select it by default so the clone is submittable, rather than
      // stranding the picker empty behind an explicit pick.
      setHosts([host({ status: "offline" })]);
      renderDialog({
        ...CODING,
        info: { managed_sandboxes_enabled: true, sandbox_provider: "modal" },
      });

      // The sandbox chrome (repository fields) is active without a manual pick,
      // and there is no host-directory reuse hint to reproduce.
      expect(screen.getByTestId("fork-session-sandbox-hint")).toBeInTheDocument();
      expect(screen.queryByTestId("fork-session-reuse-dir-hint")).not.toBeInTheDocument();
    });

    it("keeps a connected host as the default — a sandbox is never implicit while one is online", () => {
      // Provisioning costs real compute, so with a host online cloning defaults
      // to reproducing the source. Only an explicit pick spends.
      renderDialog({ ...CODING, info: { managed_sandboxes_enabled: true } });

      expect(screen.queryByTestId("fork-session-sandbox-hint")).not.toBeInTheDocument();
      expect(screen.getByTestId("fork-session-reuse-dir-hint")).toBeInTheDocument();
    });

    it("swaps the directory chrome for repository fields when the sandbox is picked", () => {
      renderDialog({ ...CODING, info: { managed_sandboxes_enabled: true } });

      selectSandbox();

      // Host-directory chrome is gone: neither the reuse hint nor (under
      // Advanced) the working-directory / worktree fields apply to a sandbox
      // whose filesystem doesn't exist yet.
      expect(screen.queryByTestId("fork-session-reuse-dir-hint")).not.toBeInTheDocument();
      expect(screen.getByTestId("fork-session-sandbox-hint")).toBeInTheDocument();
      openAdvanced();
      expect(screen.queryByTestId("fork-session-branch-input")).not.toBeInTheDocument();
      expect(screen.getByTestId("fork-session-sandbox-repo-input")).toBeInTheDocument();
      expect(screen.getByTestId("fork-session-sandbox-repo-add")).toBeInTheDocument();
    });

    it("prefills the repository from a sandbox source and forks onto a new sandbox", async () => {
      // Cloning a sandbox session should land in the same checkout, so the
      // repository the source recorded seeds the fields.
      useSessionMock.mockReturnValue({
        session: {
          labels: { [SANDBOX_REPO_LABEL_KEY]: "https://github.com/org/repo#release-1.2" },
        },
        isLoading: false,
        error: null,
      } as unknown as ReturnType<typeof useSession>);
      forkSessionMock.mockResolvedValue({
        id: "conv_fork",
      } as unknown as Awaited<ReturnType<typeof forkSession>>);
      renderDialog({
        ...CODING,
        info: { managed_sandboxes_enabled: true, sandbox_provider: "modal" },
      });

      selectSandbox();
      await act(async () => {});
      expect(screen.queryByTestId("fork-session-advanced-content")).not.toBeInTheDocument();
      expect(screen.getByTestId("fork-session-sandbox-repo-row")).toHaveTextContent("repo");
      expect(screen.getByRole("textbox", { name: "Branch for repo" })).toHaveValue("release-1.2");

      fireEvent.click(screen.getByTestId("fork-session-submit"));

      await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
      expect(forkSessionMock).toHaveBeenCalledWith("conv_src", {
        title: undefined,
        agentId: undefined,
        upToResponseId: undefined,
        config: {},
        sandbox: { provider: "modal", workspaces: ["https://github.com/org/repo#release-1.2"] },
      });
      // The server provisions the host, so the dialog must NOT also try to
      // bind one — a launchRunner here would 404 on a host that doesn't exist.
      expect(launchRunnerMock).not.toHaveBeenCalled();
      expect(checkHostDirectoryMock).not.toHaveBeenCalled();
      await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_fork"));
    });

    it("offers an Arclet source only a fresh Arclet and forks onto it", async () => {
      useSessionMock.mockReturnValue({
        session: {
          hostId: "host_1",
          labels: {
            "omnigent.host_type": "managed",
            [SANDBOX_REPO_LABEL_KEY]: "https://github.com/databricks-eng/universe",
          },
        },
        isLoading: false,
        error: null,
      } as unknown as ReturnType<typeof useSession>);
      // Both the source Arclet and another machine are online; neither is a target.
      setHosts([host({ sandbox_provider: "arclet" }), host({ host_id: "host_2", name: "laptop" })]);
      forkSessionMock.mockResolvedValue({
        id: "conv_fork",
      } as unknown as Awaited<ReturnType<typeof forkSession>>);
      renderDialog({
        ...CODING,
        info: { managed_sandboxes_enabled: true, sandbox_providers: ["lakebox", "arclet"] },
      });

      await act(async () => {});
      expect(screen.getByTestId("fork-session-sandbox-only-hint")).toHaveTextContent(
        "This session can only be cloned onto a new Arclet Sandbox.",
      );
      expect(screen.getByTestId("fork-session-sandbox-hint")).toBeInTheDocument();
      expect(screen.queryByTestId("fork-session-connect-host-toggle")).not.toBeInTheDocument();
      // The only target is preselected, so the default state is submittable.
      expect(screen.getByTestId("fork-session-host-select")).toHaveTextContent("Arclet Sandbox");
      openHostSelect();
      expect(screen.getAllByRole("option")).toHaveLength(1);
      expect(screen.getByTestId("fork-session-sandbox-option")).toHaveTextContent("Arclet Sandbox");
      fireEvent.click(screen.getByTestId("fork-session-sandbox-option"));
      expect(screen.getByTestId("fork-session-sandbox-repo-row")).toHaveTextContent("universe");

      fireEvent.click(screen.getByTestId("fork-session-submit"));

      await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
      expect(forkSessionMock).toHaveBeenCalledWith("conv_src", {
        title: undefined,
        agentId: undefined,
        upToResponseId: undefined,
        config: {},
        sandbox: { provider: "arclet", workspaces: ["https://github.com/databricks-eng/universe"] },
      });
      expect(launchRunnerMock).not.toHaveBeenCalled();
      expect(checkHostDirectoryMock).not.toHaveBeenCalled();
      await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_fork"));
    });

    it.each([true, false])(
      "chooses the sandbox host's id only on a server that accepts it (%s)",
      async (accepts) => {
        forkSessionMock.mockResolvedValue({
          id: "conv_fork",
        } as unknown as Awaited<ReturnType<typeof forkSession>>);
        renderDialog({
          ...CODING,
          info: {
            managed_sandboxes_enabled: true,
            sandbox_provider: "modal",
            request_managed_sandbox_host_id: accepts,
          },
        });

        selectSandbox();
        fireEvent.click(screen.getByTestId("fork-session-submit"));

        await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
        const sandbox = forkSessionMock.mock.calls[0][1]?.sandbox;
        if (accepts) {
          expect(sandbox?.hostId).toMatch(/^[0-9a-f]{32}$/);
        } else {
          expect(sandbox !== undefined && "hostId" in sandbox).toBe(false);
        }
      },
    );

    it.each([true, false, undefined])(
      "inherits ALL repositories with workspace-list capability %s",
      async (acceptsWorkspaces) => {
        // Legacy inheritance must remain usable even if branch discovery is available.
        useSessionMock.mockReturnValue({
          session: {
            labels: {
              [SANDBOX_REPO_LABEL_KEY]:
                "https://github.com/org/api#main https://github.com/org/web",
            },
          },
          isLoading: false,
          error: null,
        } as unknown as ReturnType<typeof useSession>);
        fetchSessionRepositoryBranchesMock.mockResolvedValue({
          repositories: [
            { url: "https://github.com/org/api", branch: "feature/api", pushed: true },
            { url: "https://github.com/org/web", branch: "feature/web", pushed: true },
          ],
        });
        forkSessionMock.mockResolvedValue({
          id: "conv_fork",
        } as unknown as Awaited<ReturnType<typeof forkSession>>);
        // A multi-repo source can only inherit all repos onto a multi-repo dest.
        renderDialog({
          ...CODING,
          info: {
            managed_sandboxes_enabled: true,
            sandbox_provider: "agent_sandbox",
            sandbox_providers: ["agent_sandbox"],
            sandbox_provider_capabilities: { agent_sandbox: { multi_repo: true } },
            session_fork_workspaces: acceptsWorkspaces,
          },
        });

        selectSandbox();
        await act(async () => {});
        expect(screen.queryByTestId("fork-session-advanced-content")).not.toBeInTheDocument();
        expect(screen.getAllByTestId("fork-session-sandbox-repo-row")).toHaveLength(2);
        expect(screen.getByRole("textbox", { name: "Branch for api" })).toHaveValue(
          acceptsWorkspaces ? "feature/api" : "main",
        );
        expect(screen.getByRole("textbox", { name: "Branch for web" })).toHaveValue(
          acceptsWorkspaces ? "feature/web" : "",
        );

        // The space-joined label no longer greys the button on a multi-repo dest.
        const submit = screen.getByTestId("fork-session-submit") as HTMLButtonElement;
        expect(submit.disabled).toBe(false);
        fireEvent.click(submit);

        await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
        const call = forkSessionMock.mock.calls[0][1];
        expect(call?.sandbox?.provider).toBe("agent_sandbox");
        if (acceptsWorkspaces) {
          expect(call?.sandbox?.workspaces).toEqual([
            "https://github.com/org/api#feature/api",
            "https://github.com/org/web#feature/web",
          ]);
        } else {
          expect(fetchSessionRepositoryBranchesMock).not.toHaveBeenCalled();
          expect(call?.sandbox).toEqual({ provider: "agent_sandbox" });
        }
      },
    );

    it("blocks multi-repo edits on an older server while allowing restoration or a single repository", async () => {
      useSessionMock.mockReturnValue({
        session: {
          labels: {
            [SANDBOX_REPO_LABEL_KEY]: "https://github.com/org/api#main https://github.com/org/web",
          },
        },
        isLoading: false,
        error: null,
      } as unknown as ReturnType<typeof useSession>);
      forkSessionMock.mockResolvedValue({ id: "conv_fork" } as ForkResponse);
      renderDialog({
        ...CODING,
        info: {
          managed_sandboxes_enabled: true,
          sandbox_provider: "agent_sandbox",
          sandbox_provider_capabilities: { agent_sandbox: { multi_repo: true } },
          session_fork_workspaces: undefined,
        },
      });
      selectSandbox();
      await act(async () => {});
      const submit = screen.getByTestId("fork-session-submit");
      const branch = screen.getByRole("textbox", { name: "Branch for api" });
      fireEvent.change(branch, { target: { value: "feature/api" } });
      expect(submit).toHaveAttribute("aria-disabled", "true");
      expect(screen.getByTestId("fork-session-field-error")).toHaveTextContent(
        "Upgrade the server to change multiple repositories.",
      );
      fireEvent.click(submit);
      expect(forkSessionMock).not.toHaveBeenCalled();
      fireEvent.change(branch, { target: { value: "main" } });
      expect(submit).not.toHaveAttribute("aria-disabled");
      expect(screen.queryByTestId("fork-session-field-error")).not.toBeInTheDocument();
      fireEvent.change(screen.getByTestId("fork-session-sandbox-repo-input"), {
        target: { value: "https://github.com/org/tools" },
      });
      fireEvent.click(screen.getByTestId("fork-session-sandbox-repo-add"));
      expect(submit).toHaveAttribute("aria-disabled", "true");
      fireEvent.click(screen.getByRole("button", { name: "Remove tools" }));
      expect(submit).not.toHaveAttribute("aria-disabled");
      fireEvent.click(screen.getByRole("button", { name: "Remove web" }));
      expect(submit).not.toHaveAttribute("aria-disabled");
      fireEvent.click(submit);
      await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
      expect(forkSessionMock.mock.calls[0][1]?.sandbox).toEqual({
        provider: "agent_sandbox",
        workspace: "https://github.com/org/api#main",
      });
    });

    it.each([
      { branch: "feature#draft", pushed: true, original: "#main", expected: "main", note: null },
      { branch: "b".repeat(256), pushed: true, original: "#main", expected: "main", note: null },
      {
        branch: "feature/api",
        pushed: true,
        original: "#main",
        expected: "feature/api",
        note: null,
      },
      {
        branch: "wip/api",
        pushed: false,
        original: "#main",
        expected: "main",
        note: "“wip/api” isn’t pushed. Using main.",
      },
      {
        branch: "wip/api",
        pushed: false,
        original: "",
        expected: "",
        note: "“wip/api” isn’t pushed. Using the default branch.",
      },
      { branch: "feature/api", pushed: null, original: "#main", expected: "main", note: null },
      { branch: null, pushed: null, original: "#main", expected: "main", note: null },
    ])(
      "prefills cloneable branches and falls back quietly: $branch / $pushed / $original",
      async ({ branch, pushed, original, expected, note }) => {
        useSessionMock.mockReturnValue({
          session: {
            labels: { [SANDBOX_REPO_LABEL_KEY]: `https://github.com/org/api${original}` },
          },
          isLoading: false,
          error: null,
        } as unknown as ReturnType<typeof useSession>);
        fetchSessionRepositoryBranchesMock.mockResolvedValue({
          repositories: [{ url: "https://github.com/org/api", branch, pushed }],
        });
        forkSessionMock.mockResolvedValue({ id: "conv_fork" } as ForkResponse);
        renderDialog({
          ...CODING,
          info: { managed_sandboxes_enabled: true, sandbox_provider: "modal" },
        });
        selectSandbox();
        await act(async () => {});
        await act(async () => {});
        const input = screen.getByRole("textbox", { name: "Branch for api" });
        expect(input).toHaveValue(expected);
        if (note) {
          expect(input).toHaveAccessibleDescription(note);
          expect(screen.getByText(note)).not.toHaveAttribute("role", "alert");
        } else {
          expect(screen.queryByText(/isn’t pushed/)).not.toBeInTheDocument();
        }
        fireEvent.click(screen.getByTestId("fork-session-submit"));
        await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
        expect(forkSessionMock.mock.calls[0][1]?.sandbox?.workspaces).toEqual([
          `https://github.com/org/api${expected ? `#${expected}` : ""}`,
        ]);
      },
    );

    it("preserves edited and removed/re-added repositories when branch discovery is late", async () => {
      useSessionMock.mockReturnValue({
        session: {
          labels: {
            [SANDBOX_REPO_LABEL_KEY]:
              "https://github.com/org/api#main https://github.com/org/web#main https://github.com/org/tools#main",
          },
        },
        isLoading: false,
        error: null,
      } as unknown as ReturnType<typeof useSession>);
      let resolveBranches!: (
        result: Awaited<ReturnType<typeof fetchSessionRepositoryBranches>>,
      ) => void;
      fetchSessionRepositoryBranchesMock.mockReturnValue(
        new Promise((resolve) => {
          resolveBranches = resolve;
        }),
      );
      renderDialog({
        ...CODING,
        info: {
          managed_sandboxes_enabled: true,
          sandbox_provider: "agent_sandbox",
          sandbox_provider_capabilities: { agent_sandbox: { multi_repo: true } },
        },
      });
      selectSandbox();
      expect(screen.getByTestId("fork-session-submit")).toBeDisabled();
      expect(screen.getByText("Checking current branches…")).toBeInTheDocument();
      fireEvent.change(screen.getByRole("textbox", { name: "Branch for api" }), {
        target: { value: "my-choice" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Remove web" }));
      fireEvent.change(screen.getByTestId("fork-session-sandbox-repo-input"), {
        target: { value: "https://github.com/org/web" },
      });
      fireEvent.click(screen.getByTestId("fork-session-sandbox-repo-add"));
      await act(async () =>
        resolveBranches({
          repositories: [
            { url: "https://github.com/org/api", branch: "wip/api", pushed: false },
            { url: "https://github.com/org/web", branch: "feature/web", pushed: true },
            { url: "https://github.com/org/tools", branch: "feature/tools", pushed: true },
          ],
        }),
      );
      expect(screen.getByRole("textbox", { name: "Branch for api" })).toHaveValue("my-choice");
      expect(screen.getByRole("textbox", { name: "Branch for web" })).toHaveValue("");
      expect(screen.getByRole("textbox", { name: "Branch for tools" })).toHaveValue(
        "feature/tools",
      );
      expect(screen.getByTestId("fork-session-submit")).not.toBeDisabled();
      expect(screen.queryByText(/isn’t pushed/)).not.toBeInTheDocument();
    });

    it.each(["Source offline", "404 Not Found"])(
      "keeps the original branch on an older server when discovery fails: %s",
      async (error) => {
        useSessionMock.mockReturnValue({
          session: { labels: { [SANDBOX_REPO_LABEL_KEY]: "https://github.com/org/api#main" } },
          isLoading: false,
          error: null,
        } as unknown as ReturnType<typeof useSession>);
        fetchSessionRepositoryBranchesMock.mockRejectedValue(new Error(error));
        forkSessionMock.mockResolvedValue({ id: "conv_fork" } as ForkResponse);
        renderDialog({
          ...CODING,
          info: {
            managed_sandboxes_enabled: true,
            sandbox_provider: "modal",
            session_fork_workspaces: undefined,
          },
        });
        selectSandbox();
        await act(async () => {});
        expect(screen.getByRole("textbox", { name: "Branch for api" })).toHaveValue("main");
        expect(
          screen.queryByText(/isn’t pushed|Source offline|404 Not Found/),
        ).not.toBeInTheDocument();
        fireEvent.click(screen.getByTestId("fork-session-submit"));
        await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
        expect(forkSessionMock.mock.calls[0][1]?.sandbox).toEqual({
          provider: "modal",
          workspace: "https://github.com/org/api#main",
        });
      },
    );

    it("preserves distinct branches when source repositories or branch results have duplicate URLs", async () => {
      useSessionMock.mockReturnValue({
        session: {
          labels: {
            [SANDBOX_REPO_LABEL_KEY]:
              "https://github.com/org/api#main https://github.com/org/api#stable https://github.com/org/web#release",
          },
        },
        isLoading: false,
        error: null,
      } as unknown as ReturnType<typeof useSession>);
      fetchSessionRepositoryBranchesMock.mockResolvedValue({
        repositories: [
          { url: "https://github.com/org/api", branch: "feature/api", pushed: true },
          { url: "https://github.com/org/api", branch: "wip/api", pushed: false },
          { url: "https://github.com/org/web", branch: "feature/web", pushed: true },
          { url: "https://github.com/org/web", branch: "other/web", pushed: true },
        ],
      });
      forkSessionMock.mockResolvedValue({ id: "conv_fork" } as ForkResponse);
      renderDialog({
        ...CODING,
        info: {
          managed_sandboxes_enabled: true,
          sandbox_provider: "agent_sandbox",
          sandbox_provider_capabilities: { agent_sandbox: { multi_repo: true } },
        },
      });
      selectSandbox();
      await act(async () => {});
      expect(
        screen
          .getAllByRole("textbox", { name: "Branch for api" })
          .map((input) => (input as HTMLInputElement).value),
      ).toEqual(["main", "stable"]);
      expect(screen.getByRole("textbox", { name: "Branch for web" })).toHaveValue("release");
      expect(screen.queryByText(/isn’t pushed/)).not.toBeInTheDocument();
      fireEvent.click(screen.getByTestId("fork-session-submit"));
      await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
      expect(forkSessionMock.mock.calls[0][1]?.sandbox?.workspaces).toEqual([
        "https://github.com/org/api#main",
        "https://github.com/org/api#stable",
        "https://github.com/org/web#release",
      ]);
    });

    it("releases Clone after a slow branch lookup and ignores its late result", async () => {
      vi.useFakeTimers();
      try {
        useSessionMock.mockReturnValue({
          session: { labels: { [SANDBOX_REPO_LABEL_KEY]: "https://github.com/org/api#main" } },
          isLoading: false,
          error: null,
        } as unknown as ReturnType<typeof useSession>);
        let resolveBranches!: (
          result: Awaited<ReturnType<typeof fetchSessionRepositoryBranches>>,
        ) => void;
        fetchSessionRepositoryBranchesMock.mockReturnValue(
          new Promise((resolve) => {
            resolveBranches = resolve;
          }),
        );
        renderDialog({
          ...CODING,
          info: { managed_sandboxes_enabled: true, sandbox_provider: "modal" },
        });
        selectSandbox();
        expect(screen.getByTestId("fork-session-submit")).toBeDisabled();
        await act(async () => {
          await vi.advanceTimersByTimeAsync(8_000);
        });
        expect(screen.getByTestId("fork-session-submit")).not.toBeDisabled();
        expect(fetchSessionRepositoryBranchesMock.mock.calls[0][1]?.aborted).toBe(true);
        await act(async () =>
          resolveBranches({
            repositories: [
              { url: "https://github.com/org/api", branch: "feature/late", pushed: true },
            ],
          }),
        );
        expect(screen.getByRole("textbox", { name: "Branch for api" })).toHaveValue("main");
      } finally {
        vi.useRealTimers();
      }
    });

    it("edits inherited branches, replaces a repository, and preserves the fork point", async () => {
      const sourceLabels = {
        [SANDBOX_REPO_LABEL_KEY]:
          "https://github.com/org/api#main https://github.com/org/web#stable",
      };
      useSessionMock.mockReturnValue({
        session: { labels: sourceLabels },
        isLoading: false,
        error: null,
      } as unknown as ReturnType<typeof useSession>);
      forkSessionMock.mockResolvedValue({ id: "conv_fork" } as ForkResponse);
      renderDialog({
        ...CODING,
        upToResponseId: "resp_selected",
        info: {
          managed_sandboxes_enabled: true,
          sandbox_provider: "agent_sandbox",
          sandbox_provider_capabilities: { agent_sandbox: { multi_repo: true } },
        },
      });

      selectSandbox();
      await act(async () => {});
      fireEvent.change(screen.getByRole("textbox", { name: "Branch for api" }), {
        target: { value: "invalid branch" },
      });
      fireEvent.click(screen.getByTestId("fork-session-submit"));
      expect(forkSessionMock).not.toHaveBeenCalled();
      expect(screen.getByTestId("fork-session-field-error")).toHaveTextContent(
        "Enter a valid branch name",
      );
      const branchInput = screen.getByRole("textbox", { name: "Branch for api" });
      expect(branchInput).toHaveFocus();
      const pendingUrl = screen.getByTestId("fork-session-sandbox-repo-input");
      fireEvent.change(pendingUrl, { target: { value: "https://github.com/org/tools" } });
      fireEvent.click(screen.getByTestId("fork-session-submit"));
      expect(pendingUrl).toHaveFocus();
      expect(screen.getByTestId("fork-session-field-error")).toHaveTextContent(
        "Finish or clear the repository URL",
      );
      fireEvent.change(pendingUrl, { target: { value: "" } });
      fireEvent.change(branchInput, { target: { value: "b".repeat(256) } });
      fireEvent.click(screen.getByTestId("fork-session-submit"));
      expect(forkSessionMock).not.toHaveBeenCalled();
      expect(branchInput).toHaveFocus();
      expect(branchInput).toHaveAccessibleDescription(
        "Repository URL and branch must be at most 256 characters together.",
      );
      expect(screen.getByTestId("fork-session-field-error")).toHaveTextContent(
        "Repository URL and branch must be at most 256 characters together.",
      );
      fireEvent.change(screen.getByRole("textbox", { name: "Branch for api" }), {
        target: { value: "feature/search" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Remove web" }));
      fireEvent.change(screen.getByTestId("fork-session-sandbox-repo-input"), {
        target: { value: "https://github.com/org/mobile.git" },
      });
      fireEvent.keyDown(screen.getByTestId("fork-session-sandbox-repo-input"), { key: "Enter" });
      expect(forkSessionMock).not.toHaveBeenCalled();
      fireEvent.change(screen.getByRole("textbox", { name: "Branch for mobile" }), {
        target: { value: "release" },
      });
      fireEvent.click(screen.getByTestId("fork-session-submit"));

      await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
      expect(forkSessionMock.mock.calls[0][1]).toMatchObject({
        upToResponseId: "resp_selected",
        sandbox: {
          provider: "agent_sandbox",
          workspaces: [
            "https://github.com/org/api#feature/search",
            "https://github.com/org/mobile.git#release",
          ],
        },
      });
      expect(sourceLabels[SANDBOX_REPO_LABEL_KEY]).toBe(
        "https://github.com/org/api#main https://github.com/org/web#stable",
      );
      expect(launchRunnerMock).not.toHaveBeenCalled();
    });

    it("adds several repositories to a local-session fork and requires pending URLs to be added", async () => {
      forkSessionMock.mockResolvedValue({ id: "conv_fork" } as ForkResponse);
      renderDialog({
        ...CODING,
        info: {
          managed_sandboxes_enabled: true,
          sandbox_provider: "agent_sandbox",
          sandbox_provider_capabilities: { agent_sandbox: { multi_repo: true } },
        },
      });
      selectSandbox();
      const input = screen.getByTestId("fork-session-sandbox-repo-input");
      fireEvent.change(input, { target: { value: "https://github.com/org/api" } });
      fireEvent.click(screen.getByTestId("fork-session-submit"));
      expect(forkSessionMock).not.toHaveBeenCalled();
      expect(input).toHaveFocus();
      expect(screen.getByTestId("fork-session-field-error")).toHaveTextContent(
        "Finish or clear the repository URL before cloning.",
      );
      fireEvent.click(screen.getByTestId("fork-session-sandbox-repo-add"));
      fireEvent.change(input, { target: { value: "git@github.com:org/web.git" } });
      fireEvent.click(screen.getByTestId("fork-session-sandbox-repo-add"));
      fireEvent.click(screen.getByTestId("fork-session-submit"));

      await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
      expect(forkSessionMock.mock.calls[0][1]?.sandbox?.workspaces).toEqual([
        "https://github.com/org/api",
        "git@github.com:org/web.git",
      ]);
    });

    it("lets a multi-repo source remove extras for a single-repo provider", async () => {
      // Switching to a smaller provider preserves all selections until the user removes them.
      forkSessionMock.mockResolvedValue({ id: "conv_fork" } as ForkResponse);
      useSessionMock.mockReturnValue({
        session: {
          labels: {
            [SANDBOX_REPO_LABEL_KEY]: "https://github.com/org/api#main https://github.com/org/web",
          },
        },
        isLoading: false,
        error: null,
      } as unknown as ReturnType<typeof useSession>);
      renderDialog({
        ...CODING,
        info: {
          managed_sandboxes_enabled: true,
          sandbox_provider: "modal",
          sandbox_providers: ["modal"],
          sandbox_provider_capabilities: { modal: { multi_repo: false } },
        },
      });

      selectSandbox();
      await act(async () => {});
      expect(screen.getAllByTestId("fork-session-sandbox-repo-row")).toHaveLength(2);
      expect(screen.queryByTestId("fork-session-sandbox-repo-add")).not.toBeInTheDocument();
      const submit = screen.getByTestId("fork-session-submit");
      expect(submit).toHaveAttribute("aria-disabled", "true");
      fireEvent.click(submit);
      expect(screen.getByTestId("fork-session-field-error")).toHaveTextContent(
        "This provider clones at most 1 repository",
      );
      expect(forkSessionMock).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "Remove web" }));
      expect(submit).not.toHaveAttribute("aria-disabled");
      fireEvent.click(submit);
      await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
      expect(forkSessionMock.mock.calls[0][1]?.sandbox?.workspaces).toEqual([
        "https://github.com/org/api#main",
      ]);
    });

    it.each([true, false, undefined])(
      "sends an explicit empty selection with workspace-list capability %s",
      async (acceptsWorkspaces) => {
        // Omitting the list would silently inherit the removed source repository.
        useSessionMock.mockReturnValue({
          session: { labels: { [SANDBOX_REPO_LABEL_KEY]: "https://github.com/org/repo" } },
          isLoading: false,
          error: null,
        } as unknown as ReturnType<typeof useSession>);
        forkSessionMock.mockResolvedValue({
          id: "conv_fork",
        } as unknown as Awaited<ReturnType<typeof forkSession>>);
        renderDialog({
          ...CODING,
          info: { managed_sandboxes_enabled: true, session_fork_workspaces: acceptsWorkspaces },
        });

        selectSandbox();
        await act(async () => {});
        fireEvent.click(screen.getByRole("button", { name: "Remove repo" }));
        openHostSelect();
        fireEvent.click(screen.getByTestId("fork-session-host-option-host_1"));
        selectSandbox();
        await act(async () => {});
        expect(screen.queryByTestId("fork-session-sandbox-repo-row")).not.toBeInTheDocument();
        fireEvent.click(screen.getByTestId("fork-session-submit"));

        await waitFor(() => expect(forkSessionMock).toHaveBeenCalledTimes(1));
        expect(forkSessionMock.mock.calls[0][1]?.sandbox).toEqual({
          provider: null,
          ...(acceptsWorkspaces ? { workspaces: [] } : { workspace: null }),
        });
      },
    );

    it("greys the submit button on a malformed repository URL", () => {
      renderDialog({ ...CODING, info: { managed_sandboxes_enabled: true } });

      selectSandbox();
      openAdvanced();
      // A blank repository is legal (empty sandbox); a bare "org/repo" is UI
      // sugar the server rejects, so catch it here instead of on a 422.
      expect(screen.getByTestId("fork-session-submit")).not.toHaveAttribute("aria-disabled");
      fireEvent.change(screen.getByTestId("fork-session-sandbox-repo-input"), {
        target: { value: "org/repo" },
      });
      expect(screen.getByTestId("fork-session-submit")).toHaveAttribute("aria-disabled", "true");

      openAdvanced();
      fireEvent.click(screen.getByTestId("fork-session-submit"));
      expect(screen.getByTestId("fork-session-sandbox-repo-input")).toHaveFocus();
      expect(screen.getByTestId("fork-session-field-error")).toHaveTextContent(
        "Enter a repository URL",
      );
      expect(forkSessionMock).not.toHaveBeenCalled();
    });
  });
});
