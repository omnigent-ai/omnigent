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

function route(children: Response | ((after: string | null) => Response), costs: unknown) {
  vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
    const url = new URL(String(input), "http://test.local");
    if (!url.pathname.endsWith("/child_sessions")) {
      return new Response(JSON.stringify({ data: costs, has_more: false }));
    }
    return typeof children === "function" ? children(url.searchParams.get("after")) : children;
  });
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
    // The older sub-agent is on the child list's second page.
    route(
      (after) =>
        new Response(
          JSON.stringify(
            after === null
              ? { data: [child("conv_a", "codex:review")], last_id: "conv_a", has_more: true }
              : { data: [child("conv_b", "worker:docs")], has_more: false },
          ),
        ),
      [
        { id: "conv_root", cost_usd: 2 },
        { id: "conv_a", cost_usd: 0.5 },
        { id: "conv_b", cost_usd: null },
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
