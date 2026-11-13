import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  useWorkspaceChangedFiles,
  useWorkspaceEnvironment,
} from "@/hooks/useWorkspaceChangedFiles";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";
import { useChatStore } from "@/store/chatStore";
import { SessionOverview } from "./SessionOverview";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceChangedFiles: vi.fn(),
  useWorkspaceEnvironment: vi.fn(),
}));

function json(body: unknown): Response {
  return new Response(JSON.stringify(body));
}

function child(id: string, tool: string) {
  return { id, title: `${tool}:run`, tool, session_name: "run", current_task_status: null };
}

function routeFetch(
  costs: unknown[] | Promise<never> | Response,
  children: Response | null = null,
) {
  vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
    const url = String(input);
    if (url.endsWith("/child_sessions")) {
      if (children !== null) return children;
      return json({
        object: "list",
        data: [
          { ...child("conv_codex", "codex"), busy: false },
          { ...child("conv_claude", "claude"), busy: false },
          { ...child("conv_new", "fresh"), busy: false },
        ],
      });
    }
    if (url.includes("/agent_costs?")) {
      return costs instanceof Response ? costs : json({ object: "list", data: await costs });
    }
    throw new Error(`unexpected fetch ${url}`);
  });
}

function renderOverview(showChangedLines = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <CapabilitiesProvider
        info={{ ...FALLBACK_SERVER_INFO, features: { session_overview: true } }}
      >
        <SessionOverview
          sessionId="conv_parent"
          showChangedLines={showChangedLines}
          onClose={() => {}}
        />
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
}

function row(label: string): HTMLElement {
  return screen.getByText(label).closest("div") as HTMLElement;
}

beforeEach(() => {
  useChatStore.setState({
    sessionCostUsd: null,
    sessionUsageByModel: null,
    loadingConversation: false,
    sessionUsageLoading: false,
  });
  vi.mocked(useWorkspaceEnvironment).mockReturnValue({ isFetching: false } as never);
  vi.mocked(useWorkspaceChangedFiles).mockReturnValue({
    data: undefined,
    isFetching: false,
  } as never);
});

afterEach(() => {
  cleanup();
  vi.mocked(authenticatedFetch).mockReset();
});

describe("SessionOverview", () => {
  it.each([
    [{ loadingConversation: true }, null],
    [{ sessionUsageLoading: true }, null],
    [{}, "Unavailable"],
  ])("separates loading from unavailable usage (%o)", (state, text) => {
    routeFetch([]);
    useChatStore.setState(state);
    renderOverview();

    const cost = row("Total cost");
    const tokens = row("Tokens");
    if (text === null) {
      expect(within(cost).getByLabelText("Loading cost")).toBeTruthy();
      expect(within(tokens).getByLabelText("Loading tokens")).toBeTruthy();
    } else {
      expect(within(cost).getByText(text)).toBeTruthy();
      expect(within(tokens).getByText(text)).toBeTruthy();
    }
  });

  it("shows a known zero and refuses a partial token total", () => {
    routeFetch([]);
    useChatStore.setState({
      sessionCostUsd: 0,
      sessionUsageLoading: true,
      sessionUsageByModel: {
        "model-a": usage(0),
        "model-b": usage(null),
      },
    });
    renderOverview();

    expect(within(row("Total cost")).getByText("$0.00")).toBeTruthy();
    expect(within(row("Tokens")).getByText("Unavailable")).toBeTruthy();
  });

  it("sums tokens only when every model reports them", () => {
    routeFetch([]);
    useChatStore.setState({
      sessionCostUsd: 1.5,
      sessionUsageByModel: { "model-a": usage(1200), "model-b": usage(0) },
    });
    renderOverview();

    expect(within(row("Tokens")).getByText("1.2K")).toBeTruthy();
  });

  it("labels exact, estimated and unknown sub-agent costs", async () => {
    routeFetch([
      { id: "conv_parent", cost_usd: 2 },
      { id: "conv_codex", cost_usd: 0.4 },
      { id: "conv_claude", cost_usd: 0.25, estimated: true },
      { id: "conv_new", cost_usd: null },
    ]);
    renderOverview();

    const list = await screen.findByTestId("session-overview-subagents");
    const costOf = (id: string) =>
      list.querySelector(`[data-child-session-id="${id}"] .font-mono`)?.textContent;
    await vi.waitFor(() => expect(costOf("conv_codex")).toBe("$0.40"));
    expect(costOf("conv_claude")).toBe("~$0.25");
    expect(costOf("conv_new")).toBe("—");
  });

  it("keeps sub-agent costs loading until the first result", async () => {
    routeFetch(new Promise<never>(() => {}));
    renderOverview();

    const list = await screen.findByTestId("session-overview-subagents");
    expect(within(list).getAllByLabelText("Loading cost")).toHaveLength(3);
  });

  it("tells a failed sub-agent list apart from an empty one", async () => {
    routeFetch([], new Response("", { status: 500 }));
    renderOverview();

    const section = screen.getByText("Sub-agents").closest("section") as HTMLElement;
    await within(section).findByText("Unavailable");
    expect(within(section).queryByText("No sub-agents")).toBeNull();
  });

  it("leaves costs unknown when the server has no breakdown", async () => {
    routeFetch(new Response("", { status: 404 }));
    renderOverview();

    const list = await screen.findByTestId("session-overview-subagents");
    await vi.waitFor(() => expect(within(list).getAllByText("—")).toHaveLength(3));
  });

  it("counts changed lines across files, including ones without line counts", () => {
    routeFetch([]);
    vi.mocked(useWorkspaceChangedFiles).mockReturnValue({
      data: {
        available: true,
        data: [
          { path: "a.ts", lines_added: 10, lines_removed: 2 },
          { path: "b.png", lines_added: null, lines_removed: null },
        ],
      },
      isFetching: false,
    } as never);
    renderOverview(true);

    expect(row("Changed lines").textContent).toContain("+10 −2 · 2 files");
  });
});

function usage(totalTokens: number | null) {
  return {
    inputTokens: null,
    outputTokens: null,
    totalTokens,
    cacheReadInputTokens: null,
    cacheCreationInputTokens: null,
    totalCostUsd: null,
  };
}
