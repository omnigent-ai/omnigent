import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import { ImportSessionsPanel } from "./ImportSessionsPanel";
import { useHosts } from "@/hooks/useHosts";
import { importLocalSessions, type LocalImportResult } from "@/lib/sessionsApi";
import { bindConversationForTest } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";

vi.mock("@/hooks/useHosts", () => ({ useHosts: vi.fn() }));
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
  }: {
    value: string;
    onValueChange: (v: string) => void;
    children: ReactNode;
  }) => (
    <select value={value} onChange={(e) => onValueChange(e.target.value)}>
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
});

afterEach(() => {
  cleanup();
  conversationRegistry.clear();
});

describe("ImportSessionsPanel", () => {
  it("prompts to start a host when none are online", () => {
    useHostsMock.mockReturnValue({ data: [] } as unknown as ReturnType<typeof useHosts>);
    renderPanel();
    expect(screen.getByTestId("import-no-hosts")).toBeInTheDocument();
    expect(screen.queryByTestId("import-submit")).toBeNull();
  });

  it("imports and lists each new session by title (null title falls back)", async () => {
    useHostsMock.mockReturnValue({
      data: [{ host_id: "host_1", name: "mac-laptop", owner: "alice", status: "online" }],
    } as unknown as ReturnType<typeof useHosts>);
    // Deliver each session through the streaming callback, then resolve with
    // the final tally — mirrors the NDJSON stream the panel consumes live.
    importLocalSessionsMock.mockImplementation(async (_host, _source, _limit, onSession) => {
      onSession?.({ id: "c1", title: "First session" });
      onSession?.({ id: "c2", title: null });
      return {
        imported: 2,
        alreadyImported: 1,
        failed: 0,
        sessions: [
          { id: "c1", title: "First session" },
          { id: "c2", title: null },
        ],
        failures: [],
      };
    });

    renderPanel();
    expect(screen.queryByTestId("import-replace-toggle")).toBeNull();
    fireEvent.click(screen.getByTestId("import-submit"));

    await waitFor(() => expect(screen.getByTestId("import-result")).toBeInTheDocument());
    // Defaults to the online host + "all" harnesses + 25 recent, plus the
    // per-session streaming callback.
    expect(importLocalSessionsMock).toHaveBeenCalledWith("host_1", "all", 25, expect.any(Function));
    expect(screen.getByTestId("import-result").textContent).toContain("Imported 2");
    expect(screen.getByTestId("import-result").textContent).toContain("1 already imported");
    const link1 = screen.getByTestId("import-result-link-c1");
    expect(link1).toHaveTextContent("First session");
    expect(link1).toHaveAttribute("href", "/c/c1");
    // A null title renders the placeholder rather than crashing.
    expect(screen.getByTestId("import-result-link-c2")).toHaveTextContent("Untitled session");
  });

  it("shows a reason for each failed session and retries without re-importing successes", async () => {
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
      };
    });

    renderPanel();
    fireEvent.click(screen.getByTestId("import-submit"));

    await waitFor(() => expect(screen.getByTestId("import-failures")).toBeInTheDocument());
    expect(screen.getByTestId("import-result").textContent).toContain("2 failed");
    const failures = screen.getAllByTestId("import-failure-item");
    expect(failures).toHaveLength(2);
    expect(failures[0]).toHaveTextContent("No visible messages to import.");
    expect(failures[0]).toHaveTextContent("bad-1");

    // Retrying just re-runs the import; server-side dedup skips the successes.
    importLocalSessionsMock.mockClear();
    fireEvent.click(screen.getByTestId("import-retry"));
    await waitFor(() => expect(importLocalSessionsMock).toHaveBeenCalledTimes(1));
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
        false,
      ),
    );
  });

  it("ignores Enter and clicks while an import is already running", async () => {
    useHostsMock.mockReturnValue({
      data: [{ host_id: "host_1", name: "mac-laptop", owner: "alice", status: "online" }],
    } as unknown as ReturnType<typeof useHosts>);
    let finish: (result: LocalImportResult) => void = () => {};
    importLocalSessionsMock.mockImplementation(
      () =>
        new Promise<LocalImportResult>((resolve) => {
          finish = resolve;
        }),
    );

    renderPanel();
    fireEvent.change(screen.getAllByRole("combobox")[1], {
      target: { value: "session" },
    });
    const idInput = screen.getByTestId("import-session-id");
    fireEvent.change(idInput, { target: { value: "session-exact" } });
    fireEvent.keyDown(idInput, { key: "Enter" });
    await waitFor(() => expect(importLocalSessionsMock).toHaveBeenCalledTimes(1));

    fireEvent.keyDown(idInput, { key: "Enter" });
    fireEvent.click(screen.getByTestId("import-submit"));
    expect(importLocalSessionsMock).toHaveBeenCalledTimes(1);

    finish({
      imported: 1,
      alreadyImported: 0,
      failed: 0,
      sessions: [{ id: "c1", title: "Exact session" }],
      failures: [],
    });
    await waitFor(() =>
      expect(screen.getByTestId("import-result")).toHaveTextContent("Imported 1"),
    );
  });

  it("reconciles an exact replacement delivered before a stream error", async () => {
    const partialId = "partial-replacement";
    useHostsMock.mockReturnValue({
      data: [{ host_id: "host_1", name: "mac-laptop", owner: "alice", status: "online" }],
    } as unknown as ReturnType<typeof useHosts>);
    bindConversationForTest(partialId);
    importLocalSessionsMock.mockImplementation(async (_host, _source, _limit, onSession) => {
      onSession?.({ id: partialId, title: "Latest partial snapshot" });
      throw new Error("host disconnected after replacement");
    });

    renderPanel();
    fireEvent.change(screen.getAllByRole("combobox")[1], {
      target: { value: "session" },
    });
    fireEvent.change(screen.getByTestId("import-session-id"), {
      target: { value: "source-session" },
    });
    fireEvent.click(screen.getByTestId("import-replace-toggle"));
    fireEvent.click(screen.getByTestId("import-submit"));
    fireEvent.click(await screen.findByTestId("import-replace-confirm"));

    await waitFor(() => expect(screen.getByTestId("import-error")).toBeInTheDocument());
    expect(screen.getByTestId("import-error")).toHaveTextContent(
      "host disconnected after replacement",
    );
    expect(screen.getByTestId("import-error")).toHaveTextContent("Imported 1 session before");
    expect(screen.getByTestId(`import-result-link-${partialId}`)).toHaveTextContent(
      "Latest partial snapshot",
    );
    expect(conversationRegistry.peek(partialId)).toBeUndefined();
  });

  it("requires confirmation before replacing an exact snapshot", async () => {
    useHostsMock.mockReturnValue({
      data: [{ host_id: "host_1", name: "mac-laptop", owner: "alice", status: "online" }],
    } as unknown as ReturnType<typeof useHosts>);
    importLocalSessionsMock.mockResolvedValue({
      imported: 1,
      alreadyImported: 0,
      failed: 0,
      sessions: [{ id: "c1", title: "Latest snapshot" }],
      failures: [],
    });

    renderPanel();
    fireEvent.change(screen.getAllByRole("combobox")[1], {
      target: { value: "session" },
    });
    fireEvent.change(screen.getByTestId("import-session-id"), {
      target: { value: "session-exact" },
    });
    fireEvent.click(screen.getByTestId("import-replace-toggle"));
    fireEvent.click(screen.getByTestId("import-submit"));

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText(/Omnigent-only changes.*will be removed/)).toBeInTheDocument();
    expect(importLocalSessionsMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("import-replace-cancel"));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(importLocalSessionsMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("import-submit"));
    fireEvent.click(await screen.findByTestId("import-replace-confirm"));
    await waitFor(() =>
      expect(importLocalSessionsMock).toHaveBeenCalledWith(
        "host_1",
        "claude",
        25,
        expect.any(Function),
        "session-exact",
        true,
      ),
    );
  });
});
