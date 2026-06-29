// Tests for the gated "new shell" affordance.
//
// The component is rendered with a real QueryClient and a stubbed
// global fetch, so the access gate is exercised end-to-end through
// the real useSessionAgent / useCreateTerminal hooks: the agent
// response's `terminals` list decides visibility, and a click drives
// the real POST body.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NewTerminalButton } from "./NewTerminalButton";

function mockResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response;
}

const fetchMock = vi.fn();

/** Wire-shaped agent object with the given declared terminal names. */
function agentWire(terminals: string[]): Record<string, unknown> {
  return { id: "ag_1", object: "agent", name: "test-agent", terminals };
}

function renderButton(onCreated?: (key: string) => void, variant?: "icon" | "row") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <NewTerminalButton conversationId="conv_abc" onCreated={onCreated} variant={variant} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("NewTerminalButton access gate", () => {
  it("renders nothing when the agent declares no terminals", async () => {
    fetchMock.mockResolvedValue(mockResponse(agentWire([])));

    renderButton();

    // Wait for the agent query to resolve so the absence below is the
    // gate's decision, not just the loading state.
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    // The iff gate: no declared terminals → no affordance at all.
    expect(screen.queryByRole("button", { name: /new shell/i })).toBeNull();
  });

  it("creates the single declared terminal on click and focuses it", async () => {
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return mockResponse({
          id: "terminal_shell_u-x1",
          object: "session.resource",
          type: "terminal",
          session_id: "conv_abc",
          name: "shell:u-x1",
          metadata: { terminal_name: "shell", session_key: "u-x1", running: true },
        });
      }
      return mockResponse(agentWire(["shell"]));
    });
    const onCreated = vi.fn();

    renderButton(onCreated);

    const button = await screen.findByRole("button", { name: /new shell/i });
    fireEvent.click(button);

    // The created terminal's tab key reaches the host surface so it
    // can focus the new tab — a miss means the click created a
    // terminal the user never sees selected.
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("terminal:terminal_shell_u-x1"));

    const postCall = fetchMock.mock.calls.find(
      (call) => (call[1] as RequestInit | undefined)?.method === "POST",
    ) as [string, RequestInit];
    expect(postCall[0]).toBe("/v1/sessions/conv_abc/resources/terminals");
    // The single declared name is used directly — no picker.
    expect(JSON.parse(postCall[1].body as string).terminal).toBe("shell");
  });

  it("row variant renders a labeled list row that creates on click", async () => {
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return mockResponse({
          id: "terminal_shell_u-x2",
          object: "session.resource",
          type: "terminal",
          session_id: "conv_abc",
          name: "shell:u-x2",
          metadata: { terminal_name: "shell", session_key: "u-x2", running: true },
        });
      }
      return mockResponse(agentWire(["shell"]));
    });
    const onCreated = vi.fn();

    renderButton(onCreated, "row");

    // The virtual row carries a visible label (not an icon-only
    // tooltip button) so an empty Shells list reads as a list with one
    // actionable entry.
    const row = await screen.findByRole("button", { name: /new shell/i });
    expect(row).toHaveTextContent("New shell");
    fireEvent.click(row);

    // Same create + focus contract as the icon variant — a divergence
    // means the variants forked behavior, not just presentation.
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("terminal:terminal_shell_u-x2"));
  });
});
