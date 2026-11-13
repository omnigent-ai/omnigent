import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";
import { childSessionsQueryKey } from "@/hooks/useChildSessions";
import { useChatStore } from "@/store/chatStore";
import { SessionOverviewSubagents } from "./SessionOverviewSubagents";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

function child(id: string, title: string) {
  return { id, title, tool: title.split(":")[0], session_name: null, busy: false };
}

function route(children: Response, costs: unknown) {
  vi.mocked(authenticatedFetch).mockImplementation(async (input) =>
    String(input).includes("/child_sessions")
      ? children
      : new Response(JSON.stringify({ data: costs, has_more: false })),
  );
}

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <CapabilitiesProvider
        info={{ ...FALLBACK_SERVER_INFO, features: { session_overview: true } }}
      >
        <SessionOverviewSubagents />
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
  return { client, ...view };
}

beforeEach(() => {
  vi.mocked(authenticatedFetch).mockReset();
  useChatStore.setState({ conversationId: "conv_root" });
});

afterEach(cleanup);

describe("SessionOverviewSubagents", () => {
  it("lists each direct sub-agent with its cost, leaving an unknown cost empty", async () => {
    route(
      new Response(
        JSON.stringify({
          data: [
            child("conv_a", "codex:review"),
            child("conv_b", "worker:docs"),
            child("conv_c", "Explore:scan"),
          ],
        }),
      ),
      [
        { id: "conv_root", cost_usd: 2 },
        { id: "conv_a", cost_usd: 0.5 },
        { id: "conv_b", cost_usd: null },
        { id: "conv_c", cost_usd: 0.25, estimated: true },
      ],
    );
    renderSection();

    await waitFor(() =>
      expect(screen.getByTestId("session-overview-subagent-conv_a")).toHaveTextContent(
        "review$0.50",
      ),
    );
    expect(screen.getByRole("region", { name: "Sub-agents" })).toBeTruthy();
    expect(screen.getByTestId("session-overview-subagent-conv_b")).toHaveTextContent(/^docs$/);
    expect(screen.getByTestId("session-overview-subagent-conv_c")).toHaveTextContent("scan~$0.25");
  });

  it("stays hidden without sub-agents", async () => {
    route(new Response('{"data":[]}'), []);
    const { client, container } = renderSection();

    await waitFor(() =>
      expect(client.getQueryState(childSessionsQueryKey("conv_root"))?.status).toBe("success"),
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("shows a failed sub-agent list as unavailable rather than none", async () => {
    route(new Response("{}", { status: 500 }), []);
    renderSection();

    expect(await screen.findByRole("region", { name: "Sub-agents" })).toHaveTextContent(
      "Unavailable",
    );
  });
});
