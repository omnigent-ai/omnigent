import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import { ImportSessionsPanel } from "./ImportSessionsPanel";
import { useHosts } from "@/hooks/useHosts";
import { useArchiveConversation } from "@/hooks/useConversations";
import { importLocalSessions, type LocalImportResult } from "@/lib/sessionsApi";
import { useSessionImportStore } from "@/store/sessionImport";

vi.mock("@/hooks/useHosts", () => ({ useHosts: vi.fn() }));
vi.mock("@/hooks/useConversations", () => ({ useArchiveConversation: vi.fn() }));
vi.mock("@/lib/sessionsApi", () => ({ importLocalSessions: vi.fn() }));
vi.mock("./HostLabel", () => ({
  HostLabel: ({ host }: { host: { name: string } }) => <span>{host.name}</span>,
}));
// Radix Select uses a portal + pointer events jsdom can't drive; a native
// <select> keeps the option list assertable.
vi.mock("@/components/ui/select", () => ({
  Select: ({
    value,
    onValueChange,
    children,
    disabled,
  }: {
    disabled?: boolean;
    value: string;
    onValueChange: (v: string) => void;
    children: ReactNode;
  }) => (
    <select disabled={disabled} value={value} onChange={(e) => onValueChange(e.target.value)}>
      {children}
    </select>
  ),
  SelectTrigger: ({ children }: { children: ReactNode }) => children,
  SelectValue: () => null,
  SelectContent: ({ children }: { children: ReactNode }) => children,
  SelectItem: ({ value, children }: { value: string; children: ReactNode }) => (
    <option value={value}>{children}</option>
  ),
}));

const useHostsMock = vi.mocked(useHosts);
const importLocalSessionsMock = vi.mocked(importLocalSessions);
const useArchiveConversationMock = vi.mocked(useArchiveConversation);
const unarchiveMutateAsync = vi.fn();

function mockUnarchiveState(state: { isPending?: boolean } = {}) {
  useArchiveConversationMock.mockReturnValue({
    mutateAsync: unarchiveMutateAsync,
    isPending: state.isPending ?? false,
  } as unknown as ReturnType<typeof useArchiveConversation>);
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ImportSessionsPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useHostsMock.mockReset();
  importLocalSessionsMock.mockReset();
  unarchiveMutateAsync.mockReset();
  unarchiveMutateAsync.mockResolvedValue({});
  mockUnarchiveState();
  useSessionImportStore.setState({
    importing: false,
    importedCount: 0,
    request: null,
    result: null,
    error: null,
  });
});

afterEach(() => cleanup());

describe("ImportSessionsPanel", () => {
  it("prompts to start a host when none are online", () => {
    useHostsMock.mockReturnValue({ data: [] } as unknown as ReturnType<typeof useHosts>);
    renderPanel();
    expect(screen.getByTestId("import-no-hosts")).toBeInTheDocument();
    expect(screen.queryByTestId("import-submit")).toBeNull();
  });

  it("keeps a large batch compact and points to the existing session list", async () => {
    useHostsMock.mockReturnValue({
      data: [{ host_id: "host_1", name: "mac-laptop", owner: "alice", status: "online" }],
    } as unknown as ReturnType<typeof useHosts>);
    importLocalSessionsMock.mockImplementation(async (_host, _source, _limit, onSession) => {
      const sessions = Array.from({ length: 35 }, (_, i) => ({
        id: `c${i}`,
        title: `Session ${i}`,
      }));
      sessions.forEach((session) => onSession?.(session));
      return {
        imported: 35,
        alreadyImported: 1,
        failed: 0,
        sessions,
        failures: [],
        alreadyImportedSession: null,
      };
    });
    renderPanel();
    fireEvent.change(screen.getAllByRole("combobox")[3], { target: { value: "50" } });
    fireEvent.click(screen.getByTestId("import-submit"));
    await waitFor(() =>
      expect(screen.getByTestId("import-result")).toHaveTextContent(
        "Imported 35, 1 already imported.",
      ),
    );
    expect(importLocalSessionsMock).toHaveBeenCalledWith("host_1", "all", 50, expect.any(Function));
    expect(screen.queryByRole("list")).toBeNull();
    expect(screen.queryByText("Session 0")).toBeNull();
    expect(screen.getAllByRole("link")).toHaveLength(1);
    expect(screen.getByRole("link", { name: "View sessions" })).toHaveAttribute("href", "/");
  });

  it("collapses reasons for failed sessions and retries without re-importing successes", async () => {
    useHostsMock.mockReturnValue({
      data: [{ host_id: "host_1", name: "mac-laptop", owner: "alice", status: "online" }],
    } as unknown as ReturnType<typeof useHosts>);
    importLocalSessionsMock.mockImplementation(async (_host, _source, _limit, onSession) => {
      onSession?.({ id: "c1", title: "Imported one" });
      return {
        imported: 1,
        alreadyImported: 0,
        failed: 2,
        sessions: [{ id: "c1", title: "Imported one" }],
        failures: [
          { externalSessionId: "bad-1", source: "codex", reason: "No visible messages to import." },
          { externalSessionId: null, source: null, reason: "This session could not be read." },
        ],
        alreadyImportedSession: null,
      };
    });

    renderPanel();
    fireEvent.click(screen.getByTestId("import-submit"));

    await waitFor(() => expect(screen.getByTestId("import-failures")).toBeInTheDocument());
    expect(screen.getByTestId("import-result").textContent).toContain("2 failed");
    const failures = screen.getAllByTestId("import-failure-item");
    expect(failures).toHaveLength(2);
    expect(failures[0].closest("details")).not.toHaveAttribute("open");
    fireEvent.click(screen.getByText("View 2 failed sessions"));
    expect(failures[0].closest("details")).toHaveAttribute("open");
    expect(failures[0]).toHaveTextContent("No visible messages to import.");
    expect(failures[0]).toHaveTextContent("bad-1");

    // Retrying just re-runs the import; server-side dedup skips the successes.
    importLocalSessionsMock.mockClear();
    fireEvent.click(screen.getByTestId("import-retry"));
    await waitFor(() => expect(importLocalSessionsMock).toHaveBeenCalledTimes(1));
  });

  it("keeps an in-flight import running and shows its progress after a remount", async () => {
    useHostsMock.mockReturnValue({
      data: [{ host_id: "host_1", name: "mac-laptop", owner: "alice", status: "online" }],
    } as unknown as ReturnType<typeof useHosts>);
    let finish!: (result: LocalImportResult) => void;
    importLocalSessionsMock.mockImplementation((_host, _source, _limit, onSession) => {
      onSession?.({ id: "c1", title: "First session" });
      return new Promise<LocalImportResult>((resolve) => {
        finish = resolve;
      });
    });

    const first = renderPanel();
    fireEvent.click(screen.getByTestId("import-submit"));
    await waitFor(() =>
      expect(screen.getByTestId("import-progress")).toHaveTextContent(
        "Importing sessions… 1 imported",
      ),
    );

    // Leaving Settings unmounts the panel; the host keeps streaming, and
    // coming back shows the same run rather than a blank form.
    first.unmount();
    renderPanel();
    expect(screen.getByTestId("import-progress")).toHaveTextContent(
      "Importing sessions… 1 imported",
    );
    expect(screen.queryByRole("list")).toBeNull();
    expect(screen.getByTestId("import-progress").querySelector("svg")).toHaveClass("animate-spin");
    for (const control of screen.getAllByRole("combobox")) expect(control).toBeDisabled();
    // A second Import while one is running does not start another stream.
    fireEvent.click(screen.getByTestId("import-submit"));
    expect(importLocalSessionsMock).toHaveBeenCalledTimes(1);

    finish({
      imported: 1,
      alreadyImported: 0,
      failed: 0,
      sessions: [{ id: "c1", title: "First session" }],
      failures: [],
      alreadyImportedSession: null,
    });
    await waitFor(() =>
      expect(screen.getByTestId("import-result")).toHaveTextContent("Imported 1."),
    );
  });

  it("imports one session by harness and ID without listing sessions", async () => {
    useHostsMock.mockReturnValue({
      data: [{ host_id: "host_1", name: "mac-laptop", owner: "alice", status: "online" }],
    } as unknown as ReturnType<typeof useHosts>);
    importLocalSessionsMock.mockImplementation(async (_host, _source, _limit, onSession) => {
      onSession?.({ id: "c1", title: "Exact session" });
      return {
        imported: 1,
        alreadyImported: 0,
        failed: 0,
        sessions: [{ id: "c1", title: "Exact session" }],
        failures: [],
        alreadyImportedSession: null,
      };
    });

    renderPanel();
    fireEvent.change(screen.getAllByRole("combobox")[1], {
      target: { value: "session" },
    });

    expect(screen.queryByTestId("import-limit-select")).toBeNull();
    const idInput = screen.getByTestId("import-session-id");
    expect(screen.getByTestId("import-submit")).toBeDisabled();
    fireEvent.change(idInput, { target: { value: "  session-exact  " } });
    fireEvent.click(screen.getByTestId("import-submit"));

    await waitFor(() =>
      expect(importLocalSessionsMock).toHaveBeenCalledWith(
        "host_1",
        "claude",
        25,
        expect.any(Function),
        "session-exact",
      ),
    );
  });

  async function importArchivedExactSession(canUnarchive = true) {
    useHostsMock.mockReturnValue({
      data: [{ host_id: "host_1", name: "mac-laptop", owner: "alice", status: "online" }],
    } as unknown as ReturnType<typeof useHosts>);
    importLocalSessionsMock.mockResolvedValue({
      imported: 0,
      alreadyImported: 1,
      failed: 0,
      sessions: [],
      failures: [],
      alreadyImportedSession: {
        id: "c9",
        title: "Planning thread",
        archived: true,
        canUnarchive,
      },
    });

    const view = renderPanel();
    fireEvent.change(screen.getAllByRole("combobox")[1], { target: { value: "session" } });
    fireEvent.change(screen.getByTestId("import-session-id"), {
      target: { value: "harness-9" },
    });
    fireEvent.click(screen.getByTestId("import-submit"));
    return { view, notice: await screen.findByTestId("import-existing-session") };
  }

  it("links an already-imported exact session and restores it when archived", async () => {
    const { view, notice } = await importArchivedExactSession();
    expect(screen.getByTestId("import-result")).toHaveTextContent(
      "Imported 0, 1 already imported.",
    );
    expect(notice).toHaveTextContent("Already imported as “Planning thread” (archived).");
    expect(screen.getByTestId("import-existing-session-open")).toHaveAttribute("href", "/c/c9");
    fireEvent.click(screen.getByTestId("import-existing-session-unarchive"));
    expect(unarchiveMutateAsync).toHaveBeenCalledWith({ id: "c9", archived: false });

    expect(await screen.findByTestId("import-existing-session-restored")).toBeInTheDocument();
    expect(screen.getByTestId("import-existing-session")).not.toHaveTextContent("(archived)");
    expect(screen.queryByTestId("import-existing-session-unarchive")).toBeNull();

    // The import result outlives the panel, so coming back to Settings stays current.
    view.unmount();
    renderPanel();
    expect(screen.getByTestId("import-existing-session")).not.toHaveTextContent("(archived)");
    expect(screen.queryByTestId("import-existing-session-unarchive")).toBeNull();
  });

  it("keeps offering Unarchive when restoring the existing session fails", async () => {
    unarchiveMutateAsync.mockRejectedValue(new Error("server down"));
    const { notice } = await importArchivedExactSession();
    fireEvent.click(screen.getByTestId("import-existing-session-unarchive"));
    await waitFor(() => expect(unarchiveMutateAsync).toHaveBeenCalledTimes(1));
    // Let the rejected restore chain settle before checking nothing changed.
    await act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 0);
      });
    });

    expect(notice).toHaveTextContent("(archived)");
    expect(screen.getByTestId("import-existing-session-unarchive")).toBeInTheDocument();
    expect(screen.queryByTestId("import-existing-session-restored")).toBeNull();
    expect(useSessionImportStore.getState().result?.alreadyImportedSession?.archived).toBe(true);
  });

  it("disables Unarchive while the restore is in flight", async () => {
    mockUnarchiveState({ isPending: true });
    await importArchivedExactSession();
    expect(screen.getByTestId("import-existing-session-unarchive")).toBeDisabled();
  });

  it("does not offer Unarchive to a reader who cannot unarchive the session", async () => {
    const { notice } = await importArchivedExactSession(false);
    expect(notice).toHaveTextContent("Already imported as “Planning thread” (archived).");
    expect(screen.getByTestId("import-existing-session-open")).toHaveAttribute("href", "/c/c9");
    expect(screen.queryByTestId("import-existing-session-unarchive")).toBeNull();
  });

  it("does not offer Unarchive for an already-imported active session", async () => {
    useHostsMock.mockReturnValue({
      data: [{ host_id: "host_1", name: "mac-laptop", owner: "alice", status: "online" }],
    } as unknown as ReturnType<typeof useHosts>);
    importLocalSessionsMock.mockResolvedValue({
      imported: 0,
      alreadyImported: 1,
      failed: 0,
      sessions: [],
      failures: [],
      alreadyImportedSession: { id: "c3", title: null, archived: false, canUnarchive: false },
    });

    renderPanel();
    fireEvent.change(screen.getAllByRole("combobox")[1], { target: { value: "session" } });
    fireEvent.change(screen.getByTestId("import-session-id"), { target: { value: "harness-3" } });
    fireEvent.click(screen.getByTestId("import-submit"));

    const notice = await screen.findByTestId("import-existing-session");
    expect(notice).toHaveTextContent("Already imported as “Untitled session”.");
    expect(screen.getByTestId("import-existing-session-open")).toHaveAttribute("href", "/c/c3");
    expect(screen.queryByTestId("import-existing-session-unarchive")).toBeNull();
  });
});

it("Retry failed preserves the original request after leaving Settings", async () => {
  useHostsMock.mockReturnValue({
    data: [
      { host_id: "host_1", name: "first-machine", owner: "alice", status: "online" },
      { host_id: "host_2", name: "selected-machine", owner: "alice", status: "online" },
    ],
  } as unknown as ReturnType<typeof useHosts>);
  let finish!: (result: LocalImportResult) => void;
  const failed: LocalImportResult = {
    imported: 0,
    alreadyImported: 0,
    failed: 1,
    sessions: [],
    failures: [
      { externalSessionId: "thread-to-retry", source: "codex", reason: "Could not read session" },
    ],
    alreadyImportedSession: null,
  };
  importLocalSessionsMock.mockImplementation(
    () =>
      new Promise<LocalImportResult>((resolve) => {
        finish = resolve;
      }),
  );
  const first = renderPanel();
  fireEvent.change(screen.getAllByRole("combobox")[0], { target: { value: "host_2" } });
  fireEvent.change(screen.getAllByRole("combobox")[3], { target: { value: "50" } });
  fireEvent.change(screen.getAllByRole("combobox")[1], { target: { value: "session" } });
  fireEvent.change(screen.getAllByRole("combobox")[2], { target: { value: "codex" } });
  fireEvent.change(screen.getByTestId("import-session-id"), {
    target: { value: "thread-to-retry" },
  });
  fireEvent.click(screen.getByTestId("import-submit"));
  expect(importLocalSessionsMock).toHaveBeenLastCalledWith(
    "host_2",
    "codex",
    50,
    expect.any(Function),
    "thread-to-retry",
  );
  first.unmount();
  finish(failed);
  await waitFor(() => expect(useSessionImportStore.getState().importing).toBe(false));
  importLocalSessionsMock.mockResolvedValue(failed);
  renderPanel();
  await waitFor(() => expect(screen.getByTestId("import-retry")).toBeInTheDocument());
  expect(screen.getAllByRole("combobox")[0]).toHaveValue("host_2");
  expect(screen.getAllByRole("combobox")[2]).toHaveValue("codex");
  expect(screen.getByTestId("import-session-id")).toHaveValue("thread-to-retry");
  fireEvent.change(screen.getAllByRole("combobox")[0], { target: { value: "host_1" } });
  fireEvent.click(screen.getByTestId("import-retry"));
  await waitFor(() => expect(importLocalSessionsMock).toHaveBeenCalledTimes(2));
  expect(importLocalSessionsMock).toHaveBeenLastCalledWith(
    "host_2",
    "codex",
    50,
    expect.any(Function),
    "thread-to-retry",
  );
});
