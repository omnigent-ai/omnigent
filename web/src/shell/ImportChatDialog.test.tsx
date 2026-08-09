import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ImportChatDialog } from "./ImportChatDialog";
import { useHosts, type Host } from "@/hooks/useHosts";
import { useHostLocalSessions } from "@/hooks/useHostLocalSessions";
import {
  importHostLocalSession,
  type ImportedSessionResult,
  type ImportSourceId,
  type LocalSessionSummary,
} from "@/lib/localSessionImportApi";

vi.mock("@/hooks/useHosts", () => ({ useHosts: vi.fn() }));
vi.mock("@/hooks/useHostLocalSessions", () => ({ useHostLocalSessions: vi.fn() }));
vi.mock("@/lib/localSessionImportApi", () => ({ importHostLocalSession: vi.fn() }));

const useHostsMock = vi.mocked(useHosts);
const useHostLocalSessionsMock = vi.mocked(useHostLocalSessions);
const importHostLocalSessionMock = vi.mocked(importHostLocalSession);

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

/** A cold `useHosts` cache: the list hasn't arrived yet. */
function setHostsLoading(): void {
  useHostsMock.mockReturnValue({ data: undefined } as ReturnType<typeof useHosts>);
}

// Two Claude rows: one fully populated, one with the nullable title/workspace
// the server passes through when the host couldn't determine them.
const CLAUDE_SESSIONS: LocalSessionSummary[] = [
  {
    source: "claude",
    external_session_id: "abc",
    workspace: "/Users/serena/proj",
    title: "Fix the flaky parser",
    item_count: 12,
    preview: [
      { role: "user", text: "why is the parser flaky?" },
      { role: "assistant", text: "Let me look at the tokenizer." },
    ],
  },
  {
    source: "claude",
    external_session_id: "def",
    workspace: null,
    title: null,
    item_count: 3,
    preview: [],
  },
];

const CODEX_SESSIONS: LocalSessionSummary[] = [
  {
    source: "codex",
    external_session_id: "xyz",
    workspace: "/Users/serena/api",
    title: "Add retries",
    item_count: 7,
    preview: [{ role: "user", text: "retry the 429s" }],
  },
];

type LocalSessionsResult = ReturnType<typeof useHostLocalSessions>;

function queryResult(
  data: LocalSessionSummary[] | undefined,
  extra: { isLoading?: boolean; error?: Error | null } = {},
): LocalSessionsResult {
  const error = extra.error ?? null;
  return {
    data,
    isLoading: extra.isLoading ?? false,
    isError: error !== null,
    error,
  } as unknown as LocalSessionsResult;
}

/** Serve each source its own rows, so a source switch is observable. */
function setLocalSessionsBySource(
  bySource: Partial<Record<ImportSourceId, LocalSessionsResult>>,
): void {
  useHostLocalSessionsMock.mockImplementation(
    (_hostId, source) => bySource[source] ?? queryResult([]),
  );
}

function renderDialog(props: { defaultHostId?: string | null } = {}): {
  onImported: ReturnType<typeof vi.fn>;
  onOpenChange: ReturnType<typeof vi.fn>;
} {
  const onImported = vi.fn();
  const onOpenChange = vi.fn();
  render(
    <ImportChatDialog
      open
      onOpenChange={onOpenChange}
      defaultHostId={props.defaultHostId ?? null}
      onImported={onImported}
    />,
  );
  return { onImported, onOpenChange };
}

beforeEach(() => {
  useHostsMock.mockReset();
  useHostLocalSessionsMock.mockReset();
  importHostLocalSessionMock.mockReset();
  setHosts([host()]);
  setLocalSessionsBySource({
    claude: queryResult(CLAUDE_SESSIONS),
    codex: queryResult(CODEX_SESSIONS),
  });
});

afterEach(cleanup);

describe("ImportChatDialog", () => {
  it("renders recent sessions with title, workspace, and item count", async () => {
    renderDialog();

    const row = await screen.findByTestId("import-chat-recent-row-abc");
    expect(row).toHaveTextContent("Fix the flaky parser");
    expect(row).toHaveTextContent("/Users/serena/proj");
    expect(row).toHaveTextContent("12 items");

    // The host can return a row with no title/workspace; it still lists.
    const untitled = screen.getByTestId("import-chat-recent-row-def");
    expect(untitled).toHaveTextContent("Untitled session");
    expect(untitled).toHaveTextContent("3 items");

    // The list defaults to Claude Code and browses the only online host.
    expect(useHostLocalSessionsMock).toHaveBeenLastCalledWith("host_1", "claude", true);
  });

  it("expanding a row reveals its preview messages", () => {
    renderDialog();

    expect(screen.queryByTestId("import-chat-preview-abc")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("import-chat-expand-abc"));

    const preview = screen.getByTestId("import-chat-preview-abc");
    expect(preview).toHaveTextContent("why is the parser flaky?");
    expect(preview).toHaveTextContent("Let me look at the tokenizer.");
    // Peeking at a preview must not select the row — the full list stays.
    expect(screen.getByTestId("import-chat-recent-row-def")).toBeInTheDocument();
    expect(screen.getByTestId("import-chat-session-id")).toHaveValue("");
  });

  it("selecting a row fills the session id input and collapses the list", () => {
    renderDialog();

    fireEvent.click(screen.getByTestId("import-chat-recent-row-abc"));

    expect(screen.getByTestId("import-chat-session-id")).toHaveValue("abc");
    // Only the picked chat remains on screen.
    expect(screen.getByTestId("import-chat-recent-row-abc")).toBeInTheDocument();
    expect(screen.queryByTestId("import-chat-recent-row-def")).not.toBeInTheDocument();
  });

  it("restores the recent list when the selection is cleared", async () => {
    renderDialog();

    fireEvent.click(await screen.findByTestId("import-chat-recent-row-abc"));
    expect(screen.queryByTestId("import-chat-recent-row-def")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("import-chat-clear-selection"));

    expect(await screen.findByTestId("import-chat-recent-row-def")).toBeInTheDocument();
    expect(screen.getByTestId("import-chat-session-id")).toHaveValue("");
  });

  it("switching source from Claude to Codex refetches and clears the selection", () => {
    renderDialog();

    fireEvent.click(screen.getByTestId("import-chat-recent-row-abc"));
    expect(screen.getByTestId("import-chat-session-id")).toHaveValue("abc");

    fireEvent.click(screen.getByTestId("import-chat-source-codex"));

    // Refetch: the browse hook is re-keyed on the new source.
    expect(useHostLocalSessionsMock).toHaveBeenLastCalledWith("host_1", "codex", true);
    // The Claude pick can't survive into Codex — it isn't a Codex session.
    expect(screen.getByTestId("import-chat-session-id")).toHaveValue("");
    expect(screen.getByTestId("import-chat-recent-row-xyz")).toBeInTheDocument();
    expect(screen.queryByTestId("import-chat-recent-row-abc")).not.toBeInTheDocument();
  });

  it("shows the empty state when the host has no recent chats", () => {
    setLocalSessionsBySource({ claude: queryResult([]), codex: queryResult([]) });
    renderDialog();

    // Copy names the harness the user is browsing, not a generic "no results".
    expect(screen.getByTestId("import-chat-empty")).toHaveTextContent(
      "No recent Claude Code chats on this machine",
    );
    expect(screen.queryByTestId("import-chat-recent-list")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("import-chat-source-codex"));
    expect(screen.getByTestId("import-chat-empty")).toHaveTextContent(
      "No recent Codex chats on this machine",
    );
  });

  it("import posts the selected session and calls onImported with the new id", async () => {
    // Two online hosts, and the composer preselected the second one.
    setHosts([host(), host({ host_id: "host_2", name: "serena-desktop" })]);
    let settle!: (result: ImportedSessionResult) => void;
    importHostLocalSessionMock.mockReturnValue(
      new Promise<ImportedSessionResult>((resolve) => {
        settle = resolve;
      }),
    );
    const { onImported, onOpenChange } = renderDialog({ defaultHostId: "host_2" });

    // Nothing to import until a session id exists.
    expect(screen.getByTestId("import-chat-submit")).toBeDisabled();
    fireEvent.click(screen.getByTestId("import-chat-recent-row-abc"));
    expect(screen.getByTestId("import-chat-submit")).not.toBeDisabled();

    fireEvent.click(screen.getByTestId("import-chat-submit"));

    expect(importHostLocalSessionMock).toHaveBeenCalledWith("host_2", "claude", "abc");
    // In flight: the button locks and shows a spinner.
    const submit = screen.getByTestId("import-chat-submit");
    expect(submit).toBeDisabled();
    expect(within(submit).getByRole("status")).toBeInTheDocument();

    settle({ session_id: "conv_new", status: "imported", item_count: 12 });

    await waitFor(() => expect(onImported).toHaveBeenCalledWith("conv_new"));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("a failed import shows the server message and keeps the dialog open", async () => {
    importHostLocalSessionMock.mockRejectedValue(new Error("already imported as conv_1"));
    const { onImported, onOpenChange } = renderDialog();

    fireEvent.click(screen.getByTestId("import-chat-recent-row-abc"));
    fireEvent.click(screen.getByTestId("import-chat-submit"));

    // The server's own wording, verbatim — never a bare status code.
    await waitFor(() =>
      expect(screen.getByTestId("import-chat-error")).toHaveTextContent(
        "already imported as conv_1",
      ),
    );
    expect(onImported).not.toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalled();
    // Still open and retryable.
    expect(screen.getByTestId("import-chat-dialog")).toBeInTheDocument();
    expect(screen.getByTestId("import-chat-submit")).not.toBeDisabled();
  });

  it("with no online hosts, the controls are disabled and a connect hint shows", () => {
    // An offline host plus a server-managed sandbox host: neither is importable.
    setHosts([
      host({ status: "offline" }),
      host({ host_id: "host_sb", name: "sandbox", sandbox_provider: "modal" }),
    ]);
    renderDialog();

    expect(screen.getByText("Connect a machine to import chats")).toBeInTheDocument();
    expect(screen.getByTestId("import-chat-host-select")).toBeDisabled();
    expect(screen.getByTestId("import-chat-source-claude")).toBeDisabled();
    expect(screen.getByTestId("import-chat-source-codex")).toBeDisabled();
    expect(screen.getByTestId("import-chat-submit")).toBeDisabled();
    // No host to browse, so neither the list nor the empty state renders.
    expect(screen.queryByTestId("import-chat-recent-list")).not.toBeInTheDocument();
    expect(screen.queryByTestId("import-chat-empty")).not.toBeInTheDocument();
    expect(useHostLocalSessionsMock).toHaveBeenLastCalledWith(null, "claude", true);
    // The manual session-id path the CLI supports stays open regardless.
    expect(screen.getByTestId("import-chat-session-id")).not.toBeDisabled();
  });

  it("waits for the host list before telling the user to connect a machine", () => {
    // A cold hosts cache is not "no machines" — showing the connect hint here
    // instructs the user to set up a machine they may already have connected.
    setHostsLoading();
    renderDialog();

    expect(screen.queryByText("Connect a machine to import chats")).not.toBeInTheDocument();
    expect(screen.getByText("Loading machines…")).toBeInTheDocument();
    // Still nothing to act on until the list lands, so the controls stay locked.
    expect(screen.getByTestId("import-chat-host-select")).toBeDisabled();
    expect(screen.getByTestId("import-chat-source-claude")).toBeDisabled();
    expect(screen.getByTestId("import-chat-submit")).toBeDisabled();
  });

  it("shows a loading state, not an empty one, while the recent chats load", () => {
    setLocalSessionsBySource({ claude: queryResult(undefined, { isLoading: true }) });
    renderDialog();

    expect(screen.getByText("Loading recent chats…")).toBeInTheDocument();
    // Flashing "no recent chats" mid-load would misreport the machine.
    expect(screen.queryByTestId("import-chat-empty")).not.toBeInTheDocument();
    expect(screen.queryByTestId("import-chat-recent-list")).not.toBeInTheDocument();
  });

  it("surfaces the server's message when the recent chats can't be listed", () => {
    setLocalSessionsBySource({
      claude: queryResult(undefined, { error: new Error("host is offline") }),
    });
    renderDialog();

    expect(screen.getByTestId("import-chat-error")).toHaveTextContent("host is offline");
    expect(screen.queryByTestId("import-chat-recent-list")).not.toBeInTheDocument();
    expect(screen.queryByTestId("import-chat-empty")).not.toBeInTheDocument();

    // A failed browse must not close off the manual path: a pasted id still imports.
    fireEvent.change(screen.getByTestId("import-chat-session-id"), { target: { value: "abc" } });
    expect(screen.getByTestId("import-chat-submit")).not.toBeDisabled();
  });
});
