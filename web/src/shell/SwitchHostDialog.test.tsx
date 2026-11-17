import type { ComponentProps, ReactNode } from "react";
import type * as WorkspacePickerModule from "./WorkspacePicker";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { SwitchHostDialog } from "./SwitchHostDialog";
import { fetchHosts, useHosts } from "@/hooks/useHosts";
import { useSession } from "@/hooks/useSession";
import { useHostFilesystem } from "@/hooks/useHostFilesystem";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import { fetchSandboxProfiles } from "@/lib/sandboxProfilesApi";
import type * as SandboxProfilesModule from "@/lib/sandboxProfilesApi";
import {
  getSessionSlim,
  launchManagedHost,
  launchRunner,
  stopSession,
  updateSession,
} from "@/lib/sessionsApi";
import { getSessionHost, setSessionHost } from "@/lib/sessionHost";
import { SLICE_KEY_HEADER } from "@/lib/routingKey";
import { terminalsQueryKey, type TerminalInfo } from "@/lib/terminals";
import type * as SessionsApiModule from "@/lib/sessionsApi";
import { handleSessionEvent } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";

// Heavy children have their own suites; stub them so this one stays on the
// dialog's move sequence and its recovery from a half-finished switch.
vi.mock("./WorkspacePathField", () => ({
  WorkspacePathField: ({
    value,
    onChange,
    onCommit,
  }: {
    value: string;
    onChange: (v: string) => void;
    onCommit?: (v: string) => void;
  }) => (
    <input
      data-testid="mock-workspace-input"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      // Enter commits the typed path (opens the tree browser at it), the
      // real field's onCommit contract.
      onKeyDown={(e) => {
        if (e.key === "Enter") onCommit?.((e.target as HTMLInputElement).value);
      }}
    />
  ),
}));
// Keep the real navigability helpers but stub the picker itself — its
// filesystem fetch isn't under test. Its "Select" button commits an absolute
// path (onSelect), the only way browsing feeds the form now that typed
// ~-paths resolve directly.
vi.mock("./WorkspacePicker", async (importActual) => ({
  ...(await importActual<typeof WorkspacePickerModule>()),
  WorkspacePicker: ({ onSelect }: { onSelect: (p: string) => void }) => (
    <div data-testid="mock-workspace-picker">
      <button
        type="button"
        data-testid="mock-pick-workspace"
        onClick={() => onSelect("/Users/alice/git/omnigent")}
      >
        pick
      </button>
    </div>
  ),
}));
vi.mock("./HostLabel", () => ({
  HostLabel: ({ host }: { host: { name: string } }) => <span>{host.name}</span>,
}));
vi.mock("@/hooks/useHosts", () => ({ fetchHosts: vi.fn(), useHosts: vi.fn() }));
vi.mock("@/hooks/useSession", () => ({ useSession: vi.fn() }));
vi.mock("@/hooks/useHostFilesystem", () => ({ useHostFilesystem: vi.fn() }));
vi.mock("@/hooks/useRecentWorkspaces", () => ({
  useRecentWorkspaces: () => ({ recent: ["/Users/alice/repo"], addRecent: vi.fn() }),
}));
vi.mock("@/lib/sandboxProfilesApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SandboxProfilesModule>()),
  fetchSandboxProfiles: vi.fn(),
}));
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  getSessionSlim: vi.fn(),
  launchManagedHost: vi.fn(),
  launchRunner: vi.fn(),
  stopSession: vi.fn(),
  updateSession: vi.fn(),
}));
// Radix Select uses a portal + pointer events jsdom can't drive; a native
// <select> keeps the option list assertable.
vi.mock("@/components/ui/select", () => ({
  Select: ({
    value,
    onValueChange,
    children,
  }: {
    value: string;
    onValueChange: (v: string) => void;
    children: ReactNode;
  }) => (
    <select
      data-testid="mock-host-select"
      value={value}
      onChange={(e) => onValueChange(e.target.value)}
    >
      {children}
    </select>
  ),
  SelectTrigger: ({ children }: { children: ReactNode }) => children,
  SelectValue: () => null,
  SelectSeparator: () => null,
  SelectContent: ({ children }: { children: ReactNode }) => children,
  SelectItem: ({
    value,
    children,
    "data-testid": testId,
  }: {
    value: string;
    children: ReactNode;
    "data-testid"?: string;
  }) => (
    <option value={value} data-testid={testId}>
      {children}
    </option>
  ),
}));

const useHostsMock = vi.mocked(useHosts);
const fetchHostsMock = vi.mocked(fetchHosts);
const useHostFilesystemMock = vi.mocked(useHostFilesystem);
const launchRunnerMock = vi.mocked(launchRunner);
const getSessionSlimMock = vi.mocked(getSessionSlim);
const stopSessionMock = vi.mocked(stopSession);
const launchManagedHostMock = vi.mocked(launchManagedHost);
const updateSessionMock = vi.mocked(updateSession);

// A server that provisions Modal sandboxes, moves sessions onto them, and lets
// the caller name the new host.
const SANDBOX_SERVER: Partial<ServerInfo> = {
  managed_sandboxes_enabled: true,
  move_to_managed_sandbox: true,
  sandbox_provider: "modal",
  sandbox_providers: ["modal"],
  sandbox_provider_capabilities: {
    modal: { accepts_session_move: true },
    e2b: { accepts_session_move: true },
  },
  request_managed_sandbox_host_id: true,
};

type DialogProps = Partial<ComponentProps<typeof SwitchHostDialog>>;

/**
 * Render the dialog for a session on ``host_old``.
 *
 * @param info - Server capabilities; omitted leaves the context loading, which
 *   keeps the sandbox rows hidden.
 */
function renderDialog(
  info?: Partial<ServerInfo>,
  overrides: DialogProps = {},
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  const content = (props: DialogProps) => {
    const dialog = (
      <SwitchHostDialog
        open
        onOpenChange={() => {}}
        sessionId="conv_1"
        currentHostId="host_old"
        {...props}
      />
    );
    return (
      <QueryClientProvider client={client}>
        {info === undefined ? (
          dialog
        ) : (
          <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, ...info }}>
            {dialog}
          </CapabilitiesProvider>
        )}
      </QueryClientProvider>
    );
  };
  const view = render(content(overrides));
  return {
    client,
    ...view,
    rerenderDialog: (props: DialogProps) => view.rerender(content({ ...overrides, ...props })),
  };
}

/** Leave the session's own laptop as the only online host. */
function onlyOriginHostOnline(): void {
  useHostsMock.mockReturnValue({
    data: [
      { host_id: "host_old", name: "mac-laptop", owner: "alice", status: "online" },
      { host_id: "host_new", name: "linux-box", owner: "alice", status: "offline" },
    ],
  } as unknown as ReturnType<typeof useHosts>);
}

beforeEach(() => {
  vi.mocked(useSession).mockReturnValue({ session: null, isLoading: false, error: null });
  useHostsMock.mockReset();
  fetchHostsMock.mockReset();
  useHostFilesystemMock.mockReset();
  launchRunnerMock.mockReset();
  getSessionSlimMock.mockReset().mockResolvedValue({
    hostId: "host_old",
  } as Awaited<ReturnType<typeof getSessionSlim>>);
  stopSessionMock.mockReset().mockResolvedValue({ queued: false });
  launchManagedHostMock.mockReset();
  updateSessionMock.mockReset();
  useHostsMock.mockReturnValue({
    data: [
      { host_id: "host_old", name: "mac-laptop", owner: "alice", status: "online" },
      { host_id: "host_new", name: "linux-box", owner: "alice", status: "online" },
    ],
  } as unknown as ReturnType<typeof useHosts>);
  useHostFilesystemMock.mockReturnValue({
    data: undefined,
    isPlaceholderData: false,
  } as unknown as ReturnType<typeof useHostFilesystem>);
  updateSessionMock.mockResolvedValue({} as Awaited<ReturnType<typeof updateSession>>);
  launchRunnerMock.mockResolvedValue({ runnerId: "runner_new" });
  launchManagedHostMock.mockResolvedValue({} as Awaited<ReturnType<typeof launchManagedHost>>);
  vi.mocked(fetchSandboxProfiles).mockReset().mockResolvedValue({
    profiles: [],
    managed_providers: [],
    default_profile_ids: {},
    next_cursor: null,
  });
});

afterEach(() => {
  cleanup();
  conversationRegistry.clear();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  setSessionHost("conv_1", null);
});

describe("SwitchHostDialog", () => {
  it("shows a loading status before exposing a supported host-switch form", async () => {
    vi.mocked(useSession).mockReturnValue({ session: null, isLoading: true, error: null });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const content = () => (
      <QueryClientProvider client={client}>
        <SwitchHostDialog
          open
          onOpenChange={() => {}}
          sessionId="conv_1"
          currentHostId="host_old"
        />
      </QueryClientProvider>
    );
    const { rerender } = render(content());
    expect(screen.getByRole("status")).toHaveTextContent("Checking session capabilities…");
    expect(screen.queryByTestId("switch-host-button")).not.toBeInTheDocument();
    expect(screen.queryByTestId("mock-workspace-input")).not.toBeInTheDocument();
    expect(updateSessionMock).not.toHaveBeenCalled();
    expect(launchRunnerMock).not.toHaveBeenCalled();

    vi.mocked(useSession).mockReturnValue({
      session: null,
      isLoading: false,
      error: new Error("Session unavailable"),
    });
    rerender(content());
    expect(screen.getByRole("status")).toHaveTextContent(
      "Unable to check session capabilities. Reload to try again.",
    );
    expect(screen.getByTestId("switch-host-button")).toBeDisabled();

    vi.mocked(useSession).mockReturnValue({ session: null, isLoading: false, error: null });
    rerender(content());
    await waitFor(() => expect(screen.getByTestId("mock-host-select")).toHaveFocus());
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByTestId("switch-host-button")).toBeEnabled();
  });

  it.each([
    ["managed snapshot", null],
    ["Arclet host", "arclet"],
    ["Databricks Sandbox host", "lakebox"],
  ])("blocks a directly opened %s dialog before releasing its runner", async (source, provider) => {
    vi.mocked(useSession).mockReturnValue({
      session: {
        id: "conv_1",
        agentId: "agent_1",
        agentName: null,
        status: "idle",
        title: null,
        createdAt: 0,
        items: [],
        permissionLevel: null,
        parentSessionId: null,
        subAgentName: null,
        kind: "default",
        hostId: "host_old",
        labels: source === "managed snapshot" ? { "omnigent.host_type": "managed" } : {},
      },
      isLoading: false,
      error: null,
    });
    useHostsMock.mockReturnValue({
      data: [
        {
          host_id: "host_old",
          name: "source-host",
          owner: "alice",
          status: "online",
          sandbox_provider: provider,
        },
      ],
    } as ReturnType<typeof useHosts>);
    renderDialog();
    expect(
      screen.getByText("Switching hosts is not supported for this sandbox session yet."),
    ).toBeVisible();
    expect(screen.getByTestId("switch-host-button")).toBeDisabled();
    expect(screen.queryByTestId("mock-workspace-input")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("switch-host-button"));
    const user = userEvent.setup();
    const target = screen.getByRole("group", { name: "Switch host" });
    expect(target).toHaveAttribute("tabindex", "0");
    act(() => target.focus());
    expect(target).toHaveFocus();
    await waitFor(() =>
      expect(screen.getByRole("tooltip")).toHaveTextContent(
        "Switching hosts is not supported for this sandbox session yet.",
      ),
    );
    expect(target).toHaveAccessibleDescription(
      "Switching hosts is not supported for this sandbox session yet.",
    );
    await user.keyboard("{Enter} ");
    expect(updateSessionMock).not.toHaveBeenCalled();
    expect(launchRunnerMock).not.toHaveBeenCalled();
  });

  it("releases the runner and the model override before launching on the new host", async () => {
    const { client } = renderDialog();
    const invalidate = vi.spyOn(client, "invalidateQueries");

    const button = await screen.findByTestId("switch-host-button");
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(button);

    // The model id is resolved against the old host's catalog, so it has to
    // go with the binding — otherwise the next turn asks the new host for a
    // model it may not have. `silent` keeps the reset out of the transcript.
    await waitFor(() => expect(updateSessionMock).toHaveBeenCalledTimes(1));
    expect(updateSessionMock).toHaveBeenCalledWith("conv_1", {
      runnerId: "",
      modelOverride: null,
      silent: true,
    });
    await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(1));
    expect(launchRunnerMock).toHaveBeenCalledWith("host_new", "conv_1", "/Users/alice/repo");
    // The launch endpoint binds with `WHERE runner_id IS NULL`, so a launch
    // that raced ahead of the release would be rejected outright.
    expect(stopSessionMock).toHaveBeenCalledWith("conv_1");
    expect(stopSessionMock.mock.invocationCallOrder[0]).toBeLessThan(
      updateSessionMock.mock.invocationCallOrder[0],
    );
    expect(updateSessionMock.mock.invocationCallOrder[0]).toBeLessThan(
      launchRunnerMock.mock.invocationCallOrder[0],
    );
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["session-agent", "conv_1"] });
  });

  it("keeps the binding when stopping the old runner fails", async () => {
    stopSessionMock.mockRejectedValue(new Error("Old runner could not be stopped"));
    renderDialog();
    const button = await screen.findByTestId("switch-host-button");
    await waitFor(() => expect(button).not.toBeDisabled());
    fireEvent.click(button);
    const error = await screen.findByTestId("switch-host-error");
    expect(error).toHaveTextContent("Old runner could not be stopped");
    expect(error).toHaveTextContent(
      "Check the current host and stop the session there, then try again.",
    );
    expect(updateSessionMock).not.toHaveBeenCalled();
    expect(launchRunnerMock).not.toHaveBeenCalled();
  });

  it.each(["offline", "online", "missing", "error"] as const)(
    "rechecks a cached offline origin before moving when fresh status is %s",
    async (freshStatus) => {
      useHostsMock.mockReturnValue({
        data: [
          { host_id: "host_old", name: "laptop", owner: "alice", status: "offline" },
          { host_id: "host_new", name: "workstation", owner: "alice", status: "online" },
        ],
      } as unknown as ReturnType<typeof useHosts>);
      let resolveHosts!: (hosts: Awaited<ReturnType<typeof fetchHosts>>) => void;
      let rejectHosts!: (reason: Error) => void;
      fetchHostsMock.mockReturnValueOnce(
        new Promise((resolve, reject) => {
          resolveHosts = resolve;
          rejectHosts = reject;
        }),
      );
      const { client } = renderDialog();
      const invalidate = vi.spyOn(client, "invalidateQueries");
      const button = await screen.findByTestId("switch-host-button");
      await waitFor(() => expect(button).not.toBeDisabled());
      fireEvent.click(button);
      await waitFor(() => expect(fetchHostsMock).toHaveBeenCalledWith(true));
      expect(stopSessionMock).not.toHaveBeenCalled();
      expect(updateSessionMock).not.toHaveBeenCalled();
      expect(launchRunnerMock).not.toHaveBeenCalled();
      await act(async () => {
        if (freshStatus === "error") rejectHosts(new Error("Host refresh failed"));
        else
          resolveHosts(
            freshStatus === "missing"
              ? []
              : [{ host_id: "host_old", name: "laptop", owner: "alice", status: freshStatus }],
          );
      });
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ["hosts"] });
      if (freshStatus === "error") {
        const error = await screen.findByTestId("switch-host-error");
        expect(error).toHaveTextContent("Host refresh failed");
        expect(error).toHaveTextContent("Check your connection and try again.");
        expect(updateSessionMock).not.toHaveBeenCalled();
        expect(launchRunnerMock).not.toHaveBeenCalled();
      } else {
        await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(1));
        expect(stopSessionMock).toHaveBeenCalledTimes(freshStatus === "offline" ? 0 : 1);
        expect(updateSessionMock).toHaveBeenCalledTimes(1);
        if (freshStatus !== "offline")
          expect(stopSessionMock.mock.invocationCallOrder[0]).toBeLessThan(
            updateSessionMock.mock.invocationCallOrder[0],
          );
      }
    },
  );

  it("resets the terminals cache so the strip re-attaches to the new host", async () => {
    const { client } = renderDialog();
    // Stale entry: the origin host's shell. The terminals query never goes
    // stale on its own (staleTime: Infinity) and its queryFn unions with the
    // cache, so only an explicit clear can drop a dead entry.
    client.setQueryData<TerminalInfo[]>(terminalsQueryKey("conv_1"), [
      { id: "terminal_shellA_s1", name: "shellA", session: "s1", running: true },
    ]);
    const invalidate = vi.spyOn(client, "invalidateQueries");

    const button = await screen.findByTestId("switch-host-button");
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(button);

    await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(1));
    // The old host's shell is dropped synchronously…
    await waitFor(() =>
      expect(client.getQueryData<TerminalInfo[]>(terminalsQueryKey("conv_1"))).toEqual([]),
    );
    // …and the list is refetched so the new host's shells replace it.
    expect(invalidate).toHaveBeenCalledWith({ queryKey: terminalsQueryKey("conv_1") });
  });

  it.each([
    ["release", "online"],
    ["launch", "online"],
    ["release", "offline"],
  ] as const)(
    "offers the origin host again when %s fails after stopping and the origin is %s",
    async (failure, originStatus) => {
      const failedRequest = failure === "release" ? updateSessionMock : launchRunnerMock;
      failedRequest.mockRejectedValueOnce(new Error("move request failed"));
      const { client } = renderDialog();
      const invalidate = vi.spyOn(client, "invalidateQueries");

      // Moving to the host it is already on is a no-op, so the origin starts
      // out of the list.
      expect(screen.queryByTestId("switch-host-option-host_old")).toBeNull();
      expect(screen.getByTestId("switch-host-option-host_new")).toBeInTheDocument();

      const button = await screen.findByTestId("switch-host-button");
      await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
      fireEvent.click(button);

      // A confirmed stop makes the origin a recovery target even if release fails.
      const error = await screen.findByTestId("switch-host-error");
      expect(error).toHaveTextContent(
        failure === "release" ? "The session stopped" : "Check its current host before retrying.",
      );
      expect(error).not.toHaveTextContent("isn't running anywhere");
      if (failure === "release") {
        expect(error).not.toHaveTextContent("left its old host");
        expect(launchRunnerMock).not.toHaveBeenCalled();
      }
      expect(error).toHaveTextContent("move request failed");
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ["session", "conv_1"] });
      expect(screen.getByTestId("switch-host-option-host_old")).toBeInTheDocument();

      fireEvent.change(screen.getByTestId("mock-host-select"), { target: { value: "host_old" } });
      let finishRetry!: () => void;
      const recoveryHostId = originStatus === "offline" ? "host_new" : "host_old";
      if (originStatus === "offline") {
        const hosts = [
          { host_id: "host_old", name: "laptop", owner: "alice", status: "offline" as const },
          { host_id: "host_new", name: "workstation", owner: "alice", status: "online" as const },
        ];
        useHostsMock.mockReturnValue({ data: hosts } as unknown as ReturnType<typeof useHosts>);
        fetchHostsMock.mockReturnValueOnce(
          new Promise((resolve) => {
            finishRetry = () => resolve(hosts);
          }),
        );
        stopSessionMock.mockRejectedValue(new Error("Original host disconnected"));
        fireEvent.change(screen.getByTestId("mock-host-select"), {
          target: { value: recoveryHostId },
        });
      } else {
        stopSessionMock.mockReturnValueOnce(
          new Promise((resolve) => {
            finishRetry = () => resolve({ queued: false });
          }),
        );
      }
      await waitFor(() => expect(button).toBeEnabled());
      fireEvent.click(button);
      if (originStatus === "offline") {
        await waitFor(() => expect(fetchHostsMock).toHaveBeenCalledWith(true));
        expect(stopSessionMock).toHaveBeenCalledTimes(1);
      } else {
        await waitFor(() => expect(stopSessionMock).toHaveBeenCalledTimes(2));
        expect(screen.getByTestId("switch-host-option-host_old")).toBeInTheDocument();
      }
      expect(screen.getByTestId("mock-host-select")).toHaveValue(recoveryHostId);
      expect(updateSessionMock).toHaveBeenCalledTimes(1);
      await act(async () => finishRetry());
      await waitFor(() =>
        expect(launchRunnerMock).toHaveBeenLastCalledWith(
          recoveryHostId,
          "conv_1",
          "/Users/alice/repo",
        ),
      );
      expect(stopSessionMock).toHaveBeenCalledTimes(originStatus === "offline" ? 1 : 2);
      expect(updateSessionMock).toHaveBeenCalledTimes(2);
      if (originStatus === "online")
        expect(stopSessionMock.mock.invocationCallOrder[1]).toBeLessThan(
          updateSessionMock.mock.invocationCallOrder[1],
        );
      expect(updateSessionMock.mock.invocationCallOrder[1]).toBeLessThan(
        launchRunnerMock.mock.invocationCallOrder.at(-1)!,
      );
    },
  );

  it.each([
    { reopen: false, failure: "stop" },
    { reopen: true, failure: "stop" },
    { reopen: true, failure: "lookup" },
    { reopen: true, failure: null },
  ] as const)(
    "requires Stop on recovery with a stale offline origin ($reopen, $failure)",
    async ({ reopen, failure }) => {
      const hosts = [
        { host_id: "host_old", name: "laptop", owner: "alice", status: "offline" as const },
        { host_id: "host_new", name: "workstation", owner: "alice", status: "online" as const },
      ];
      useHostsMock.mockReturnValue({ data: hosts } as unknown as ReturnType<typeof useHosts>);
      fetchHostsMock.mockResolvedValue(hosts);
      vi.mocked(useSession).mockReturnValue({
        session: { hostId: "host_old" } as Awaited<ReturnType<typeof getSessionSlim>>,
        isLoading: false,
        error: null,
      });
      const api = await vi.importActual<typeof SessionsApiModule>("@/lib/sessionsApi");
      getSessionSlimMock.mockImplementation(api.getSessionSlim);
      stopSessionMock.mockImplementation(api.stopSession);
      vi.stubEnv("VITE_DATABRICKS_WORKSPACE", "true");
      setSessionHost("conv_1", "host_old");
      let boundHost = "host_old";
      let resolveRequest!: (response: Response) => void;
      let rejectRequest!: (reason: Error) => void;
      const pendingRequest = new Promise<Response>((resolve, reject) => {
        resolveRequest = resolve;
        rejectRequest = reject;
      });
      const stopHosts: (string | null)[] = [];
      vi.stubGlobal(
        "fetch",
        vi.fn(async (url: string, init?: RequestInit) => {
          if (url.startsWith("/v1/sessions/conv_1?")) {
            if (boundHost === "host_new" && failure === "lookup") return pendingRequest;
            return Response.json({
              id: "conv_1",
              agent_id: "agent_1",
              created_at: 0,
              status: "running",
              host_id: boundHost,
              runner_id: boundHost === "host_old" ? "runner_old" : "runner_new",
            });
          }
          expect(url).toBe("/v1/sessions/conv_1/events");
          const hostId = new Headers(init?.headers).get(SLICE_KEY_HEADER);
          stopHosts.push(hostId);
          // A, B and the unkeyed default have separate replica-local tunnels.
          if (hostId !== boundHost)
            return Response.json({ error: { code: "wrong_replica" } }, { status: 400 });
          return pendingRequest;
        }),
      );
      launchRunnerMock.mockImplementationOnce(async () => {
        boundHost = "host_new";
        throw new Error("launch response lost");
      });
      const onOpenChange = vi.fn();
      const { client, unmount } = renderDialog(undefined, { onOpenChange });
      vi.spyOn(client, "invalidateQueries").mockImplementation(() => new Promise<void>(() => {}));
      let button = await screen.findByTestId("switch-host-button");
      await waitFor(() => expect(button).toBeEnabled());
      fireEvent.click(button);
      await screen.findByText(/launch response lost/);
      expect(stopSessionMock).not.toHaveBeenCalled();
      expect(updateSessionMock).toHaveBeenCalledTimes(1);
      expect(getSessionHost("conv_1")).toBe("host_old");

      if (reopen) {
        fireEvent.click(screen.getByRole("button", { name: "Close" }));
        expect(onOpenChange).toHaveBeenCalledWith(false);
        unmount();
        renderDialog(undefined, { onOpenChange }, client);
        button = await screen.findByTestId("switch-host-button");
      }
      await waitFor(() => expect(button).toBeEnabled());
      fireEvent.click(button);
      await waitFor(() => expect(getSessionSlimMock).toHaveBeenCalledTimes(2));
      if (failure !== "lookup") {
        await waitFor(() => expect(stopHosts).toEqual(["host_new"]));
        expect(getSessionHost("conv_1")).toBe("host_new");
      } else expect(stopSessionMock).not.toHaveBeenCalled();
      expect(updateSessionMock).toHaveBeenCalledTimes(1);
      expect(launchRunnerMock).toHaveBeenCalledTimes(1);
      if (failure === null) {
        await act(async () => resolveRequest(Response.json({ queued: false })));
        await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(2));
        expect(updateSessionMock).toHaveBeenCalledTimes(2);
        expect(stopSessionMock.mock.invocationCallOrder[0]).toBeLessThan(
          updateSessionMock.mock.invocationCallOrder[1],
        );
      } else {
        const detail = failure === "stop" ? "Current runner unreachable" : "Session refresh failed";
        await act(async () => rejectRequest(new Error(detail)));
        expect(await screen.findByTestId("switch-host-error")).toHaveTextContent(detail);
        expect(updateSessionMock).toHaveBeenCalledTimes(1);
        expect(launchRunnerMock).toHaveBeenCalledTimes(1);
      }
      expect(fetchHostsMock).toHaveBeenCalledTimes(1);
    },
  );

  it.each(["stop", "release"] as const)(
    "stops the refreshed binding and reports a retry %s failure",
    async (failure) => {
      launchRunnerMock.mockRejectedValueOnce(new Error("launch response lost"));
      const { rerenderDialog } = renderDialog();
      const button = await screen.findByTestId("switch-host-button");
      await waitFor(() => expect(button).toBeEnabled());
      fireEvent.click(button);
      await screen.findByText(/launch response lost/);

      // The launch landed despite its lost response; only the new host is online.
      useHostsMock.mockReturnValue({
        data: [
          { host_id: "host_old", name: "mac-laptop", owner: "alice", status: "offline" },
          { host_id: "host_new", name: "linux-box", owner: "alice", status: "online" },
        ],
      } as unknown as ReturnType<typeof useHosts>);
      getSessionSlimMock.mockResolvedValue({ hostId: "host_new" } as Awaited<
        ReturnType<typeof getSessionSlim>
      >);
      rerenderDialog({ currentHostId: "host_new" });
      const failedRequest = failure === "stop" ? stopSessionMock : updateSessionMock;
      failedRequest.mockRejectedValueOnce(new Error("retry failed"));
      await waitFor(() => expect(button).toBeEnabled());
      fireEvent.click(button);

      const error = await screen.findByTestId("switch-host-error");
      await waitFor(() => expect(error).toHaveTextContent("retry failed"));
      expect(error).toHaveTextContent(
        failure === "stop"
          ? "Check the current host and stop the session there, then try again."
          : "The session stopped, but the move didn't finish.",
      );
      expect(error).not.toHaveTextContent("The old session stopped");
      expect(error).not.toHaveTextContent("Check the original host");
      if (failure === "stop") {
        expect(error).not.toHaveTextContent("The session left its old host");
        expect(error).not.toHaveTextContent("Pick a host");
      }
      expect(stopSessionMock).toHaveBeenCalledTimes(2);
      expect(updateSessionMock).toHaveBeenCalledTimes(failure === "stop" ? 1 : 2);
      expect(launchRunnerMock).toHaveBeenCalledTimes(1);
    },
  );

  it("enables the switch for a typed tilde path without opening the browser", async () => {
    // The reported journey: type "~/git/omnigent" into the field and stop —
    // no Enter, no browsing. The dialog resolves ~ against the host's home
    // (from the home listing) so the typed path is directly submittable.
    useHostFilesystemMock.mockReturnValue({
      data: { entries: [{ name: "git", path: "/Users/alice/git", type: "directory" }] },
      isPlaceholderData: false,
    } as unknown as ReturnType<typeof useHostFilesystem>);
    renderDialog();

    const button = await screen.findByTestId("switch-host-button");
    const input = screen.getByTestId("mock-workspace-input");
    fireEvent.change(input, { target: { value: "~/git/omnigent" } });

    // No Enter, no browser — the switch enables purely from the ~-resolve.
    expect(screen.queryByTestId("mock-workspace-picker")).not.toBeInTheDocument();
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));

    fireEvent.click(button);
    await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(1));
    // Launched with the resolved absolute path, not the raw tilde.
    expect(launchRunnerMock).toHaveBeenCalledWith(
      "host_new",
      "conv_1",
      "/Users/alice/git/omnigent",
    );
  });

  it("adopts an absolute path picked from the tree browser", async () => {
    // The browse route: type a ~-path (not yet submittable), open the tree
    // browser via Enter, then "Select" commits the browser's absolute path
    // through this dialog's distinct handleWorkspaceChange, enabling the
    // switch. (Typed ~-paths resolve directly, tested above — this covers the
    // still-live browser path.)
    renderDialog();

    const button = await screen.findByTestId("switch-host-button");
    const input = screen.getByTestId("mock-workspace-input");
    // A tilde value alone is not submittable (the server never expands ~).
    fireEvent.change(input, { target: { value: "~/git/omnigent" } });
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(true));

    // Enter commits the typed path and opens the tree browser at it.
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByTestId("mock-workspace-picker")).toBeInTheDocument();

    // "Select" commits the browser's absolute path into the form.
    fireEvent.click(screen.getByTestId("mock-pick-workspace"));
    expect((input as HTMLInputElement).value).toBe("/Users/alice/git/omnigent");
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));

    fireEvent.click(button);
    await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(1));
    expect(launchRunnerMock).toHaveBeenCalledWith(
      "host_new",
      "conv_1",
      "/Users/alice/git/omnigent",
    );
  });

  describe("new sandbox target", () => {
    it("lists a sandbox per provider only on a server that can move a session onto one", () => {
      renderDialog({ ...SANDBOX_SERVER, managed_sandboxes_enabled: false });
      expect(screen.queryByTestId("switch-host-sandbox-option-modal")).toBeNull();
      cleanup();

      // An older server launches sandboxes but lacks the move, which would
      // fail only after the switch has released the runner.
      renderDialog({ ...SANDBOX_SERVER, move_to_managed_sandbox: false });
      expect(screen.queryByTestId("switch-host-sandbox-option-modal")).toBeNull();
      cleanup();

      renderDialog({ ...SANDBOX_SERVER, sandbox_providers: ["modal", "e2b"] });
      expect(screen.getByTestId("switch-host-sandbox-option-modal")).toHaveTextContent(
        "Modal Sandbox",
      );
      expect(screen.getByTestId("switch-host-sandbox-option-e2b")).toBeInTheDocument();
      // A fresh provision is never the default while another host is online.
      expect(screen.getByTestId("mock-host-select")).toHaveValue("host_new");
    });

    it("lists no sandbox once the session has lost its host", () => {
      // The server only moves a session off a host it still has, e.g. not after
      // an earlier switch failed partway and left it unbound.
      vi.mocked(useSession).mockReturnValue({
        session: {
          id: "conv_1",
          agentId: "agent_1",
          agentName: null,
          status: "idle",
          title: null,
          createdAt: 0,
          items: [],
          permissionLevel: null,
          parentSessionId: null,
          subAgentName: null,
          kind: "default",
          hostId: null,
          labels: {},
        },
        isLoading: false,
        error: null,
      });
      renderDialog(SANDBOX_SERVER);
      expect(screen.queryByTestId("switch-host-sandbox-option-modal")).toBeNull();
      expect(screen.getByTestId("mock-host-select")).toHaveValue("host_new");
    });

    it.each([false, true])(
      "leaves out a provider a session can't move onto (profile discovery on: %s)",
      (profileDiscovery) => {
        const server = {
          ...SANDBOX_SERVER,
          sandbox_providers: ["modal", "profiled", "unreported"],
          sandbox_provider_capabilities: {
            modal: { accepts_session_move: true },
            // Manages sandbox profiles, which the server won't move a session onto.
            profiled: { accepts_session_move: false },
            // Says nothing about moves, so it is left out too.
            unreported: { multi_repo: true },
          },
          features: { sandbox_profiles: profileDiscovery },
        };
        renderDialog(server);
        expect(screen.getByTestId("switch-host-sandbox-option-modal")).toBeInTheDocument();
        expect(screen.queryByTestId("switch-host-sandbox-option-profiled")).toBeNull();
        expect(screen.queryByTestId("switch-host-sandbox-option-unreported")).toBeNull();
        cleanup();

        // With nothing else to pick, nothing can release the runner.
        onlyOriginHostOnline();
        renderDialog({ ...server, sandbox_providers: ["profiled", "unreported"] });
        expect(screen.getByTestId("switch-host-no-targets")).toBeVisible();
        expect(screen.queryByTestId("switch-host-button")).toBeNull();
        expect(updateSessionMock).not.toHaveBeenCalled();
        expect(fetchSandboxProfiles).not.toHaveBeenCalled();
      },
    );

    it("moves the session onto a new sandbox after releasing its runner", async () => {
      conversationRegistry.acquire("conv_1");
      // What the move returns; its launch events may reach another replica's stream.
      launchManagedHostMock.mockResolvedValue({
        sandboxStatus: { stage: "provisioning", error: null, provider: "modal" },
      } as Awaited<ReturnType<typeof launchManagedHost>>);
      const { client } = renderDialog(SANDBOX_SERVER);
      client.setQueryData<TerminalInfo[]>(terminalsQueryKey("conv_1"), [
        { id: "terminal_shellA_s1", name: "shellA", session: "s1", running: true },
      ]);

      fireEvent.change(screen.getByTestId("mock-host-select"), {
        target: { value: "__sandbox__:modal" },
      });
      // A sandbox doesn't exist yet, so a repository replaces the directory.
      expect(screen.queryByTestId("mock-workspace-input")).toBeNull();
      fireEvent.change(screen.getByTestId("switch-host-sandbox-repo-input"), {
        target: { value: "https://github.com/example/repo" },
      });
      fireEvent.change(screen.getByTestId("switch-host-sandbox-branch-input"), {
        target: { value: "feature/login" },
      });
      fireEvent.click(screen.getByTestId("switch-host-button"));

      await waitFor(() => expect(launchManagedHostMock).toHaveBeenCalledTimes(1));
      expect(updateSessionMock).toHaveBeenCalledWith("conv_1", {
        runnerId: "",
        modelOverride: null,
        silent: true,
      });
      // The managed-host endpoint refuses a session whose runner is still bound.
      expect(stopSessionMock).toHaveBeenCalledWith("conv_1");
      expect(stopSessionMock.mock.invocationCallOrder[0]).toBeLessThan(
        updateSessionMock.mock.invocationCallOrder[0],
      );
      expect(updateSessionMock.mock.invocationCallOrder[0]).toBeLessThan(
        launchManagedHostMock.mock.invocationCallOrder[0],
      );
      const [sessionId, sandbox] = launchManagedHostMock.mock.calls[0];
      expect(sessionId).toBe("conv_1");
      expect(sandbox).toMatchObject({
        provider: "modal",
        workspace: "https://github.com/example/repo#feature/login",
      });
      expect(sandbox.hostId).toMatch(/^[0-9a-f]{32}$/);
      expect(launchRunnerMock).not.toHaveBeenCalled();
      await waitFor(() =>
        expect(client.getQueryData<TerminalInfo[]>(terminalsQueryKey("conv_1"))).toEqual([]),
      );
      expect(conversationRegistry.peek("conv_1")!.getState().sandboxStatus).toMatchObject({
        stage: "provisioning",
        provider: "modal",
      });
    });

    it("doesn't reopen a launch that finished before the move's response", async () => {
      conversationRegistry.acquire("conv_1");
      launchManagedHostMock.mockImplementation(async () => {
        for (const stage of ["provisioning", "ready"] as const) {
          handleSessionEvent(
            {
              type: "session_sandbox_status",
              conversationId: "conv_1",
              stage,
              error: null,
              provider: "modal",
              details: null,
            },
            "conv_1",
          );
        }
        // Read before the launch finished, so it still says provisioning.
        return {
          sandboxStatus: { stage: "provisioning", error: null, provider: "modal" },
        } as Awaited<ReturnType<typeof launchManagedHost>>;
      });
      const { client } = renderDialog(SANDBOX_SERVER);
      fireEvent.change(screen.getByTestId("mock-host-select"), {
        target: { value: "__sandbox__:modal" },
      });
      fireEvent.click(screen.getByTestId("switch-host-button"));

      await waitFor(() =>
        expect(client.getQueryData<TerminalInfo[]>(terminalsQueryKey("conv_1"))).toEqual([]),
      );
      expect(conversationRegistry.peek("conv_1")!.getState().sandboxStatus).toBeNull();
    });

    it("keeps a launch event that lands before the move's response", async () => {
      conversationRegistry.acquire("conv_1");
      launchManagedHostMock.mockImplementation(async () => {
        handleSessionEvent(
          {
            type: "session_sandbox_status",
            conversationId: "conv_1",
            stage: "failed",
            error: "quota exceeded",
            provider: "modal",
            details: null,
          },
          "conv_1",
        );
        return {
          sandboxStatus: { stage: "provisioning", error: null, provider: "modal" },
        } as Awaited<ReturnType<typeof launchManagedHost>>;
      });
      const { client } = renderDialog(SANDBOX_SERVER);
      fireEvent.change(screen.getByTestId("mock-host-select"), {
        target: { value: "__sandbox__:modal" },
      });
      fireEvent.click(screen.getByTestId("switch-host-button"));

      // The move is through once the old host's terminals are cleared.
      await waitFor(() =>
        expect(client.getQueryData<TerminalInfo[]>(terminalsQueryKey("conv_1"))).toEqual([]),
      );
      expect(conversationRegistry.peek("conv_1")!.getState().sandboxStatus).toMatchObject({
        stage: "failed",
        error: "quota exceeded",
      });
    });

    it("waits for the runner release, and doesn't move when it fails", async () => {
      let release!: (value: Awaited<ReturnType<typeof updateSession>>) => void;
      updateSessionMock.mockReturnValueOnce(
        new Promise((resolve) => {
          release = resolve;
        }),
      );
      renderDialog(SANDBOX_SERVER);
      fireEvent.change(screen.getByTestId("mock-host-select"), {
        target: { value: "__sandbox__:modal" },
      });
      fireEvent.click(screen.getByTestId("switch-host-button"));
      await waitFor(() => expect(updateSessionMock).toHaveBeenCalledTimes(1));
      // The endpoint refuses a session whose runner is still bound.
      expect(launchManagedHostMock).not.toHaveBeenCalled();
      await act(async () => {
        release({} as Awaited<ReturnType<typeof updateSession>>);
      });
      await waitFor(() => expect(launchManagedHostMock).toHaveBeenCalledTimes(1));
      cleanup();

      updateSessionMock.mockRejectedValueOnce(new Error("release failed"));
      renderDialog(SANDBOX_SERVER);
      fireEvent.change(screen.getByTestId("mock-host-select"), {
        target: { value: "__sandbox__:modal" },
      });
      fireEvent.click(screen.getByTestId("switch-host-button"));
      expect(await screen.findByTestId("switch-host-error")).toHaveTextContent("release failed");
      expect(launchManagedHostMock).toHaveBeenCalledTimes(1);
    });

    it("defaults to an empty sandbox when no other host is online", async () => {
      onlyOriginHostOnline();
      renderDialog(SANDBOX_SERVER);

      expect(screen.queryByTestId("switch-host-no-targets")).toBeNull();
      await waitFor(() =>
        expect(screen.getByTestId("mock-host-select")).toHaveValue("__sandbox__:modal"),
      );
      fireEvent.click(screen.getByTestId("switch-host-button"));

      await waitFor(() => expect(launchManagedHostMock).toHaveBeenCalledTimes(1));
      expect(launchManagedHostMock).toHaveBeenCalledWith("conv_1", {
        provider: "modal",
        workspace: null,
        hostId: expect.stringMatching(/^[0-9a-f]{32}$/),
      });
    });

    it("blocks a branch without a repository, a malformed repository URL and an invalid branch", () => {
      onlyOriginHostOnline();
      renderDialog(SANDBOX_SERVER);
      const button = screen.getByTestId("switch-host-button");
      const repo = screen.getByTestId("switch-host-sandbox-repo-input");

      fireEvent.change(screen.getByTestId("switch-host-sandbox-branch-input"), {
        target: { value: "main" },
      });
      expect(button).toBeDisabled();
      expect(screen.getByTestId("switch-host-sandbox-repo-error")).toHaveTextContent(
        "Add the repository this branch belongs to.",
      );

      fireEvent.change(repo, { target: { value: "/Users/alice/repo" } });
      expect(button).toBeDisabled();
      expect(screen.getByTestId("switch-host-sandbox-repo-error")).toHaveTextContent(
        "Enter a repository URL",
      );

      fireEvent.change(repo, { target: { value: "git@github.com:org/repo.git" } });
      expect(button).toBeEnabled();
      expect(screen.queryByTestId("switch-host-sandbox-repo-error")).toBeNull();

      // A bad branch is reported under the branch input, not the repository.
      fireEvent.change(screen.getByTestId("switch-host-sandbox-branch-input"), {
        target: { value: "bad..branch" },
      });
      expect(button).toBeDisabled();
      expect(screen.getByTestId("switch-host-sandbox-branch-error")).toHaveTextContent(
        "Enter a valid branch name",
      );
      expect(screen.queryByTestId("switch-host-sandbox-repo-error")).toBeNull();
    });

    it("offers the origin host again when the sandbox launch is refused", async () => {
      launchManagedHostMock.mockRejectedValue(
        new Error("sandbox provider 'modal' is not configured on this server"),
      );
      onlyOriginHostOnline();
      renderDialog(SANDBOX_SERVER);

      fireEvent.click(screen.getByTestId("switch-host-button"));

      const error = await screen.findByTestId("switch-host-error");
      expect(error).toHaveTextContent("Check its current host before retrying.");
      expect(error).toHaveTextContent("is not configured on this server");
      expect(screen.getByTestId("switch-host-option-host_old")).toBeInTheDocument();
    });
  });
});
