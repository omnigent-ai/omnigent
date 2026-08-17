import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import type { RoutingApi } from "@/lib/routing";
import { ImportChatDialog } from "./ImportChatDialog";

const navigate = vi.fn();

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/routing", async () => {
  const actual = await vi.importActual<RoutingApi>("@/lib/routing");
  return { ...actual, useNavigate: () => navigate };
});

const fetchMock = vi.mocked(authenticatedFetch);

describe("ImportChatDialog", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    navigate.mockReset();
  });

  it("lists recent chats with transcript context", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          sessions: [
            {
              session_id: "claude-session-1",
              title: "Fix the login redirect",
              workspace: "/repo/omnigent",
              item_count: 14,
            },
          ],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    render(
      <ImportChatDialog
        open
        onOpenChange={vi.fn()}
        hosts={[{ host_id: "host_local", name: "This machine", owner: "me", status: "online" }]}
        defaultHostId="host_local"
      />,
    );

    await waitFor(() =>
      expect(screen.getByTestId("import-chat-host")).toHaveTextContent("This machine"),
    );
    expect(await screen.findByText("Fix the login redirect")).toBeTruthy();
    expect(screen.getByText("/repo/omnigent · 14 items")).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/hosts/host_local/chat-imports?source=claude&limit=10",
    );
  });

  it("selects this machine when hosts arrive after the dialog opens", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ sessions: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const { rerender } = render(
      <ImportChatDialog open onOpenChange={vi.fn()} hosts={[]} defaultHostId={null} />,
    );

    expect(fetchMock).not.toHaveBeenCalled();

    rerender(
      <ImportChatDialog
        open
        onOpenChange={vi.fn()}
        hosts={[{ host_id: "host_local", name: "This machine", owner: "me", status: "online" }]}
        defaultHostId={null}
      />,
    );

    await waitFor(() =>
      expect(screen.getByTestId("import-chat-host")).toHaveTextContent("This machine"),
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  });

  it("restores the recent list when the selected session ID is cleared", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          sessions: [
            {
              session_id: "claude-session-1",
              title: "Fix login",
              workspace: "/repo",
              item_count: 4,
            },
          ],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    render(
      <ImportChatDialog
        open
        onOpenChange={vi.fn()}
        hosts={[{ host_id: "host_local", name: "This machine", owner: "me", status: "online" }]}
        defaultHostId="host_local"
      />,
    );

    fireEvent.click(await screen.findByText("Fix login"));
    const input = screen.getByTestId("import-chat-session-id");
    expect(input).toHaveValue("claude-session-1");
    expect(screen.queryByText("Fix login")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Clear selected session" }));
    expect(screen.getByText("Fix login")).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
