import type { ReactNode } from "react";
import type * as WorkspacePickerModule from "./WorkspacePicker";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { SwitchHostDialog } from "./SwitchHostDialog";
import { useHosts } from "@/hooks/useHosts";
import { useHostFilesystem } from "@/hooks/useHostFilesystem";
import { launchRunner, updateSession } from "@/lib/sessionsApi";

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
vi.mock("@/hooks/useHostFilesystem", () => ({ useHostFilesystem: vi.fn() }));
vi.mock("@/hooks/useRecentWorkspaces", () => ({
  useRecentWorkspaces: () => ({ recent: ["/Users/alice/repo"], addRecent: vi.fn() }),
}));
vi.mock("@/lib/sessionsApi", () => ({ launchRunner: vi.fn(), updateSession: vi.fn() }));
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
const launchRunnerMock = vi.mocked(launchRunner);
const updateSessionMock = vi.mocked(updateSession);

function renderDialog() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <SwitchHostDialog open onOpenChange={() => {}} sessionId="conv_1" currentHostId="host_old" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useHostsMock.mockReset();
  useHostFilesystemMock.mockReset();
  launchRunnerMock.mockReset();
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
});

afterEach(() => cleanup());

describe("SwitchHostDialog", () => {
  it("releases the runner and the model override before launching on the new host", async () => {
    renderDialog();

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
});
