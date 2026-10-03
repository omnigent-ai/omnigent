import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import { ImportSessionsPanel } from "./ImportSessionsPanel";
import { useHosts } from "@/hooks/useHosts";
import {
  ApiError,
  importLocalSessions,
  type ImportErrorInfo,
  type ImportFailureRef,
  type LocalImportResult,
} from "@/lib/sessionsApi";
import type * as SessionsApiModule from "@/lib/sessionsApi";

vi.mock("@/hooks/useHosts", () => ({ useHosts: vi.fn() }));
// Only the network call is faked; ApiError / importErrorFromException stay real
// so the panel's mapping of thrown errors is exercised.
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  importLocalSessions: vi.fn(),
}));
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
  const invalidateSpy = vi.spyOn(client, "invalidateQueries");
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ImportSessionsPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, invalidateSpy };
}

function withOnlineHost() {
  useHostsMock.mockReturnValue({
    data: [{ host_id: "host_1", name: "mac-laptop", owner: "alice", status: "online" }],
  } as unknown as ReturnType<typeof useHosts>);
}

function result(overrides: Partial<LocalImportResult> = {}): LocalImportResult {
  return {
    imported: 0,
    alreadyImported: 0,
    failed: 0,
    sessions: [],
    failures: [],
    total: null,
    complete: true,
    error: null,
    ...overrides,
  };
}

function failure(overrides: Partial<ImportFailureRef> = {}): ImportFailureRef {
  return {
    externalSessionId: "ext-1",
    source: "claude",
    reason: "Could not import.",
    code: null,
    retryable: true,
    errorId: null,
    ...overrides,
  };
}

function importError(overrides: Partial<ImportErrorInfo> = {}): ImportErrorInfo {
  return {
    code: null,
    message: "Import stopped.",
    retryable: true,
    errorId: null,
    fixCommands: [],
    hostName: null,
    ...overrides,
  };
}

function hostsInvalidated(spy: ReturnType<typeof renderPanel>["invalidateSpy"]): boolean {
  return spy.mock.calls.some(
    (call) => JSON.stringify(call[0]?.queryKey) === JSON.stringify(["hosts"]),
  );
}

async function runImport(res: LocalImportResult) {
  withOnlineHost();
  importLocalSessionsMock.mockResolvedValue(res);
  const view = renderPanel();
  fireEvent.click(screen.getByTestId("import-submit"));
  await waitFor(() => expect(screen.getByTestId("import-submit")).not.toBeDisabled());
  await waitFor(() => expect(screen.queryByTestId("import-progress")).toBeNull());
  return view;
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
      return result({
        imported: 2,
        alreadyImported: 1,
        sessions: [
          { id: "c1", title: "First session" },
          { id: "c2", title: null },
        ],
      });
    });

    renderPanel();
    fireEvent.click(screen.getByTestId("import-submit"));

    await waitFor(() => expect(screen.getByTestId("import-result")).toBeInTheDocument());
    // Defaults to the online host + "all" harnesses + 25 recent, plus the
    // per-session streaming and progress callbacks.
    expect(importLocalSessionsMock).toHaveBeenCalledWith(
      "host_1",
      "all",
      25,
      expect.any(Function),
      undefined,
      { onProgress: expect.any(Function) },
    );
    expect(screen.getByTestId("import-result")).toHaveTextContent(
      "Imported 2 · 1 already imported",
    );
    // Nothing failed and nothing is retryable: no retry button, no banner.
    expect(screen.queryByTestId("import-retry")).toBeNull();
    expect(screen.queryByTestId("import-error")).toBeNull();
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
      // Failures from a server that predates codes: retryable by default.
      return result({
        imported: 1,
        failed: 2,
        sessions: [{ id: "c1", title: "Imported one" }],
        failures: [
          failure({
            externalSessionId: "bad-1",
            source: "codex",
            reason: "No visible messages to import.",
          }),
          failure({
            externalSessionId: null,
            source: null,
            reason: "This session could not be read.",
          }),
        ],
      });
    });

    renderPanel();
    fireEvent.click(screen.getByTestId("import-submit"));

    await waitFor(() => expect(screen.getByTestId("import-failures")).toBeInTheDocument());
    expect(screen.getByTestId("import-result")).toHaveTextContent("Imported 1 · 2 failed");
    const failures = screen.getAllByTestId("import-failure-item");
    expect(failures).toHaveLength(2);
    expect(failures[0]).toHaveTextContent("No visible messages to import.");
    expect(failures[0]).toHaveTextContent("Codex · bad-1");
    // No code / error id from an old server → no empty details disclosure.
    expect(screen.queryByTestId("import-failure-details")).toBeNull();
    expect(screen.getByTestId("import-retry")).toHaveTextContent("Retry failed (2)");

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
      return result({ imported: 1, sessions: [{ id: "c1", title: "Exact session" }] });
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
        { onProgress: expect.any(Function) },
      ),
    );
  });
});

describe("ImportSessionsPanel import outcomes", () => {
  it.each([
    {
      imported: 12,
      alreadyImported: 3,
      failed: 2,
      text: "Imported 12 · 3 already imported · 2 failed",
    },
    { imported: 4, alreadyImported: 0, failed: 0, text: "Imported 4" },
    { imported: 0, alreadyImported: 0, failed: 2, text: "2 failed" },
    {
      imported: 0,
      alreadyImported: 3,
      failed: 0,
      text: "Nothing new to import · 3 already imported",
    },
    { imported: 0, alreadyImported: 0, failed: 0, text: "No sessions to import." },
  ])("summarizes $imported/$alreadyImported/$failed", async ({ text, ...counts }) => {
    await runImport(result(counts));
    expect(screen.getByTestId("import-result").textContent).toBe(text);
  });

  it("shows live progress from progress events", async () => {
    withOnlineHost();
    let onProgress: ((p: { done: number; total: number | null }) => void) | undefined;
    let finish: (r: LocalImportResult) => void = () => {};
    importLocalSessionsMock.mockImplementation((_h, _s, _l, _onSession, _id, options) => {
      onProgress = options?.onProgress;
      return new Promise<LocalImportResult>((resolve) => {
        finish = resolve;
      });
    });
    renderPanel();
    fireEvent.click(screen.getByTestId("import-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("import-progress")).toHaveTextContent("Importing…"),
    );
    act(() => onProgress?.({ done: 7, total: 20 }));
    expect(screen.getByTestId("import-progress")).toHaveTextContent("Importing 7 of 20…");
    // A host that doesn't know the total yet: a running count instead.
    act(() => onProgress?.({ done: 3, total: null }));
    expect(screen.getByTestId("import-progress")).toHaveTextContent("Importing… 3 so far");

    await act(async () => finish(result({ imported: 3, total: 20 })));
    await waitFor(() => expect(screen.queryByTestId("import-progress")).toBeNull());
    expect(screen.getByTestId("import-result")).toHaveTextContent("Imported 3");
  });

  it("keeps the partial tally, sessions and failures when the import stops with an error", async () => {
    withOnlineHost();
    importLocalSessionsMock.mockImplementation(async (_h, _s, _l, onSession) => {
      onSession?.({ id: "c1", title: "One" });
      onSession?.({ id: "c2", title: "Two" });
      return result({
        imported: 2,
        failed: 1,
        sessions: [
          { id: "c1", title: "One" },
          { id: "c2", title: "Two" },
        ],
        failures: [
          failure({
            reason: "Saving this session timed out (812 messages).",
            code: "session_save_timeout",
            retryable: true,
          }),
        ],
        complete: false,
        error: importError({
          code: "host_disconnected",
          message:
            "mac-laptop disconnected after 3 of 10 sessions. Reconnect it (run `omnigent host`) and import again — sessions already imported are skipped.",
          errorId: "err_disc",
          hostName: "mac-laptop",
        }),
      });
    });
    renderPanel();
    fireEvent.click(screen.getByTestId("import-submit"));

    await waitFor(() => expect(screen.getByTestId("import-error")).toBeInTheDocument());
    expect(screen.getByTestId("import-result")).toHaveTextContent("Imported 2 · 1 failed");
    expect(screen.getByTestId("import-result-link-c1")).toHaveTextContent("One");
    expect(screen.getByTestId("import-result-link-c2")).toHaveTextContent("Two");
    expect(screen.getAllByTestId("import-failure-item")).toHaveLength(1);
    expect(screen.getByTestId("import-error-message")).toHaveTextContent(
      "mac-laptop disconnected after 3 of 10 sessions.",
    );
    // Backticked commands render as copyable inline code.
    expect(screen.getByTestId("import-error-message").querySelector("code")).toHaveTextContent(
      "omnigent host",
    );
    expect(screen.getByTestId("import-retry")).toHaveTextContent("Retry failed (1)");
  });

  it.each([
    {
      code: "host_disconnected",
      message: "mac-laptop disconnected after 3 of 10 sessions.",
      retryable: true,
      refreshesHosts: true,
    },
    {
      code: "host_unresponsive",
      message: "mac-laptop stopped responding after 3 of 10 sessions (nothing for 30 s).",
      retryable: true,
      refreshesHosts: true,
    },
    {
      code: "time_limit_reached",
      message: "Imported 40 of 100 before the time limit — run it again to continue.",
      retryable: true,
      refreshesHosts: false,
    },
    {
      code: "stream_interrupted",
      message: "The connection to Omnigent dropped after 3 sessions. Import again to continue.",
      retryable: true,
      refreshesHosts: false,
    },
    {
      code: "internal",
      message: "Import stopped because of an internal error. Try again.",
      retryable: true,
      refreshesHosts: false,
    },
    {
      code: "host_python_missing_sqlite",
      message: "Your machine's Python was built without SQLite.",
      retryable: false,
      refreshesHosts: false,
    },
    // A server that predates codes: message verbatim, offered for retry.
    { code: null, message: "host stalled mid-import", retryable: true, refreshesHosts: false },
  ])("renders a $code stream error", async ({ code, message, retryable, refreshesHosts }) => {
    const { invalidateSpy } = await runImport(
      result({ complete: false, error: importError({ code, message, retryable }) }),
    );

    const banner = screen.getByTestId("import-error");
    expect(banner).toHaveAttribute("data-import-code", code ?? "");
    expect(screen.getByTestId("import-error-message")).toHaveTextContent(message);
    // Nothing processed: the banner alone, no "Imported 0" line.
    expect(screen.queryByTestId("import-result")).toBeNull();
    if (retryable) {
      expect(screen.getByTestId("import-retry")).toHaveTextContent("Import again");
    } else {
      expect(screen.queryByTestId("import-retry")).toBeNull();
    }
    expect(hostsInvalidated(invalidateSpy)).toBe(refreshesHosts);
  });

  it.each([
    {
      name: "host_offline 409",
      error: new ApiError(
        "mac-laptop is offline (last seen 5 minutes ago). Start `omnigent host` on that machine, then try again.",
        409,
        "conflict",
        { importCode: "host_offline", retryable: true, details: { host_name: "mac-laptop" } },
      ),
      code: "host_offline",
      retryable: true,
      refreshesHosts: true,
    },
    {
      name: "host_unreachable 409",
      error: new ApiError(
        "mac-laptop isn't responding (nothing heard for 2 minutes).",
        409,
        "conflict",
        { importCode: "host_unreachable", retryable: true },
      ),
      code: "host_unreachable",
      retryable: true,
      refreshesHosts: true,
    },
    {
      name: "422 validation",
      error: new ApiError("limit: Input should be less than or equal to 100", 422, null),
      code: "invalid_request",
      retryable: false,
      refreshesHosts: false,
    },
    {
      name: "old-server 409 without import_code",
      error: new ApiError("Host is offline.", 409, "conflict"),
      code: null,
      retryable: true,
      refreshesHosts: true,
    },
    {
      name: "old-server 400",
      error: new ApiError("Bad request.", 400, "invalid_input"),
      code: null,
      retryable: false,
      refreshesHosts: false,
    },
  ])("renders a pre-stream $name", async ({ error, code, retryable, refreshesHosts }) => {
    withOnlineHost();
    importLocalSessionsMock.mockRejectedValue(error);
    const { invalidateSpy } = renderPanel();
    fireEvent.click(screen.getByTestId("import-submit"));

    await waitFor(() => expect(screen.getByTestId("import-error")).toBeInTheDocument());
    expect(screen.getByTestId("import-error")).toHaveAttribute("data-import-code", code ?? "");
    expect(screen.getByTestId("import-error-message")).toHaveTextContent(
      error.message.replace(/`/g, ""),
    );
    expect(screen.queryByTestId("import-retry") !== null).toBe(retryable);
    await waitFor(() => expect(hostsInvalidated(invalidateSpy)).toBe(refreshesHosts));
  });

  it.each([
    { code: "session_too_large", retryable: false },
    { code: "session_unreadable", retryable: false },
    { code: "session_save_timeout", retryable: true },
    { code: "encryption_unavailable", retryable: true },
    { code: "internal", retryable: true },
  ])("renders a $code failed session", async ({ code, retryable }) => {
    await runImport(
      result({
        imported: 1,
        failed: 1,
        failures: [
          failure({
            externalSessionId: "sess-9",
            source: "codex",
            reason: `Reason for ${code}.`,
            code,
            retryable,
            errorId: "err_row",
          }),
        ],
      }),
    );

    const row = screen.getByTestId("import-failure-item");
    expect(row).toHaveTextContent(`Reason for ${code}.`);
    expect(row).toHaveTextContent("Codex · sess-9");
    const details = screen.getByTestId("import-failure-details");
    expect(details).toHaveTextContent(code);
    expect(details).toHaveTextContent("err_row");
    if (retryable) {
      expect(screen.getByTestId("import-retry")).toHaveTextContent("Retry failed (1)");
    } else {
      expect(screen.queryByTestId("import-retry")).toBeNull();
    }
  });

  it("counts only retryable failures in the retry button", async () => {
    await runImport(
      result({
        failed: 3,
        failures: [
          failure({ externalSessionId: "a", code: "session_too_large", retryable: false }),
          failure({ externalSessionId: "b", code: "session_save_timeout", retryable: true }),
          failure({ externalSessionId: "c", code: "encryption_unavailable", retryable: true }),
        ],
      }),
    );
    expect(screen.getByTestId("import-retry")).toHaveTextContent("Retry failed (2)");
  });

  it("offers a plain re-run when only the whole-import error is retryable", async () => {
    await runImport(
      result({
        imported: 5,
        failed: 1,
        failures: [failure({ code: "session_too_large", retryable: false })],
        complete: false,
        error: importError({
          code: "time_limit_reached",
          message: "Imported 6 of 30 before the time limit.",
        }),
      }),
    );
    expect(screen.getByTestId("import-retry")).toHaveTextContent("Import again");
  });

  it("shows no retry when nothing is retryable", async () => {
    await runImport(
      result({
        failed: 1,
        failures: [failure({ code: "session_too_large", retryable: false })],
        complete: false,
        error: importError({ code: "host_python_missing_sqlite", retryable: false }),
      }),
    );
    expect(screen.queryByTestId("import-retry")).toBeNull();
  });

  it("renders fix_commands as copyable code blocks and the error id under Details", async () => {
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await runImport(
      result({
        complete: false,
        error: importError({
          code: "host_python_missing_sqlite",
          retryable: false,
          message:
            "Your machine's Python was built without SQLite (`_sqlite3` is missing), so `omnigent host` can't read local sessions.",
          errorId: "err_sqlite",
          fixCommands: [
            { label: "macOS", command: "brew install sqlite && pyenv install --force 3.12" },
            {
              label: "Linux",
              command: "sudo apt-get install libsqlite3-dev && pyenv install --force 3.12",
            },
            { label: null, command: "omnigent host" },
          ],
        }),
      }),
    );

    expect(screen.getByTestId("import-fix-0-command")).toHaveTextContent(
      "brew install sqlite && pyenv install --force 3.12",
    );
    // The label sits outside the code block, so Copy yields a runnable command.
    expect(screen.getByTestId("import-fix-0-label")).toHaveTextContent("macOS");
    expect(screen.getByTestId("import-fix-0-command")).not.toHaveTextContent("macOS");
    expect(screen.getByTestId("import-fix-1-label")).toHaveTextContent("Linux");
    expect(screen.queryByTestId("import-fix-2-label")).toBeNull();
    expect(screen.getByTestId("import-fix-1-command")).toHaveTextContent(
      "sudo apt-get install libsqlite3-dev && pyenv install --force 3.12",
    );
    fireEvent.click(screen.getByTestId("import-fix-1-copy"));
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(
        "sudo apt-get install libsqlite3-dev && pyenv install --force 3.12",
      ),
    );
    const details = screen.getByTestId("import-error-details");
    expect(details.tagName).toBe("DETAILS");
    expect(details).not.toHaveAttribute("open");
    expect(details).toHaveTextContent("host_python_missing_sqlite");
    expect(details).toHaveTextContent("err_sqlite");
  });

  it("keeps the tally and banner when the machine goes offline mid-import", async () => {
    let hostsData = [{ host_id: "host_1", name: "mac-laptop", owner: "alice", status: "online" }];
    useHostsMock.mockImplementation(
      () => ({ data: hostsData }) as unknown as ReturnType<typeof useHosts>,
    );
    importLocalSessionsMock.mockImplementation(async (_h, _s, _l, onSession) => {
      onSession?.({ id: "c1", title: "One" });
      // The hosts query refetches as the host drops (poll / host_offline refresh).
      hostsData = [{ ...hostsData[0], status: "offline" }];
      return result({
        imported: 1,
        sessions: [{ id: "c1", title: "One" }],
        complete: false,
        error: importError({
          code: "host_disconnected",
          message: "mac-laptop disconnected after 1 of 5 sessions.",
          hostName: "mac-laptop",
        }),
      });
    });
    const { rerender } = renderPanel();
    fireEvent.click(screen.getByTestId("import-submit"));
    await waitFor(() => expect(screen.getByTestId("import-error")).toBeInTheDocument());
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <ImportSessionsPanel />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(screen.getByTestId("import-no-hosts")).toBeInTheDocument();
    expect(screen.getByTestId("import-error-message")).toHaveTextContent(
      "mac-laptop disconnected after 1 of 5 sessions.",
    );
    expect(screen.getByTestId("import-result")).toHaveTextContent("Imported 1");
    expect(screen.getByTestId("import-result-link-c1")).toHaveTextContent("One");
    // Nothing to re-run on until a machine is back; the notice says how.
    expect(screen.queryByTestId("import-retry")).toBeNull();
    expect(screen.queryByTestId("import-submit")).toBeNull();
  });

  it("shows only the offline notice when nothing was imported yet", () => {
    useHostsMock.mockReturnValue({
      data: [{ host_id: "host_1", name: "mac-laptop", owner: "alice", status: "offline" }],
    } as unknown as ReturnType<typeof useHosts>);
    renderPanel();
    expect(screen.getByTestId("import-no-hosts")).toBeInTheDocument();
    expect(screen.queryByTestId("import-outcome")).toBeNull();
  });

  it.each([
    {
      shape: "S12 time limit",
      code: "time_limit_reached",
      message: "Imported 10 of 14 before the time limit — run it again to continue.",
    },
    {
      shape: "S7 host disconnected",
      code: "host_disconnected",
      message: "mac-laptop disconnected after 10 of 20 sessions.",
    },
  ])(
    "never says 'Nothing new to import' when the run stopped early ($shape)",
    async ({ code, message }) => {
      await runImport(
        result({
          alreadyImported: 10,
          complete: false,
          error: importError({ code, message }),
        }),
      );
      const summary = screen.getByTestId("import-result");
      expect(summary).toHaveTextContent("Imported 0 · 10 already imported · stopped early");
      expect(summary).not.toHaveTextContent("Nothing new");
      expect(screen.getByTestId("import-error-message")).toHaveTextContent(message);
    },
  );

  it("marks a partial tally as stopped early", async () => {
    await runImport(
      result({
        imported: 3,
        alreadyImported: 2,
        complete: false,
        error: importError({ code: "time_limit_reached" }),
      }),
    );
    expect(screen.getByTestId("import-result")).toHaveTextContent(
      "Imported 3 · 2 already imported · stopped early",
    );
  });

  it("names the machine when a wrong_replica outlives the re-address", async () => {
    withOnlineHost();
    importLocalSessionsMock.mockRejectedValue(
      new ApiError("host is on another replica", 400, "wrong_replica"),
    );
    renderPanel();
    fireEvent.click(screen.getByTestId("import-submit"));
    await waitFor(() => expect(screen.getByTestId("import-error")).toBeInTheDocument());
    expect(screen.getByTestId("import-error-message")).toHaveTextContent(
      "Couldn't reach “mac-laptop”'s connection. Try again in a few seconds.",
    );
    expect(screen.getByTestId("import-error")).toHaveAttribute(
      "data-import-code",
      "host_unreachable",
    );
    expect(screen.getByTestId("import-retry")).toHaveTextContent("Import again");
  });

  it("clears the previous error when the import is re-run", async () => {
    withOnlineHost();
    importLocalSessionsMock.mockResolvedValueOnce(
      result({ complete: false, error: importError({ code: "stream_interrupted" }) }),
    );
    importLocalSessionsMock.mockResolvedValueOnce(result({ imported: 2 }));
    renderPanel();
    fireEvent.click(screen.getByTestId("import-submit"));
    await waitFor(() => expect(screen.getByTestId("import-retry")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("import-retry"));

    await waitFor(() =>
      expect(screen.getByTestId("import-result")).toHaveTextContent("Imported 2"),
    );
    expect(screen.queryByTestId("import-error")).toBeNull();
    expect(screen.queryByTestId("import-retry")).toBeNull();
  });
});
