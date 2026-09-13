import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import { ImportSessionsPanel } from "./ImportSessionsPanel";
import { useHosts } from "@/hooks/useHosts";
import { importLocalSessions } from "@/lib/sessionsApi";

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

afterEach(() => cleanup());

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
      };
    });

    renderPanel();
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
});
