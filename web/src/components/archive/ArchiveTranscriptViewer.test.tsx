import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Conversation } from "@/hooks/useConversations";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ArchiveTranscriptViewer } from "./ArchiveTranscriptViewer";

const mocks = vi.hoisted(() => ({
  fetchPage: vi.fn(),
  fetchWindow: vi.fn(),
  searchItems: vi.fn(),
  copyText: vi.fn(() => Promise.resolve()),
}));

vi.mock("@/lib/sessionsApi", () => ({
  INITIAL_WINDOW_ITEMS: 100,
  fetchSessionItemsPage: mocks.fetchPage,
  fetchSessionItemsWindow: mocks.fetchWindow,
  searchSessionItems: mocks.searchItems,
}));
vi.mock("@/lib/clipboard", () => ({ copyText: mocks.copyText }));
vi.mock("@/lib/routing", () => ({
  useNavigate: () => vi.fn(),
  useRebasePath: () => (path: string) => path,
}));
vi.mock("@/lib/itemsToBlocks", () => ({ itemsToBlocks: (items: unknown[]) => items }));
vi.mock("@/lib/renderItems", () => ({
  buildBubbles: (items: { id: string; text?: string }[]) =>
    items.map((item) => ({
      kind: "user",
      itemId: item.id,
      stableKey: item.id,
      content: [{ type: "input_text", text: item.text ?? "hello archive" }],
    })),
}));
vi.mock("@/pages/ChatPage", () => ({
  BubbleView: ({ bubble }: { bubble: { itemId: string } }) => (
    <div>Rendered message {bubble.itemId}</div>
  ),
  collectBubbleMarkdown: () => "assistant text",
}));

const conversation = {
  id: "conv_archive",
  title: "Research notes",
  archived: true,
  created_at: 1,
  updated_at: 2,
  agent_name: "codex",
} as Conversation;

function renderViewer(value: Conversation = conversation) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <ArchiveTranscriptViewer conversation={value} />
        </TooltipProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("ArchiveTranscriptViewer", () => {
  beforeEach(() => {
    mocks.fetchPage.mockReset().mockResolvedValue({
      items: [{ id: "msg_latest", text: "quoted insight" }],
      hasMore: false,
    });
    mocks.fetchWindow.mockReset().mockResolvedValue({
      items: [{ id: "msg_hit", text: "matching insight" }],
      anchorId: "msg_hit",
      hasOlder: true,
      hasNewer: true,
    });
    mocks.searchItems.mockReset().mockResolvedValue({
      items: [
        { id: "msg_first_hit", text: "first archive match" },
        { id: "msg_second_hit", text: "second archive match" },
      ],
      hasMore: false,
    });
    mocks.copyText.mockClear();
  });

  it("renders the normal message surface and copies a message-level deep link", async () => {
    renderViewer();

    expect(await screen.findByText("Rendered message msg_latest")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Copy message citation" }));
    await waitFor(() =>
      expect(mocks.copyText).toHaveBeenCalledWith(
        expect.stringMatching(
          /^⟦Omnigent reference \| session=conv_archive \| target=item:msg_latest \| agent=codex \| host=unknown-host \| title=Research notes \| link=omnigent:\/\/[^/]+\/archive\/conv_archive\?item=msg_latest⟧\n> quoted insight$/,
        ),
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "Copy message link" }));

    await waitFor(() =>
      expect(mocks.copyText).toHaveBeenCalledWith(
        expect.stringMatching(/^omnigent:\/\/[^/]+\/archive\/conv_archive\?item=msg_latest$/),
      ),
    );
  });

  it("uses the exact search match to fetch a bounded transcript window", async () => {
    renderViewer({
      ...conversation,
      search_match: {
        item_id: "msg_hit",
        response_id: "resp_hit",
        created_at: 123,
        snippet: "matching insight",
      },
    });

    expect(await screen.findByText("Rendered message msg_hit")).toBeInTheDocument();
    expect(mocks.fetchWindow).toHaveBeenCalledWith("conv_archive", "msg_hit");
    expect(mocks.fetchPage).not.toHaveBeenCalled();
    expect(screen.getByText("Showing the matching conversation window")).toBeInTheDocument();
  });

  it("loads earlier messages when the reader scrolls upward at the top", async () => {
    mocks.fetchPage
      .mockResolvedValueOnce({
        items: [{ id: "msg_latest", text: "latest" }],
        hasMore: true,
      })
      .mockResolvedValueOnce({
        items: [{ id: "msg_earlier", text: "earlier" }],
        hasMore: false,
      });
    renderViewer();

    const transcript = await screen.findByTestId("archive-transcript");
    await screen.findByText("Rendered message msg_latest");
    fireEvent.wheel(transcript, { deltaY: -32 });

    expect(await screen.findByText("Rendered message msg_earlier")).toBeInTheDocument();
    expect(mocks.fetchPage).toHaveBeenCalledTimes(2);
  });

  it("searches the full session and exposes compact and desktop match navigation", async () => {
    renderViewer();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search this archived session" }), {
      target: { value: "archive match" },
    });

    await waitFor(() =>
      expect(mocks.searchItems).toHaveBeenCalledWith("conv_archive", "archive match"),
    );
    await waitFor(() =>
      expect(mocks.fetchWindow).toHaveBeenCalledWith("conv_archive", "msg_first_hit"),
    );
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Session search matches" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Jump to end of session" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Open search match 2" }));
    await waitFor(() =>
      expect(mocks.fetchWindow).toHaveBeenCalledWith("conv_archive", "msg_second_hit"),
    );
    expect(screen.getByText("2 / 2")).toBeInTheDocument();
  });

  it("offers one-click navigation to the end", async () => {
    renderViewer();
    const transcript = await screen.findByTestId("archive-transcript");
    const scrollTo = vi.fn();
    Object.defineProperty(transcript, "scrollHeight", { configurable: true, value: 1200 });
    Object.defineProperty(transcript, "clientHeight", { configurable: true, value: 400 });
    Object.defineProperty(transcript, "scrollTo", { configurable: true, value: scrollTo });
    fireEvent.scroll(transcript);

    fireEvent.click(screen.getByRole("button", { name: "Jump to end of session" }));
    await waitFor(() => expect(scrollTo).toHaveBeenCalledWith({ top: 1200, behavior: "smooth" }));
  });

  it("shows a turn rail and jumps directly between conversation turns", async () => {
    mocks.fetchPage.mockResolvedValueOnce({
      items: [
        { id: "turn_one", text: "first turn" },
        { id: "turn_two", text: "second turn" },
        { id: "turn_three", text: "third turn" },
      ],
      hasMore: false,
    });
    renderViewer();

    await screen.findByText("Rendered message turn_three");
    expect(screen.getByRole("navigation", { name: "Conversation turns" })).toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "Conversation turn controls" }),
    ).toBeInTheDocument();
    const target = document.querySelector<HTMLElement>('[data-archive-turn="1"]');
    const scrollIntoView = vi.fn();
    Object.defineProperty(target, "scrollIntoView", { configurable: true, value: scrollIntoView });

    fireEvent.click(screen.getByRole("button", { name: "Jump to: second turn" }));

    expect(scrollIntoView).toHaveBeenCalledWith({ block: "start", behavior: "smooth" });
    expect(screen.getByText("2/3")).toBeInTheDocument();
  });

  it("keeps the final turn selected when it is too short to reach the reading line", async () => {
    mocks.fetchPage.mockResolvedValueOnce({
      items: [
        { id: "turn_one", text: "first turn" },
        { id: "turn_two", text: "second turn" },
        { id: "turn_three", text: "short final turn" },
      ],
      hasMore: false,
    });
    renderViewer();
    await screen.findByText("Rendered message turn_three");
    const scroller = screen.getByTestId("archive-transcript");
    Object.defineProperties(scroller, {
      scrollHeight: { configurable: true, value: 1200 },
      clientHeight: { configurable: true, value: 600 },
      scrollTop: { configurable: true, value: 600 },
    });
    const turns = [...scroller.querySelectorAll<HTMLElement>("[data-archive-turn]")];
    for (const [index, turn] of turns.entries()) {
      vi.spyOn(turn, "getBoundingClientRect").mockReturnValue({
        top: [-500, -100, 200][index],
      } as DOMRect);
    }
    fireEvent.scroll(scroller);
    expect(screen.getByText("3/3")).toBeInTheDocument();
  });

  it("copies a selected passage with the containing item deep link", async () => {
    renderViewer();

    const message = await screen.findByText("Rendered message msg_latest");
    const getSelection = vi.spyOn(window, "getSelection").mockReturnValue({
      anchorNode: message.firstChild,
      toString: () => "selected passage",
    } as Selection);

    fireEvent.mouseUp(screen.getByTestId("archive-transcript"));
    fireEvent.click(screen.getByRole("button", { name: "Copy citation" }));

    await waitFor(() =>
      expect(mocks.copyText).toHaveBeenCalledWith(
        expect.stringMatching(
          /^⟦Omnigent reference \| session=conv_archive \| target=item:msg_latest .*\?item=msg_latest⟧\n> selected passage$/,
        ),
      ),
    );
    getSelection.mockRestore();
  });

  it("clears a selected passage when the reader switches sessions", async () => {
    const rendered = renderViewer();
    const message = await screen.findByText("Rendered message msg_latest");
    const getSelection = vi.spyOn(window, "getSelection").mockReturnValue({
      anchorNode: message.firstChild,
      toString: () => "selection from the first session",
    } as Selection);
    fireEvent.mouseUp(screen.getByTestId("archive-transcript"));
    expect(screen.getByRole("button", { name: "Copy citation" })).toBeInTheDocument();

    rendered.rerender(
      <MemoryRouter>
        <QueryClientProvider client={new QueryClient()}>
          <TooltipProvider>
            <ArchiveTranscriptViewer conversation={{ ...conversation, id: "conv_second" }} />
          </TooltipProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Copy citation" })).not.toBeInTheDocument(),
    );
    getSelection.mockRestore();
  });
});
