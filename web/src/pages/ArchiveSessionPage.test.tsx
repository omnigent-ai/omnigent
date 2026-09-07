import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ArchiveSessionPage } from "./ArchiveSessionPage";

const mocks = vi.hoisted(() => ({ getSessionSlim: vi.fn() }));

vi.mock("@/lib/sessionsApi", () => ({ getSessionSlim: mocks.getSessionSlim }));
vi.mock("@/components/archive/ArchiveTranscriptViewer", () => ({
  ArchiveTranscriptViewer: ({
    conversation,
  }: {
    conversation: {
      id: string;
      updated_at: number;
      archived_at?: number | null;
      archived?: boolean;
      search_match?: { item_id: string };
    };
  }) => (
    <div data-testid="archive-page-reader">
      {conversation.id}:{conversation.search_match?.item_id ?? "latest"}:{conversation.updated_at}:
      {conversation.archived_at}:{String(conversation.archived)}
    </div>
  ),
}));

describe("ArchiveSessionPage", () => {
  it("hydrates source metadata and forwards the deep-linked item locator", async () => {
    mocks.getSessionSlim.mockResolvedValueOnce({
      id: "conv_a",
      agentId: "agent_a",
      agentName: "Codex",
      hostId: "host_mac",
      status: "idle",
      createdAt: 123,
      updatedAt: 456,
      archivedAt: 345,
      archived: true,
      title: "Source",
      labels: {},
      items: [],
      permissionLevel: 1,
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <MemoryRouter initialEntries={["/archive/conv_a?response=resp_1&item=msg_1"]}>
        <QueryClientProvider client={client}>
          <Routes>
            <Route path="/archive/:sessionId" element={<ArchiveSessionPage />} />
          </Routes>
        </QueryClientProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByTestId("archive-page-reader")).toHaveTextContent(
      "conv_a:msg_1:456:345:true",
    );
    expect(mocks.getSessionSlim).toHaveBeenCalledWith("conv_a");
  });
});
