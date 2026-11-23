import { createRef } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Agent } from "@/hooks/useAgents";
import { useChatStore } from "@/store/chatStore";
import { Transcript } from "./Transcript";
import { useAgentChatPresentation } from "./useAgentChatPresentation";

const initialState = useChatStore.getState();
const clients: QueryClient[] = [];
const noop = () => {};

afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  useChatStore.setState(initialState, true);
  vi.unstubAllGlobals();
});

function SessionTranscript({ agent }: { agent: Agent }) {
  const presentation = useAgentChatPresentation(agent);
  return (
    <Transcript
      presentation={presentation}
      hostId={null}
      setConversationEl={noop}
      containerEl={null}
      scroller={null}
      setScroller={noop}
      sendScrollNonce={0}
      hasMoreHistory={false}
      loadingMoreHistory={false}
      isMobileViewport
      showsWorking={false}
      agentsError={null}
      sandboxLaunching={false}
      spacerMeasureRef={createRef()}
    />
  );
}

it.each([
  ["maestro", "Maestro", "data-agent-icon", "maestro"],
  ["claude-native-ui", "Claude Code", "data-harness-icon", "claude"],
  ["codex-native-ui", "Codex", "data-harness-icon", "codex"],
])(
  "uses %s's orb and one name per response block in the full transcript",
  async (name, displayName, attribute, icon) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    clients.push(client);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: [] }))));
    useChatStore.setState({
      conversationId: null,
      blocks: [],
      pendingUserMessages: [],
      activeResponse: null,
      sessionStatus: "idle",
    });
    const agent = { id: "bound", name, description: "Helps with this work." };
    const view = (populated: boolean) => (
      <QueryClientProvider client={client}>
        <TooltipProvider>
          <MemoryRouter>
            <SessionTranscript agent={agent} key={String(populated)} />
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>
    );
    const rendered = render(view(false));
    expect(
      screen
        .getByRole("button", { name: `About ${displayName}` })
        .querySelector(`[${attribute}="${icon}"]`),
    ).not.toBeNull();
    expect(screen.queryByTestId("assistant-name")).toBeNull();
    expect(screen.queryByText(displayName)).toBeNull();
    expect(
      screen.getByRole("button", { name: `About ${displayName}` }).closest('[role="log"]'),
    ).toBeNull();

    rendered.unmount();
    const ctx = {
      agent: null,
      depth: 0,
      turn: 0,
      timestamp: 0,
      responseId: "response-1",
      itemId: "output-1",
    };
    useChatStore.setState({
      blocks: [
        { type: "text_done", ctx, fullText: "First output", hasCodeBlocks: false },
        {
          type: "text_done",
          ctx: { ...ctx, itemId: "output-2" },
          fullText: "Second output",
          hasCodeBlocks: false,
        },
        { type: "response_end", ctx, status: "completed", response: null },
      ],
    });
    render(view(true));
    expect(screen.getAllByTestId("assistant-name")).toHaveLength(1);
    expect(screen.getByTestId("assistant-name")).toHaveTextContent(displayName);
    expect(screen.getByText("First output")).toBeVisible();
    expect(screen.getByText("Second output")).toBeVisible();
    expect(screen.getAllByRole("button", { name: "Copy" })).toHaveLength(1);
    expect(
      screen.getByRole("button", { name: `About ${displayName}` }).closest('[role="log"]'),
    ).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: `About ${displayName}` }));
    expect(await screen.findByRole("region", { name: `About ${displayName}` })).toHaveTextContent(
      agent.description,
    );
    expect(screen.queryByText("Harness")).toBeNull();
    expect(screen.queryByText("Model")).toBeNull();
    expect(screen.queryByText("Effort")).toBeNull();
  },
);
