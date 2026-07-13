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
import type { TerminalFirstContextValue } from "./TerminalFirstContext";
import { TerminalFirstContextProvider } from "./TerminalFirstContext";

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

/** Minimal TerminalFirst context; only `isNativeWrapper` matters here. */
function terminalFirstCtx(isNativeWrapper: boolean): TerminalFirstContextValue {
  return {
    isClaudeNative: false,
    isNativeWrapper,
    isTerminalFirst: isNativeWrapper,
    isShellView: false,
    view: "chat",
    terminalViewKey: null,
    setView: () => {},
    terminalsAvailable: true,
    terminalStartingUp: false,
  } as TerminalFirstContextValue;
}

function renderButton(
  onCreated?: (key: string) => void,
  variant?: "icon" | "row",
  isNativeWrapper = false,
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <TerminalFirstContextProvider value={terminalFirstCtx(isNativeWrapper)}>
        <NewTerminalButton conversationId="conv_abc" onCreated={onCreated} variant={variant} />
      </TerminalFirstContextProvider>
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

/**
 * Wire a fetch mock that answers the agent query with the given declared
 * terminals and echoes any create POST back as a terminal resource whose
 * ``terminal_name`` is the launched name.
 */
function mockAgentAndCreate(terminals: string[]): void {
  fetchMock.mockImplementation(async (_url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const name = JSON.parse(init.body as string).terminal as string;
      return mockResponse({
        id: `terminal_${name}_u-xx`,
        object: "session.resource",
        type: "terminal",
        session_id: "conv_abc",
        name: `${name}:u-xx`,
        metadata: { terminal_name: name, session_key: "u-xx", running: true },
      });
    }
    return mockResponse(agentWire(terminals));
  });
}

/** The POST body's ``terminal`` field for the single create call made so far. */
function launchedTerminal(): string {
  const postCall = fetchMock.mock.calls.find(
    (call) => (call[1] as RequestInit | undefined)?.method === "POST",
  ) as [string, RequestInit];
  return JSON.parse(postCall[1].body as string).terminal;
}

describe("NewTerminalButton native shell picker", () => {
  it("split button launches the default shell (declared[0]) on primary click", async () => {
    // Native wrapper declares the host's installed shells, $SHELL first.
    mockAgentAndCreate(["zsh", "bash", "fish"]);
    const onCreated = vi.fn();

    renderButton(onCreated, "icon", true);

    // Primary segment is the "New shell" button; the caret is separate.
    const primary = await screen.findByRole("button", { name: /^new shell$/i });
    fireEvent.click(primary);

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("terminal:terminal_zsh_u-xx"));
    // The default ($SHELL, first) launches directly — no menu interaction.
    expect(launchedTerminal()).toBe("zsh");
  });

  it("caret opens a picker that can launch a non-default shell", async () => {
    mockAgentAndCreate(["zsh", "bash", "fish"]);
    const onCreated = vi.fn();

    renderButton(onCreated, "icon", true);

    // Open the picker via the caret, then choose fish (not the default).
    // Radix menus open on pointerdown, not click.
    const caret = await screen.findByRole("button", { name: /choose shell/i });
    fireEvent.pointerDown(caret, { button: 0 });
    const fishItem = await screen.findByRole("menuitem", { name: /^fish$/ });
    // While the menu is open: the default entry is labeled as such; the
    // others are bare names. (Radix unmounts the menu on select, so this
    // must be checked before clicking.)
    expect(screen.queryByRole("menuitem", { name: /zsh \(default\)/i })).not.toBeNull();
    fireEvent.click(fishItem);

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("terminal:terminal_fish_u-xx"));
    expect(launchedTerminal()).toBe("fish");
  });

  it("SDK agent with multiple terminals keeps a plain dropdown (no default click)", async () => {
    // Not a native wrapper: distinct-purpose terminals, no privileged default.
    mockAgentAndCreate(["worker", "shell"]);
    const onCreated = vi.fn();

    renderButton(onCreated, "icon", false);

    // The whole control is the dropdown trigger — opening it (Radix opens on
    // pointerdown) reveals the menu rather than launching anything, and there
    // is no separate caret.
    expect(screen.queryByRole("button", { name: /choose shell/i })).toBeNull();
    const trigger = await screen.findByRole("button", { name: /new shell/i });
    fireEvent.pointerDown(trigger, { button: 0 });
    // No launch happened from opening the menu.
    expect(
      fetchMock.mock.calls.some((c) => (c[1] as RequestInit | undefined)?.method === "POST"),
    ).toBe(false);
    // Picking the first entry launches it verbatim, unlabeled.
    const workerItem = await screen.findByRole("menuitem", { name: /^worker$/ });
    fireEvent.click(workerItem);
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("terminal:terminal_worker_u-xx"));
    expect(launchedTerminal()).toBe("worker");
  });
});
