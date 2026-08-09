import type * as ImportsApiModule from "@/lib/importsApi";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ImportChatDialog } from "./ImportChatDialog";
import { importLocalSession, listLocalImportSessions } from "@/lib/importsApi";
import { ApiError } from "@/lib/sessionsApi";

const navigateMock = vi.fn();
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigateMock }));
vi.mock("@/lib/importsApi", async (importActual) => ({
  ...(await importActual<typeof ImportsApiModule>()),
  listLocalImportSessions: vi.fn(),
  importLocalSession: vi.fn(),
}));

const listMock = vi.mocked(listLocalImportSessions);
const importMock = vi.mocked(importLocalSession);

const CLAUDE_SESSIONS = [
  {
    sessionId: "claude-1",
    title: "inspect TODO.md",
    workspace: "/Users/dev/omnigent",
    itemCount: 12,
    modifiedAt: 1_700_000_000,
  },
  {
    sessionId: "claude-2",
    title: null,
    workspace: null,
    itemCount: 3,
    modifiedAt: null,
  },
];

function renderDialog() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidateSpy = vi.spyOn(client, "invalidateQueries");
  const utils = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ImportChatDialog open onOpenChange={vi.fn()} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...utils, invalidateSpy };
}

/** Open the Radix harness <Select> (mirrors ForkSessionDialog.test). */
function openSourceSelect(): void {
  const trigger = screen.getByTestId("import-chat-source-select");
  fireEvent.pointerDown(trigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
  fireEvent.click(trigger);
}

beforeEach(() => {
  navigateMock.mockReset();
  listMock.mockReset();
  listMock.mockResolvedValue(CLAUDE_SESSIONS);
  importMock.mockReset();
  importMock.mockResolvedValue({ sessionId: "conv_imported", itemCount: 12 });
});

afterEach(cleanup);

describe("ImportChatDialog", () => {
  it("lists this machine's recent Claude Code chats with their workspace and size", async () => {
    renderDialog();

    await waitFor(() => expect(listMock).toHaveBeenCalledWith("claude", 20));
    const row = await screen.findByTestId("import-chat-session-claude-1");
    expect(row).toHaveTextContent("inspect TODO.md");
    // Only the directory name shows; the full path stays available on hover.
    expect(row).toHaveTextContent("omnigent");
    expect(row).toHaveTextContent("12");
    // A title-less transcript still has to be pickable, so it falls back to its id.
    expect(await screen.findByTestId("import-chat-session-claude-2")).toHaveTextContent("claude-2");
  });

  it("refetches for the picked harness and drops the previous selection", async () => {
    renderDialog();

    fireEvent.click(await screen.findByTestId("import-chat-session-claude-1"));
    expect(screen.getByTestId("import-chat-selected")).toBeInTheDocument();

    listMock.mockResolvedValue([]);
    openSourceSelect();
    fireEvent.click(screen.getByTestId("import-chat-source-option-codex"));

    await waitFor(() => expect(listMock).toHaveBeenCalledWith("codex", 20));
    // The selection belonged to the old harness — keeping it would import the
    // wrong transcript on submit.
    expect(screen.queryByTestId("import-chat-selected")).not.toBeInTheDocument();
    expect(await screen.findByTestId("import-chat-empty")).toHaveTextContent(
      "No recent Codex chats found on this machine.",
    );
  });

  it("keeps Import disabled until a chat is picked and restores the list on clear", async () => {
    renderDialog();

    expect(screen.getByTestId("import-chat-submit")).toBeDisabled();
    fireEvent.click(await screen.findByTestId("import-chat-session-claude-1"));
    expect(screen.getByTestId("import-chat-submit")).not.toBeDisabled();
    // The list collapses to the pick; clearing brings it back.
    expect(screen.queryByTestId("import-chat-session-claude-1")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("import-chat-clear-selection"));

    expect(screen.getByTestId("import-chat-submit")).toBeDisabled();
    expect(await screen.findByTestId("import-chat-session-claude-1")).toBeInTheDocument();
  });

  it("imports the picked chat, refreshes the sidebar, and opens the new session", async () => {
    const { invalidateSpy } = renderDialog();

    fireEvent.click(await screen.findByTestId("import-chat-session-claude-1"));
    fireEvent.click(screen.getByTestId("import-chat-submit"));

    await waitFor(() => expect(importMock).toHaveBeenCalledTimes(1));
    expect(importMock).toHaveBeenCalledWith({
      source: "claude",
      externalSessionId: "claude-1",
      force: false,
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["conversations"] });
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_imported"));
  });

  it("offers a forced re-import when the chat was already imported", async () => {
    importMock.mockRejectedValueOnce(
      new ApiError("This claude session has already been imported as conv_old", 409, "conflict"),
    );
    renderDialog();

    fireEvent.click(await screen.findByTestId("import-chat-session-claude-1"));
    fireEvent.click(screen.getByTestId("import-chat-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("import-chat-conflict")).toHaveTextContent("already been imported"),
    );
    // A duplicate is not an error the user should have to leave the dialog for.
    expect(screen.queryByTestId("import-chat-error")).not.toBeInTheDocument();
    expect(navigateMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("import-chat-force"));

    await waitFor(() => expect(importMock).toHaveBeenCalledTimes(2));
    expect(importMock).toHaveBeenLastCalledWith({
      source: "claude",
      externalSessionId: "claude-1",
      force: true,
    });
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_imported"));
  });

  it("surfaces a failed import inline and stays on the screen", async () => {
    importMock.mockRejectedValue(new ApiError("500 internal error", 500, "internal_error"));
    renderDialog();

    fireEvent.click(await screen.findByTestId("import-chat-session-claude-1"));
    fireEvent.click(screen.getByTestId("import-chat-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("import-chat-error")).toHaveTextContent("500 internal error"),
    );
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it("surfaces a failed listing instead of an empty-looking machine", async () => {
    listMock.mockRejectedValue(
      new ApiError(
        "Importing local chats is only available on a single-user local server",
        403,
        "forbidden",
      ),
    );
    renderDialog();

    expect(await screen.findByTestId("import-chat-list-error")).toHaveTextContent(
      "single-user local server",
    );
    expect(screen.queryByTestId("import-chat-empty")).not.toBeInTheDocument();
  });
});
