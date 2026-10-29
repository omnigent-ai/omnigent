import { useRef, useState, type ReactNode } from "react";
import type * as WorkspacePickerModule from "./WorkspacePicker";
import type * as TopologyModule from "@/hooks/useExecutionTopology";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { SwitchHostDialog } from "./SwitchHostDialog";
import {
  fetchHostModelOptions,
  fetchHosts,
  useHostModelOptions,
  useHosts,
  type Host,
} from "@/hooks/useHosts";
import { useSession } from "@/hooks/useSession";
import { useExecutionTopology, type ExecutionTopology } from "@/hooks/useExecutionTopology";
import { checkHostDirectory, useHostFilesystem } from "@/hooks/useHostFilesystem";
import { getSessionSlim, launchRunner, updateSession } from "@/lib/sessionsApi";
import { terminalsQueryKey, type TerminalInfo } from "@/lib/terminals";
import type { NativeModelOption, Session } from "@/lib/types";

// The dialog is exercised through its entry point, so the registry check, the
// session read and the apply flow run together; heavy children are stubbed.
vi.mock("./WorkspacePathField", () => ({
  WorkspacePathField: ({ value, onChange }: { value: string; onChange: (v: string) => void }) => (
    <input
      data-testid="mock-workspace-input"
      value={value}
      onChange={(e) => onChange(e.target.value)}
    />
  ),
}));
vi.mock("./WorkspacePicker", async (importActual) => ({
  ...(await importActual<typeof WorkspacePickerModule>()),
  WorkspacePicker: () => null,
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: vi.fn(),
  useHostModelOptions: vi.fn(),
  fetchHosts: vi.fn(),
  fetchHostModelOptions: vi.fn(),
}));
vi.mock("@/hooks/useSession", () => ({ useSession: vi.fn() }));
vi.mock("@/hooks/useExecutionTopology", async (actual) => ({
  ...(await actual<typeof TopologyModule>()),
  useExecutionTopology: vi.fn(),
}));
vi.mock("@/hooks/useHostFilesystem", () => ({
  useHostFilesystem: vi.fn(),
  checkHostDirectory: vi.fn(),
}));
vi.mock("@/hooks/useRecentWorkspaces", () => ({
  useRecentWorkspaces: () => ({ recent: ["/Users/alice/repo"], addRecent: vi.fn() }),
}));
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));
vi.mock("@/lib/sessionsApi", () => ({
  getSessionSlim: vi.fn(),
  launchRunner: vi.fn(),
  updateSession: vi.fn(),
}));
// Radix Select uses a portal + pointer events jsdom can't drive; a native
// <select> per control (keyed by componentId) keeps options assertable.
vi.mock("@/components/ui/select", () => ({
  Select: ({
    value,
    onValueChange,
    componentId,
    disabled,
    children,
  }: {
    value: string;
    onValueChange: (v: string) => void;
    componentId: string;
    disabled?: boolean;
    children: ReactNode;
  }) => (
    <select
      data-testid={componentId}
      value={value}
      disabled={disabled}
      onChange={(e) => onValueChange(e.target.value)}
    >
      <option value="">(none)</option>
      {children}
    </select>
  ),
  SelectTrigger: ({ children }: { children: ReactNode }) => children,
  SelectValue: () => null,
  SelectContent: ({ children }: { children: ReactNode }) => children,
  SelectItem: ({
    value,
    children,
    "data-testid": testId,
    disabled,
  }: {
    value: string;
    children: ReactNode;
    "data-testid"?: string;
    disabled?: boolean;
  }) => (
    <option disabled={disabled} value={value} data-testid={testId}>
      {children}
    </option>
  ),
}));

const CLAUDE_ROWS: NativeModelOption[] = [
  { id: "opus", displayName: "Opus", isDefault: true },
  { id: "sonnet", displayName: "Sonnet" },
];

const HOSTS: Host[] = [
  { host_id: "host_old", name: "mac-laptop", owner: "alice", status: "online" },
  { host_id: "host_new", name: "linux-box", owner: "alice", status: "online" },
  { host_id: "host_work", name: "mac-work", owner: "alice", status: "online" },
];

// Two machines. "claude-personal" runs on both; "claude-work" only on the Mac.
// "openai-personal" is another provider and must never be offered to a Claude
// Code session.
function twoMachineTopology(): ExecutionTopology {
  const route = (host_id: string, machine_id: string, account_id: string, harness: string) => ({
    host_id,
    machine_id,
    account_id,
    harness: harness as "claude-native" | "codex-native",
    available: true,
    reason: null,
  });
  return {
    configured: true,
    machines: [
      { id: "mac", label: "Mac" },
      { id: "linux", label: "Linux" },
    ],
    accounts: [
      {
        id: "claude-personal",
        label: "Claude Personal",
        provider: "anthropic",
        harness: "claude-native",
      },
      { id: "claude-work", label: "Claude Work", provider: "anthropic", harness: "claude-native" },
      {
        id: "openai-personal",
        label: "OpenAI Personal",
        provider: "openai",
        harness: "codex-native",
      },
    ],
    bindings: [
      route("host_old", "mac", "claude-personal", "claude-native"),
      route("host_new", "linux", "claude-personal", "claude-native"),
      route("host_work", "mac", "claude-work", "claude-native"),
      route("host_old", "mac", "openai-personal", "codex-native"),
    ],
  };
}

function oneMachineTopology(): ExecutionTopology {
  const view = twoMachineTopology();
  return {
    ...view,
    machines: [view.machines[0]!],
    bindings: view.bindings.filter((b) => b.machine_id === "mac"),
  };
}

// Simulated server: the session the API reports, mutated by the API mocks.
let server: Session;
let view: ExecutionTopology;
let hosts: Host[];
let catalogs: Record<
  string,
  { data?: NativeModelOption[]; isPending?: boolean; isError?: boolean }
>;
const catalogRefetch = vi.fn();

const useHostsMock = vi.mocked(useHosts);
const useHostModelOptionsMock = vi.mocked(useHostModelOptions);
const fetchHostsMock = vi.mocked(fetchHosts);
const fetchHostModelOptionsMock = vi.mocked(fetchHostModelOptions);
const checkHostDirectoryMock = vi.mocked(checkHostDirectory);
const getSessionSlimMock = vi.mocked(getSessionSlim);
const launchRunnerMock = vi.mocked(launchRunner);
const updateSessionMock = vi.mocked(updateSession);

function baseSession(overrides: Partial<Session> = {}): Session {
  return {
    id: "conv_1",
    agentId: "ag_x",
    status: "idle",
    items: [],
    hostId: "host_old",
    runnerId: "runner_old",
    harness: "claude-native",
    modelOverride: "opus",
    // What the harness reports it runs; the override is only the request.
    llmModel: "opus",
    reasoningEffort: "high",
    costControlModeOverride: null,
    workspace: "/home/alice/work/omnigent",
    labels: {},
    ...overrides,
  } as Session;
}

function renderDialog(currentWorkspace?: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onOpenChange = vi.fn();
  render(
    <QueryClientProvider client={client}>
      <SwitchHostDialog
        open
        onOpenChange={onOpenChange}
        sessionId="conv_1"
        currentHostId="host_old"
        currentWorkspace={currentWorkspace}
      />
    </QueryClientProvider>,
  );
  return { client, onOpenChange };
}

const select = (id: string) => screen.getByTestId(id) as HTMLSelectElement;
const button = () => screen.getByTestId("switch-host-button") as HTMLButtonElement;
const choose = (id: string, value: string) => fireEvent.change(select(id), { target: { value } });
const routeDetail = () => screen.getByTestId("switch-route-detail").textContent;

async function ready() {
  await screen.findByTestId("switch_host.account");
}

async function expectEnabled() {
  await waitFor(() => expect(button().disabled).toBe(false));
}

async function expectBlocked(text: string) {
  await waitFor(() => expect(screen.getByTestId("switch-blocker").textContent).toContain(text));
  expect(button().disabled).toBe(true);
}

function noMutations() {
  expect(updateSessionMock).not.toHaveBeenCalled();
  expect(launchRunnerMock).not.toHaveBeenCalled();
}

beforeEach(() => {
  vi.clearAllMocks();
  view = twoMachineTopology();
  hosts = HOSTS;
  vi.mocked(useExecutionTopology).mockImplementation(
    () => ({ data: view, isPending: false, isError: false }) as never,
  );
  vi.mocked(useSession).mockReturnValue({ session: null, isLoading: false, error: null });
  server = baseSession();
  catalogs = {};
  useHostsMock.mockImplementation(() => ({ data: hosts }) as never);
  fetchHostsMock.mockImplementation(async () => hosts);
  useHostModelOptionsMock.mockImplementation((hostId, harness) => {
    const state = catalogs[`${hostId}|${harness}`] ?? { data: CLAUDE_ROWS };
    return {
      data: state.data,
      isPending: state.isPending ?? false,
      isError: state.isError ?? false,
      error: state.isError ? new Error("unavailable") : null,
      isFetching: false,
      refetch: catalogRefetch,
    } as never;
  });
  fetchHostModelOptionsMock.mockImplementation(async (hostId, harness) => {
    const state = catalogs[`${hostId}|${harness}`] ?? { data: CLAUDE_ROWS };
    if (!state.data) throw new Error("probe failed");
    return state.data;
  });
  vi.mocked(useHostFilesystem).mockReturnValue({
    data: undefined,
    isPlaceholderData: false,
  } as never);
  checkHostDirectoryMock.mockResolvedValue(null);
  getSessionSlimMock.mockImplementation(async () => ({ ...server }));
  updateSessionMock.mockImplementation(async (_id, updates) => {
    server = {
      ...server,
      ...(updates.runnerId === "" ? { runnerId: null } : {}),
      ...(updates.abandonTransition ? { runnerId: null, accountTransition: null } : {}),
      ...(updates.targetHostId
        ? {
            accountTransition: {
              id: "test-transition",
              account_id: updates.accountId!,
              host_id: updates.targetHostId,
              workspace: updates.targetWorkspace,
              harness: updates.targetHarness!,
              model: updates.targetModel!,
              effort: updates.targetEffort ?? null,
            },
          }
        : {}),
      ...("modelOverride" in updates ? { modelOverride: updates.modelOverride ?? null } : {}),
      ...("reasoningEffort" in updates ? { reasoningEffort: updates.reasoningEffort ?? null } : {}),
      ...("costControlModeOverride" in updates
        ? { costControlModeOverride: updates.costControlModeOverride ?? null }
        : {}),
    };
    // A confirmed live change is what the harness then reports.
    if ("modelOverride" in updates && !updates.silent && server.runnerId) {
      server = { ...server, llmModel: server.modelOverride ?? null };
    }
    return { ...server };
  });
  launchRunnerMock.mockImplementation(async (hostId) => {
    server = {
      ...server,
      hostId,
      runnerId: "runner_new",
      llmModel: server.modelOverride ?? null,
      accountTransition: null,
    };
    return { runnerId: "runner_new" };
  });
});

afterEach(() => cleanup());

describe("SwitchAccountDialog", () => {
  describe("draft", () => {
    it("starts from the registered account, model and effort and treats it as a no-op", async () => {
      renderDialog();
      await ready();

      expect(select("switch_host.machine").value).toBe("mac");
      expect(select("switch_host.account").value).toBe("claude-personal");
      expect(select("switch_host.model").value).toBe("opus");
      expect(select("switch_host.effort").value).toBe("high");
      expect(screen.getByTestId("switch-current-summary").textContent).toBe(
        "Now: Claude Personal · Claude Code · Opus · High",
      );
      // Same host, still bound: no directory to choose and nothing to apply.
      expect(screen.queryByTestId("mock-workspace-input")).toBeNull();
      await expectBlocked("Already using this account, machine, model and effort.");
    });

    it("offers only accounts of the session's own harness", async () => {
      renderDialog();
      await ready();

      expect(screen.getByTestId("switch-account-option-claude-work")).toBeTruthy();
      expect(screen.queryByTestId("switch-account-option-openai-personal")).toBeNull();
      expect(screen.queryByTestId("switch_host.family")).toBeNull();
    });

    it("lists Codex accounts for a Codex session", async () => {
      server = baseSession({ harness: "codex-native", modelOverride: null, llmModel: null });
      renderDialog();
      await ready();

      expect(select("switch_host.account").value).toBe("openai-personal");
      expect(screen.queryByTestId("switch-account-option-claude-personal")).toBeNull();
    });

    it("shows the machine control only when accounts span several machines", async () => {
      view = oneMachineTopology();
      renderDialog();
      await ready();

      expect(screen.queryByTestId("switch_host.machine")).toBeNull();
      expect(select("switch_host.account").value).toBe("claude-personal");
      expect(select("switch_host.account").disabled).toBe(false);
    });

    it("does not relabel an unregistered current host as an account", async () => {
      server = baseSession({ hostId: "host_unknown" });
      renderDialog();
      await ready();

      expect(select("switch_host.account").value).toBe("");
      expect(screen.getByTestId("switch-current-summary").textContent).toContain(
        "Unregistered account",
      );
      await expectBlocked("Choose a machine.");
      noMutations();
    });

    it("keeps a busy session locked until its turn finishes", async () => {
      server = baseSession({ status: "running" });
      renderDialog();
      await ready();

      choose("switch_host.model", "sonnet");
      await expectBlocked("The session is busy.");
    });

    it("keeps the actions outside the scrolling form", async () => {
      renderDialog();
      await ready();

      const body = screen.getByTestId("switch-scroll-body");
      const actions = screen.getByTestId("switch-actions");
      expect(body.contains(actions)).toBe(false);
      expect(actions.parentElement).toBe(body.parentElement);
      expect(actions).toContainElement(screen.getByTestId("switch-cancel-button"));
    });

    it("hides route identifiers in an optional connection disclosure", async () => {
      renderDialog();
      await ready();

      expect(screen.getByTestId("switch-connection-details")).not.toHaveAttribute("open");
      fireEvent.click(screen.getByText("Connection details"));
      expect(routeDetail()).toContain("mac-laptop");
      expect(routeDetail()).toContain("Claude Code");
      expect(routeDetail()).toContain("Route ID: host_old");
    });

    it("hands focus back to a non-trigger opener when closed with Escape", async () => {
      function Opener() {
        const ref = useRef<HTMLButtonElement>(null);
        const [open, setOpen] = useState(false);
        return (
          <>
            <button type="button" ref={ref} data-testid="opener" onClick={() => setOpen(true)}>
              open
            </button>
            {open && (
              <SwitchHostDialog
                open
                onOpenChange={setOpen}
                sessionId="conv_1"
                currentHostId="host_old"
                returnFocusRef={ref}
              />
            )}
          </>
        );
      }
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      render(
        <QueryClientProvider client={client}>
          <Opener />
        </QueryClientProvider>,
      );
      const opener = screen.getByTestId("opener");
      opener.focus();
      fireEvent.click(opener);
      await ready();
      fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
      await waitFor(() => expect(screen.queryByTestId("switch-host-dialog")).toBeNull());
      await waitFor(() => expect(document.activeElement).toBe(opener));
    });
  });

  describe("same host", () => {
    it("changes only the model through a live PATCH, without a restart", async () => {
      renderDialog();
      await ready();

      choose("switch_host.model", "sonnet");
      await expectEnabled();
      expect(button().textContent).toBe("Apply");
      fireEvent.click(button());

      await waitFor(() => expect(updateSessionMock).toHaveBeenCalledTimes(1));
      // Not silent: the runner applies it to the live harness. Effort is
      // unchanged, so it isn't re-sent.
      expect(updateSessionMock).toHaveBeenCalledWith("conv_1", {
        modelOverride: "sonnet",
      });
      expect(launchRunnerMock).not.toHaveBeenCalled();
    });

    it("changes only the effort", async () => {
      renderDialog();
      await ready();

      choose("switch_host.effort", "max");
      await expectEnabled();
      fireEvent.click(button());

      await waitFor(() => expect(updateSessionMock).toHaveBeenCalledTimes(1));
      expect(updateSessionMock).toHaveBeenCalledWith("conv_1", {
        reasoningEffort: "max",
      });
    });

    it("keeps the model-only path free of terminal resets", async () => {
      const { client } = renderDialog();
      const shells = [{ id: "t1", name: "shellA", session: "s1", running: true }];
      client.setQueryData<TerminalInfo[]>(terminalsQueryKey("conv_1"), shells);
      await ready();

      choose("switch_host.model", "sonnet");
      await expectEnabled();
      fireEvent.click(button());
      await waitFor(() => expect(updateSessionMock).toHaveBeenCalledTimes(1));
      expect(client.getQueryData(terminalsQueryKey("conv_1"))).toEqual(shells);
    });

    it("pins the explicit model and turns Smart Routing off, even for the same model", async () => {
      server = baseSession({ modelOverride: null, costControlModeOverride: "on" });
      renderDialog();
      await ready();

      await expectEnabled();
      fireEvent.click(button());
      await waitFor(() => expect(updateSessionMock).toHaveBeenCalledTimes(1));
      expect(updateSessionMock).toHaveBeenCalledWith("conv_1", {
        modelOverride: "opus",
        costControlModeOverride: "off",
      });
    });
  });

  describe("moving machines", () => {
    it("records the move, writes the settings, then launches with the route", async () => {
      const { client } = renderDialog("/home/alice/work/omnigent");
      client.setQueryData<TerminalInfo[]>(terminalsQueryKey("conv_1"), [
        { id: "terminal_shellA_s1", name: "shellA", session: "s1", running: true },
      ]);
      const invalidate = vi.spyOn(client, "invalidateQueries");
      await ready();

      choose("switch_host.machine", "linux");
      // Account, model and effort carry over to a compatible machine.
      expect(select("switch_host.account").value).toBe("claude-personal");
      expect(select("switch_host.model").value).toBe("opus");
      expect(select("switch_host.effort").value).toBe("high");
      expect(routeDetail()).toContain("host_new");
      noMutations();

      await expectEnabled();
      expect(button().textContent).toBe("Move session");
      fireEvent.click(button());
      await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(1));

      expect(updateSessionMock.mock.calls[0]![1]).toEqual({
        runnerId: "",
        accountId: "claude-personal",
        targetHostId: "host_new",
        targetHarness: "claude-native",
        targetModel: "opus",
        targetEffort: "high",
        targetWorkspace: "/home/alice/work/omnigent",
        silent: true,
      });
      expect(updateSessionMock.mock.calls[1]![1]).toEqual({
        modelOverride: "opus",
        reasoningEffort: "high",
        costControlModeOverride: "off",
        silent: true,
        transitionId: "test-transition",
      });
      expect(launchRunnerMock).toHaveBeenCalledWith(
        "host_new",
        "conv_1",
        "/home/alice/work/omnigent",
        undefined,
        { accountId: "claude-personal", transitionId: "test-transition" },
      );
      // The launch binds with `WHERE runner_id IS NULL`, so the release lands first.
      expect(updateSessionMock.mock.invocationCallOrder[1]).toBeLessThan(
        launchRunnerMock.mock.invocationCallOrder[0],
      );
      // The old host's shells are dropped and the list refetched.
      await waitFor(() =>
        expect(client.getQueryData<TerminalInfo[]>(terminalsQueryKey("conv_1"))).toEqual([]),
      );
      expect(invalidate).toHaveBeenCalledWith({ queryKey: terminalsQueryKey("conv_1") });
    });

    it("keeps the session's directory when the destination has it, else warns about the change", async () => {
      renderDialog("/home/alice/work/omnigent");
      await ready();
      choose("switch_host.machine", "linux");
      await waitFor(() =>
        expect((screen.getByTestId("mock-workspace-input") as HTMLInputElement).value).toBe(
          "/home/alice/work/omnigent",
        ),
      );
      expect(checkHostDirectoryMock).toHaveBeenCalledWith("host_new", "/home/alice/work/omnigent");
      expect(screen.queryByTestId("switch-host-warning")).toBeNull();

      fireEvent.change(screen.getByTestId("mock-workspace-input"), {
        target: { value: "/srv/other" },
      });
      expect(screen.getByTestId("switch-host-warning").textContent).toContain(
        "continues in /srv/other instead of /home/alice/work/omnigent",
      );
    });

    it("falls back to the recent directory when the session's is missing there", async () => {
      checkHostDirectoryMock.mockImplementation(async (_host, path) =>
        path === "/home/alice/work/omnigent" ? "doesn't exist on this host" : null,
      );
      renderDialog("/home/alice/work/omnigent");
      await ready();
      choose("switch_host.machine", "linux");

      await waitFor(() =>
        expect((screen.getByTestId("mock-workspace-input") as HTMLInputElement).value).toBe(
          "/Users/alice/repo",
        ),
      );
    });

    it("clears the account when the new machine lacks it, and keeps model and effort", async () => {
      renderDialog();
      await ready();
      choose("switch_host.account", "claude-work");
      expect(routeDetail()).toContain("host_work");

      choose("switch_host.machine", "linux");
      expect(select("switch_host.account").value).toBe("");
      expect(screen.getByTestId("switch-selection-notice").textContent).toContain(
        "Choose an account",
      );
      choose("switch_host.account", "claude-personal");
      expect(select("switch_host.model").value).toBe("opus");
      expect(select("switch_host.effort").value).toBe("high");
      noMutations();
    });

    it("changes account on one machine without resetting compatible model and effort", async () => {
      renderDialog();
      await ready();

      choose("switch_host.account", "claude-work");
      expect(select("switch_host.machine").value).toBe("mac");
      expect(select("switch_host.model").value).toBe("opus");
      expect(select("switch_host.effort").value).toBe("high");
      expect(routeDetail()).toContain("host_work");
      noMutations();
    });

    it("requires an explicit model when the destination lacks the current one", async () => {
      catalogs["host_new|claude-native"] = { data: [CLAUDE_ROWS[1]!] };
      renderDialog();
      await ready();

      choose("switch_host.machine", "linux");
      expect((await screen.findByTestId("switch-model-unsupported")).textContent).toContain(
        "opus isn't offered here",
      );
      expect(select("switch_host.model").value).toBe("");
      await expectBlocked("Choose a model.");

      choose("switch_host.model", "sonnet");
      await expectEnabled();
      fireEvent.click(button());
      await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(1));
      expect(updateSessionMock.mock.calls[0]![1]).toMatchObject({ targetModel: "sonnet" });
    });
  });

  describe("destination catalog", () => {
    it("shows loading and keeps Apply disabled until the catalog arrives", async () => {
      catalogs["host_new|claude-native"] = { isPending: true };
      renderDialog();
      await ready();

      choose("switch_host.machine", "linux");
      expect(await screen.findByTestId("switch-catalog-loading")).toBeInTheDocument();
      await expectBlocked("Loading models…");
    });

    it("shows a load error with a working Retry", async () => {
      catalogs["host_new|claude-native"] = { isError: true };
      renderDialog();
      await ready();

      choose("switch_host.machine", "linux");
      const error = await screen.findByTestId("switch-catalog-error");
      expect(error.textContent).toContain("unavailable");
      expect(button().disabled).toBe(true);
      fireEvent.click(screen.getByTestId("switch-catalog-retry"));
      expect(catalogRefetch).toHaveBeenCalledTimes(1);
    });

    it("treats an empty catalog as unusable", async () => {
      catalogs["host_new|claude-native"] = { data: [] };
      renderDialog();
      await ready();

      choose("switch_host.machine", "linux");
      expect(await screen.findByTestId("switch-catalog-empty")).toBeInTheDocument();
      await expectBlocked("This account reports no models.");
    });

    it("blocks a failed catalog refresh even when cached models exist", async () => {
      catalogs["host_old|claude-native"] = { data: CLAUDE_ROWS, isError: true };
      renderDialog();
      await ready();

      await expectBlocked("model list didn't load");
      expect(screen.queryByTestId("switch_host.model")).toBeNull();
    });

    it("re-checks the catalog at Apply and stops before any mutation when it changed", async () => {
      renderDialog();
      await ready();

      choose("switch_host.machine", "linux");
      await expectEnabled();
      // The account's models changed after the picker loaded.
      fetchHostModelOptionsMock.mockResolvedValueOnce([CLAUDE_ROWS[1]!]);
      fireEvent.click(button());

      expect((await screen.findByTestId("switch-host-error")).textContent).toContain(
        "no longer offers the model",
      );
      noMutations();
    });

    it("requires a new effort when the model doesn't support the current one", async () => {
      server = baseSession({ harness: "codex-native", modelOverride: "gpt-5.5" });
      hosts = HOSTS;
      const codexRows: NativeModelOption[] = [
        {
          id: "gpt-5.5",
          supportedReasoningEfforts: [{ reasoningEffort: "low" }, { reasoningEffort: "high" }],
        },
        { id: "gpt-5.5-mini", supportedReasoningEfforts: [{ reasoningEffort: "low" }] },
      ];
      catalogs["host_old|codex-native"] = { data: codexRows };
      renderDialog();
      await ready();

      choose("switch_host.model", "gpt-5.5-mini");
      expect((await screen.findByTestId("switch-effort-unsupported")).textContent).toContain(
        "High isn't available",
      );
      await expectBlocked("Choose an effort for this model.");
      choose("switch_host.effort", "low");
      await expectEnabled();
    });
  });

  describe("routes", () => {
    it("keeps the offline current route visible instead of choosing an online duplicate", async () => {
      view.bindings.find(
        (b) => b.host_id === "host_old" && b.harness === "claude-native",
      )!.available = false;
      hosts = HOSTS.map((h) => (h.host_id === "host_old" ? { ...h, status: "offline" } : h));
      renderDialog();
      await ready();

      expect(select("switch_host.account").value).toBe("claude-personal");
      expect(routeDetail()).toContain("host_old");
      await expectBlocked("unavailable");
      noMutations();
    });

    it("offers a same-machine connection recovery that only mutates on Apply", async () => {
      view.accounts = view.accounts.filter((a) => a.id === "claude-personal");
      view.machines = [view.machines[0]!];
      view.bindings = [
        {
          host_id: "host_old",
          machine_id: "mac",
          account_id: "claude-personal",
          harness: "claude-native",
          available: false,
          reason: "offline",
        },
        {
          host_id: "host_work",
          machine_id: "mac",
          account_id: "claude-personal",
          harness: "claude-native",
          available: true,
          reason: null,
        },
      ];
      hosts = HOSTS.map((h) => (h.host_id === "host_old" ? { ...h, status: "offline" } : h));
      renderDialog("/home/alice/work/omnigent");
      await screen.findByTestId("switch-use-available-connection");
      expect(routeDetail()).toContain("host_old");

      fireEvent.click(screen.getByTestId("switch-use-available-connection"));
      expect(routeDetail()).toContain("host_work");
      expect(select("switch_host.model").value).toBe("opus");
      noMutations();

      await expectEnabled();
      fireEvent.click(button());
      await waitFor(() => expect(launchRunnerMock).toHaveBeenCalled());
      expect(updateSessionMock.mock.calls[0]![1]).toMatchObject({
        runnerId: "",
        targetHostId: "host_work",
      });
    });
  });

  describe("pending transition", () => {
    const marker = {
      id: "persisted",
      account_id: "claude-personal",
      host_id: "host_new",
      harness: "claude-native",
      model: "opus",
      effort: "high",
    };

    it("adopts the recorded destination and locks the pickers", async () => {
      server = baseSession({ runnerId: null, accountTransition: marker });
      renderDialog("/home/alice/work/omnigent");
      await ready();

      expect(select("switch_host.machine").value).toBe("linux");
      expect(select("switch_host.machine").disabled).toBe(true);
      expect(select("switch_host.account").disabled).toBe(true);
      expect(select("switch_host.model").disabled).toBe(true);
      expect(screen.getByTestId("switch-current-summary").textContent).toContain(
        "Move interrupted",
      );
      expect(screen.getByTestId("switch-pending-notice").textContent).toContain("interrupted");
    });

    it("cancels the move, refreshes the session and frees the pickers", async () => {
      server = baseSession({ runnerId: null, accountTransition: marker });
      renderDialog("/home/alice/work/omnigent");
      await ready();
      expect(select("switch_host.machine").disabled).toBe(true);

      fireEvent.click(screen.getByTestId("switch-cancel-move-button"));

      await waitFor(() => expect(select("switch_host.machine").disabled).toBe(false));
      expect(updateSessionMock).toHaveBeenCalledTimes(1);
      expect(updateSessionMock).toHaveBeenCalledWith("conv_1", {
        transitionId: "persisted",
        abandonTransition: true,
      });
      expect(launchRunnerMock).not.toHaveBeenCalled();
      expect(select("switch_host.account").disabled).toBe(false);
      expect(screen.queryByTestId("switch-cancel-move-button")).toBeNull();
      expect(screen.queryByTestId("switch-pending-notice")).toBeNull();
    });

    it("keeps the move recorded when cancelling fails", async () => {
      server = baseSession({ runnerId: null, accountTransition: marker });
      updateSessionMock.mockRejectedValueOnce(new Error("Only the owner can cancel."));
      renderDialog("/home/alice/work/omnigent");
      await ready();

      fireEvent.click(screen.getByTestId("switch-cancel-move-button"));

      expect((await screen.findByTestId("switch-host-error")).textContent).toContain(
        "Only the owner can cancel.",
      );
      expect(select("switch_host.machine").disabled).toBe(true);
      expect(screen.getByTestId("switch-cancel-move-button")).toBeTruthy();
    });

    it("offers no Cancel move without a recorded move", async () => {
      renderDialog();
      await ready();
      expect(screen.queryByTestId("switch-cancel-move-button")).toBeNull();
    });

    it("resumes without recording or detaching again", async () => {
      server = baseSession({ runnerId: null, accountTransition: marker });
      renderDialog("/home/alice/work/omnigent");
      await ready();
      await expectEnabled();
      fireEvent.click(button());

      await waitFor(() => expect(launchRunnerMock).toHaveBeenCalled());
      expect(updateSessionMock.mock.calls.every(([, update]) => update.runnerId !== "")).toBe(true);
      expect(updateSessionMock.mock.calls[0]![1]).toMatchObject({ transitionId: "persisted" });
      expect(launchRunnerMock.mock.calls[0]![4]).toEqual({
        accountId: "claude-personal",
        transitionId: "persisted",
      });
    });

    it("does not substitute a duplicate route for an unavailable pending destination", async () => {
      view.bindings.find((b) => b.host_id === "host_new")!.available = false;
      server = baseSession({ runnerId: null, accountTransition: marker });
      renderDialog();
      await ready();

      expect(routeDetail()).toContain("host_new");
      await expectBlocked("unavailable");
      expect(screen.queryByTestId("switch-use-available-connection")).toBeNull();
    });

    it("adopts a transition recorded elsewhere while the dialog is open", async () => {
      const { client } = renderDialog();
      await ready();

      server = baseSession({ runnerId: null, accountTransition: marker });
      client.setQueryData(["session-switch-snapshot", "conv_1"], server);
      await waitFor(() => expect(select("switch_host.machine").value).toBe("linux"));
      expect(select("switch_host.model").disabled).toBe(true);
      noMutations();
    });
  });

  describe("preflight", () => {
    it("stops before any mutation when the destination went offline", async () => {
      renderDialog();
      await ready();
      choose("switch_host.machine", "linux");
      await expectEnabled();
      fetchHostsMock.mockResolvedValue(
        HOSTS.map((h) => (h.host_id === "host_new" ? { ...h, status: "offline" } : h)),
      );
      fireEvent.click(button());

      expect((await screen.findByTestId("switch-host-error")).textContent).toContain("is offline");
      noMutations();
    });

    it("stops before any mutation when the directory is missing", async () => {
      renderDialog();
      await ready();
      choose("switch_host.machine", "linux");
      await expectEnabled();
      checkHostDirectoryMock.mockResolvedValue("The working directory doesn't exist.");
      fireEvent.click(button());

      expect((await screen.findByTestId("switch-host-error")).textContent).toContain(
        "doesn't exist",
      );
      noMutations();
    });

    it("stops before any mutation when a turn started meanwhile", async () => {
      renderDialog();
      await ready();
      choose("switch_host.model", "sonnet");
      await expectEnabled();
      server = baseSession({ status: "running" });
      fireEvent.click(button());

      expect((await screen.findByTestId("switch-host-error")).textContent).toContain(
        "started a turn",
      );
      noMutations();
    });

    it("stops when the session changed elsewhere during the checks", async () => {
      renderDialog();
      await ready();
      choose("switch_host.model", "sonnet");
      await expectEnabled();
      let reads = 0;
      getSessionSlimMock.mockImplementation(async () => {
        reads += 1;
        return { ...server, modelOverride: reads >= 2 ? "sonnet" : "opus" };
      });
      fireEvent.click(button());

      expect((await screen.findByTestId("switch-host-error")).textContent).toContain(
        "changed while it was being checked",
      );
      noMutations();
    });
  });

  describe("recovery", () => {
    it("explains a stranded session and retries the launch without recording again", async () => {
      launchRunnerMock.mockRejectedValueOnce(new Error("host is offline"));
      renderDialog("/home/alice/work/omnigent");
      await ready();
      choose("switch_host.machine", "linux");
      await expectEnabled();
      fireEvent.click(button());

      const error = await screen.findByTestId("switch-host-error");
      expect(error.textContent).toContain("isn't running anywhere");
      expect(error.textContent).toContain("host is offline");
      const recordings = updateSessionMock.mock.calls.filter(([, u]) => u.runnerId === "");
      expect(recordings).toHaveLength(1);

      await expectEnabled();
      expect(button().textContent).toBe("Move session");
      fireEvent.click(button());
      await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(2));
      expect(updateSessionMock.mock.calls.filter(([, u]) => u.runnerId === "")).toHaveLength(1);
    });

    it("locks Apply when the state after a failure can't be read back", async () => {
      launchRunnerMock.mockRejectedValueOnce(new Error("boom"));
      renderDialog("/home/alice/work/omnigent");
      await ready();
      choose("switch_host.machine", "linux");
      await expectEnabled();
      let reads = 0;
      getSessionSlimMock.mockImplementation(async () => {
        reads += 1;
        if (reads > 2) throw new Error("unreachable");
        return { ...server };
      });
      fireEvent.click(button());

      await screen.findByTestId("switch-recheck");
      expect(button().disabled).toBe(true);
    });

    it("re-sends a model whose live change failed on retry", async () => {
      updateSessionMock.mockRejectedValueOnce(new Error("runner busy"));
      renderDialog();
      await ready();
      choose("switch_host.model", "sonnet");
      await expectEnabled();
      fireEvent.click(button());
      await screen.findByTestId("switch-host-error");

      // The failed write may be stored without reaching the harness.
      server = { ...server, modelOverride: "sonnet" };
      await expectEnabled();
      fireEvent.click(button());
      await waitFor(() => expect(updateSessionMock).toHaveBeenCalledTimes(2));
      expect(updateSessionMock.mock.calls[1]![1]).toMatchObject({ modelOverride: "sonnet" });
    });
  });

  describe("requested versus reported model", () => {
    it("shows the reported model and re-sends a stored request it doesn't report", async () => {
      server = baseSession({ modelOverride: "sonnet", llmModel: "opus" });
      renderDialog();
      await ready();

      expect(screen.getByTestId("switch-current-requested").textContent).toContain(
        "requested Sonnet, not active yet",
      );
      expect(select("switch_host.model").value).toBe("sonnet");
      expect(screen.getByTestId("switch-model-unapplied")).toBeTruthy();
      await expectEnabled();
      fireEvent.click(button());
      await waitFor(() => expect(updateSessionMock).toHaveBeenCalledTimes(1));
      expect(updateSessionMock.mock.calls[0]![1]).toMatchObject({ modelOverride: "sonnet" });
    });

    it("shows a request the session hasn't reported yet as pending, not applied", async () => {
      server = baseSession({ modelOverride: "sonnet", llmModel: null });
      renderDialog();
      await ready();

      expect(await screen.findByTestId("switch-model-pending")).toBeTruthy();
      await expectBlocked("becomes active when the session reports it");
    });
  });
});
