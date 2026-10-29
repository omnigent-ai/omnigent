import type { ReactNode } from "react";
import type * as WorkspacePickerModule from "./WorkspacePicker";
import type * as SwitchAccountDialogModule from "./SwitchAccountDialog";
import type * as TopologyModule from "@/hooks/useExecutionTopology";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { SwitchHostDialog } from "./SwitchHostDialog";
import { useHosts } from "@/hooks/useHosts";
import { useSession } from "@/hooks/useSession";
import { useExecutionTopology } from "@/hooks/useExecutionTopology";
import { checkHostDirectory, useHostFilesystem } from "@/hooks/useHostFilesystem";
import { getSessionSlim, launchRunner, updateSession } from "@/lib/sessionsApi";
import { terminalsQueryKey, type TerminalInfo } from "@/lib/terminals";
import type { Session } from "@/lib/types";

// Heavy children have their own suites; stub them so this one stays on the
// dialog's two-call move and its recovery from a half-finished switch.
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
vi.mock("@/hooks/useHosts", () => ({ useHosts: vi.fn() }));
vi.mock("@/hooks/useSession", () => ({ useSession: vi.fn() }));
vi.mock("@/hooks/useHostFilesystem", () => ({
  useHostFilesystem: vi.fn(),
  checkHostDirectory: vi.fn(),
}));
vi.mock("@/hooks/useExecutionTopology", async (importActual) => ({
  ...(await importActual<typeof TopologyModule>()),
  useExecutionTopology: vi.fn(),
}));
// The account dialog has its own suite; here it only marks that the
// router chose it.
vi.mock("./SwitchAccountDialog", async (importActual) => ({
  ...(await importActual<typeof SwitchAccountDialogModule>()),
  SwitchAccountDialog: (props: { harness: string }) => (
    <div data-testid="account-dialog" data-harness={props.harness} />
  ),
}));
vi.mock("@/hooks/useRecentWorkspaces", () => ({
  useRecentWorkspaces: () => ({ recent: ["/Users/alice/repo"], addRecent: vi.fn() }),
}));
vi.mock("@/lib/sessionsApi", () => ({
  getSessionSlim: vi.fn(),
  launchRunner: vi.fn(),
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
const useHostFilesystemMock = vi.mocked(useHostFilesystem);
const checkHostDirectoryMock = vi.mocked(checkHostDirectory);
const useExecutionTopologyMock = vi.mocked(useExecutionTopology);
const getSessionSlimMock = vi.mocked(getSessionSlim);
const launchRunnerMock = vi.mocked(launchRunner);
const updateSessionMock = vi.mocked(updateSession);

function renderDialog(currentWorkspace?: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <SwitchHostDialog
        open
        onOpenChange={() => {}}
        sessionId="conv_1"
        currentHostId="host_old"
        currentWorkspace={currentWorkspace}
      />
    </QueryClientProvider>,
  );
  return client;
}

function topology(configured: boolean, bindings: { host_id: string; harness: string }[] = []) {
  useExecutionTopologyMock.mockReturnValue({
    data: { configured, machines: [], accounts: [], bindings },
    isError: false,
  } as unknown as ReturnType<typeof useExecutionTopology>);
}

beforeEach(() => {
  vi.mocked(useSession).mockReturnValue({ session: null, isLoading: false, error: null });
  useHostsMock.mockReset();
  useHostFilesystemMock.mockReset();
  launchRunnerMock.mockReset();
  updateSessionMock.mockReset();
  getSessionSlimMock.mockReset();
  checkHostDirectoryMock.mockReset();
  checkHostDirectoryMock.mockResolvedValue(null);
  topology(false);
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
});

afterEach(() => cleanup());

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
    const client = renderDialog();
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
    expect(updateSessionMock.mock.invocationCallOrder[0]).toBeLessThan(
      launchRunnerMock.mock.invocationCallOrder[0],
    );
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["session-agent", "conv_1"] });
  });

  it("resets the terminals cache so the strip re-attaches to the new host", async () => {
    const client = renderDialog();
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

  it("offers the origin host again when the launch fails after the release", async () => {
    launchRunnerMock.mockRejectedValue(new Error("host is offline"));
    renderDialog();

    // Moving to the host it is already on is a no-op, so the origin starts
    // out of the list.
    expect(screen.queryByTestId("switch-host-option-host_old")).toBeNull();
    expect(screen.getByTestId("switch-host-option-host_new")).toBeInTheDocument();

    const button = await screen.findByTestId("switch-host-button");
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(button);

    // Released but not re-bound: the session is on no host at all, so the
    // origin becomes a real target and the copy has to say what happened
    // rather than read as two unrelated outages.
    const error = await screen.findByTestId("switch-host-error");
    expect(error.textContent).toContain("isn't running anywhere");
    expect(error.textContent).toContain("host is offline");
    expect(screen.getByTestId("switch-host-option-host_old")).toBeInTheDocument();
  });

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

  it("is the plain host move when no account registry is configured", async () => {
    renderDialog();

    expect(await screen.findByTestId("mock-host-select")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Switch host" })).toBeInTheDocument();
    expect(screen.queryByTestId("switch-legacy-notice")).toBeNull();
    expect(getSessionSlimMock).not.toHaveBeenCalled();
  });

  describe("account registry", () => {
    function session(overrides: Partial<Session> = {}): Session {
      return {
        id: "conv_1",
        hostId: "host_old",
        harness: "claude-native",
        labels: {},
        parentSessionId: null,
        ...overrides,
      } as Session;
    }

    it("opens the account dialog for a native Claude Code session", async () => {
      topology(true);
      getSessionSlimMock.mockResolvedValue(session());
      renderDialog();

      expect((await screen.findByTestId("account-dialog")).dataset.harness).toBe("claude-native");
    });

    it("shows the host-only dialog, not a spinner, while the registry loads", async () => {
      useExecutionTopologyMock.mockReturnValue({
        data: undefined,
        isPending: true,
        isError: false,
      } as unknown as ReturnType<typeof useExecutionTopology>);
      renderDialog();

      expect(await screen.findByTestId("mock-host-select")).toBeInTheDocument();
      expect(screen.getByRole("heading", { name: "Switch host" })).toBeInTheDocument();
      expect(screen.queryByTestId("account-dialog")).toBeNull();
      expect(getSessionSlimMock).not.toHaveBeenCalled();
    });

    it("shows an error with Try again, not the host form, when the registry can't be read", async () => {
      const refetch = vi.fn();
      useExecutionTopologyMock.mockReturnValue({
        data: undefined,
        isPending: false,
        isError: true,
        refetch,
      } as unknown as ReturnType<typeof useExecutionTopology>);
      renderDialog();

      expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load machines");
      expect(screen.queryByTestId("mock-host-select")).toBeNull();
      expect(getSessionSlimMock).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      expect(refetch).toHaveBeenCalledTimes(1);
    });

    it("asks for a live registry view while the dialog is open", () => {
      renderDialog();
      expect(useExecutionTopologyMock).toHaveBeenCalledWith(true, { live: true });
    });

    it("shows a loading status, not the host form, while a configured session loads", async () => {
      topology(true);
      getSessionSlimMock.mockReturnValue(new Promise(() => {}));
      renderDialog();

      expect(await screen.findByRole("status")).toHaveTextContent("Loading");
      expect(screen.getByRole("heading", { name: "Switch model or account" })).toBeInTheDocument();
      expect(screen.queryByTestId("mock-host-select")).toBeNull();
      expect(screen.queryByTestId("account-dialog")).toBeNull();
    });

    it.each([
      ["a harness without model switching", { harness: "pi-native" }],
      ["a saved inference profile", { inferenceConfigured: true }],
      ["a sub-agent", { parentSessionId: "conv_parent" }],
    ])("keeps %s on the host-only dialog", async (_label, overrides) => {
      topology(true, [{ host_id: "host_new", harness: "claude-native" }]);
      getSessionSlimMock.mockResolvedValue(session(overrides));
      renderDialog();

      expect(await screen.findByTestId("mock-host-select")).toBeInTheDocument();
      expect(screen.queryByTestId("account-dialog")).toBeNull();
      expect(screen.queryByTestId("switch-legacy-notice")).toBeNull();
    });

    describe("registered hosts only", () => {
      const hostOptions = () =>
        Array.from(
          (screen.getByTestId("mock-host-select") as HTMLSelectElement).options,
          (o) => o.value,
        );

      beforeEach(() => {
        useHostsMock.mockReturnValue({
          data: [
            { host_id: "host_old", name: "mac-laptop", owner: "alice", status: "online" },
            { host_id: "host_new", name: "linux-box", owner: "alice", status: "online" },
            { host_id: "host_other", name: "spare-box", owner: "alice", status: "online" },
          ],
        } as unknown as ReturnType<typeof useHosts>);
      });

      it("offers a native session only the hosts registered for its harness", async () => {
        topology(true, [
          { host_id: "host_old", harness: "claude-native" },
          { host_id: "host_new", harness: "claude-native" },
          { host_id: "host_other", harness: "codex-native" },
        ]);
        getSessionSlimMock.mockResolvedValue(session({ inferenceConfigured: true }));
        renderDialog();

        // The unfiltered list shows first, then the session read narrows it.
        await waitFor(() => expect(hostOptions()).toEqual(["host_new"]));
      });

      it("offers every online host to a harness the registry doesn't cover", async () => {
        topology(true, [{ host_id: "host_new", harness: "claude-native" }]);
        getSessionSlimMock.mockResolvedValue(session({ harness: "pi-native" }));
        renderDialog();

        await waitFor(() => expect(getSessionSlimMock).toHaveBeenCalled());
        await waitFor(() => expect(hostOptions()).toEqual(["host_new", "host_other"]));
      });
    });

    it("shows a retry when the session can't be read", async () => {
      topology(true);
      getSessionSlimMock.mockRejectedValue(new Error("boom"));
      renderDialog();

      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Couldn't read this session's current state.",
      );
      expect(screen.queryByTestId("mock-host-select")).toBeNull();
      expect(screen.queryByTestId("switch-host-button")).toBeNull();

      getSessionSlimMock.mockResolvedValue(session());
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      expect(await screen.findByTestId("account-dialog")).toBeInTheDocument();
    });
  });

  describe("working directory default", () => {
    const input = () => screen.getByTestId("mock-workspace-input") as HTMLInputElement;

    it("keeps the session's directory when the new host has it", async () => {
      renderDialog("/home/alice/work/omnigent");

      await waitFor(() => expect(input().value).toBe("/home/alice/work/omnigent"));
      expect(checkHostDirectoryMock).toHaveBeenCalledWith("host_new", "/home/alice/work/omnigent");

      fireEvent.click(await screen.findByTestId("switch-host-button"));
      await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(1));
      expect(launchRunnerMock).toHaveBeenCalledWith(
        "host_new",
        "conv_1",
        "/home/alice/work/omnigent",
      );
    });

    it("falls back to the host's recent directory when the session's is missing there", async () => {
      checkHostDirectoryMock.mockResolvedValue("doesn't exist on this host");
      renderDialog("/home/alice/work/omnigent");

      await waitFor(() => expect(input().value).toBe("/Users/alice/repo"));
    });

    it("falls back when the probe itself fails", async () => {
      checkHostDirectoryMock.mockRejectedValue(new Error("network"));
      renderDialog("/home/alice/work/omnigent");

      // checkHostDirectory is documented never to throw; if it does, the
      // dialog must still reach the fallback rather than strand an empty field.
      await waitFor(() => expect(input().value).toBe("/Users/alice/repo"));
    });

    it("waits for the probe instead of racing the recent directory ahead of it", async () => {
      let resolveProbe: (problem: string | null) => void = () => {};
      checkHostDirectoryMock.mockReturnValue(
        new Promise((resolve) => {
          resolveProbe = resolve;
        }),
      );
      renderDialog("/home/alice/work/omnigent");

      await waitFor(() => expect(checkHostDirectoryMock).toHaveBeenCalled());
      expect(input().value).toBe("");
      resolveProbe(null);
      await waitFor(() => expect(input().value).toBe("/home/alice/work/omnigent"));
    });

    it("never overwrites a directory the user typed while the probe was pending", async () => {
      let resolveProbe: (problem: string | null) => void = () => {};
      checkHostDirectoryMock.mockReturnValue(
        new Promise((resolve) => {
          resolveProbe = resolve;
        }),
      );
      renderDialog("/home/alice/work/omnigent");

      await waitFor(() => expect(checkHostDirectoryMock).toHaveBeenCalled());
      fireEvent.change(input(), { target: { value: "/srv/other" } });
      resolveProbe(null);
      await waitFor(() => expect(checkHostDirectoryMock).toHaveBeenCalledTimes(1));

      expect(input().value).toBe("/srv/other");
      fireEvent.click(await screen.findByTestId("switch-host-button"));
      await waitFor(() => expect(launchRunnerMock).toHaveBeenCalledTimes(1));
      expect(launchRunnerMock).toHaveBeenCalledWith("host_new", "conv_1", "/srv/other");
    });

    it("does not probe when the session has no directory", async () => {
      renderDialog();

      await waitFor(() => expect(input().value).toBe("/Users/alice/repo"));
      expect(checkHostDirectoryMock).not.toHaveBeenCalled();
    });
  });
});
