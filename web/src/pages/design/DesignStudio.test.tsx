// Tests for the Design studio view. The deck read, the side chat pane, and the
// deck viewer are mocked; turn state comes from the real conversation registry.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as ChatStoreModule from "@/store/chatStore";
import { fetchFileContent, type FileContentResponse } from "@/hooks/useFileContent";
import { ensureConversationStreamed } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import {
  fetchImportTarget,
  planDesignSystemImportFrom,
  runDesignSystemImport,
} from "@/lib/designDeckApi";
import type { StudioView } from "@/lib/designStudio";
import type { ImportPlan } from "@/lib/designSystemImport";
import { DesignStudio } from "./DesignStudio";

const { mobileRef, viewerMounts } = vi.hoisted(() => ({
  mobileRef: { current: false },
  viewerMounts: { count: 0 },
}));

vi.mock("@/store/chatStore", async (importOriginal) => ({
  ...(await importOriginal<typeof ChatStoreModule>()),
  ensureConversationStreamed: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("@/hooks/useIsMobileViewport", () => ({ useIsMobileViewport: () => mobileRef.current }));
vi.mock("@/hooks/useWorkingLabelTick", () => ({ useWorkingLabelTick: () => 0 }));
vi.mock("@/hooks/useFileContent", () => ({ fetchFileContent: vi.fn() }));
vi.mock("@/components/chat/SideChatPane", () => ({
  SideChatPane: (props: { childId: string; fullHistory?: boolean; placeholder?: string }) => (
    <div
      data-testid="side-chat"
      data-session={props.childId}
      data-full={String(props.fullHistory)}
      data-placeholder={props.placeholder}
    />
  ),
}));
vi.mock("@/shell/SlidesViewer", async () => {
  const { useState } = await import("react");
  return {
    SlidesViewer: ({ content }: { content: string }) => {
      useState(() => (viewerMounts.count += 1));
      return <div data-testid="slides-viewer">{content}</div>;
    },
  };
});
vi.mock("@/lib/designDeckApi", () => ({
  fetchImportTarget: vi.fn(),
  planDesignSystemImportFrom: vi.fn(),
  runDesignSystemImport: vi.fn(),
}));

const SESSION = "conv_a";
const PATH = "decks/pitch.slides.html";
const contentMock = vi.mocked(fetchFileContent);
let client: QueryClient;

function deckFile(content: string): FileContentResponse {
  return {
    object: "session.environment.filesystem.file_content",
    path: PATH,
    content_type: "text/html",
    encoding: "utf-8",
    content,
    bytes: content.length,
  };
}

function renderStudio(props: { view?: StudioView; fresh?: boolean } = {}): {
  onView: ReturnType<typeof vi.fn>;
  onBack: ReturnType<typeof vi.fn>;
} {
  const onView = vi.fn();
  const onBack = vi.fn();
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DesignStudio
          sessionId={SESSION}
          path={PATH}
          view={props.view ?? "preview"}
          fresh={props.fresh ?? false}
          onView={onView}
          onBack={onBack}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { onView, onBack };
}

function setSession(
  patch: Parameters<ReturnType<typeof conversationRegistry.acquire>["setState"]>[0],
) {
  act(() => conversationRegistry.acquire(SESSION).setState(patch));
}

beforeEach(() => {
  mobileRef.current = false;
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  conversationRegistry.clear();
  contentMock.mockResolvedValue(deckFile("<section>Title</section>"));
  vi.mocked(fetchImportTarget).mockResolvedValue(null);
  viewerMounts.count = 0;
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  conversationRegistry.clear();
});

describe("DesignStudio on desktop", () => {
  it("shows the full-history chat beside the preview and keeps the stream bound", async () => {
    renderStudio();
    expect(await screen.findByTestId("slides-viewer")).toHaveTextContent(
      "<section>Title</section>",
    );
    const chat = screen.getByTestId("side-chat");
    expect(chat).toHaveAttribute("data-session", SESSION);
    expect(chat).toHaveAttribute("data-full", "true");
    expect(chat).toHaveAttribute("data-placeholder", "Ask for changes to this deck");
    expect(ensureConversationStreamed).toHaveBeenCalledWith(SESSION);
    expect(contentMock).toHaveBeenCalledWith(SESSION, PATH);
    expect(screen.getByRole("link", { name: "Open in session" })).toHaveAttribute(
      "href",
      `/c/${SESSION}?file=${encodeURIComponent(PATH)}`,
    );
  });

  it("toggles Preview and Full through onView", async () => {
    const { onView } = renderStudio();
    expect(screen.getByRole("button", { name: "Preview" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Full" }));
    expect(onView).toHaveBeenCalledWith("full");
  });

  it("hides the chat in Full mode", async () => {
    const { onView } = renderStudio({ view: "full" });
    expect(await screen.findByTestId("slides-viewer")).toBeInTheDocument();
    expect(screen.queryByTestId("side-chat")).toBeNull();
    expect(screen.getByRole("button", { name: "Full" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    expect(onView).toHaveBeenCalledWith("preview");
  });

  it("calls onBack from Back to designs", () => {
    const { onBack } = renderStudio();
    fireEvent.click(screen.getByRole("link", { name: "Back to designs" }));
    expect(onBack).toHaveBeenCalled();
  });
});

describe("DesignStudio design-system import", () => {
  const source = { path: "/brand/acme", kind: "full" as const, name: "Acme" };
  const plan: ImportPlan = {
    files: [
      { path: "SKILL.md", bytes: 10 },
      { path: "fonts/a.woff2", bytes: 20 },
    ],
    skipped: [{ path: "preview/", reason: "never imported" }],
    totalBytes: 30,
  };
  const importButton = () => screen.findByRole("button", { name: "Import design system" });
  beforeEach(() => {
    vi.mocked(fetchImportTarget).mockResolvedValue({ hostId: "host_1", source });
    vi.mocked(planDesignSystemImportFrom).mockResolvedValue(plan);
  });

  it("is not offered without an outside system the viewer owns", async () => {
    vi.mocked(fetchImportTarget).mockResolvedValue(null);
    renderStudio();
    expect(await screen.findByTestId("slides-viewer")).toBeInTheDocument();
    await waitFor(() => expect(fetchImportTarget).toHaveBeenCalledWith(SESSION));
    expect(screen.queryByRole("button", { name: "Import design system" })).toBeNull();
  });

  it("confirms, copies, and reloads the preview", async () => {
    vi.mocked(runDesignSystemImport).mockImplementation(async (_s, _p, _r, onProgress) => {
      onProgress(1, 2);
      onProgress(2, 2);
      return { errors: [], ref: { ...source, path: ".omnigent/design-system" } };
    });
    renderStudio();
    expect(await screen.findByTestId("slides-viewer")).toBeInTheDocument();
    fireEvent.click(await importButton());
    expect(planDesignSystemImportFrom).toHaveBeenCalledWith("host_1", "/brand/acme");
    expect(await screen.findByTestId("design-system-import-summary")).toHaveTextContent(
      "Copies 2 files (30 B) into .omnigent/design-system.",
    );
    expect(screen.getByText("preview/: never imported")).toBeInTheDocument();
    vi.mocked(fetchImportTarget).mockResolvedValue(null);
    fireEvent.click(screen.getByRole("button", { name: "Import" }));

    await waitFor(() => expect(screen.queryByTestId("import-design-system-dialog")).toBeNull());
    expect(runDesignSystemImport).toHaveBeenCalledWith(SESSION, plan, source, expect.any(Function));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Import design system" })).toBeNull(),
    );
    expect(viewerMounts.count).toBe(2);
  });

  it("lists per-file errors and offers Retry", async () => {
    vi.mocked(runDesignSystemImport).mockResolvedValue({
      errors: [{ path: "fonts/a.woff2", message: "507 Insufficient Storage" }],
      ref: null,
    });
    renderStudio();
    fireEvent.click(await importButton());
    fireEvent.click(await screen.findByRole("button", { name: "Import" }));
    expect(await screen.findByText("fonts/a.woff2: 507 Insufficient Storage")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Copied 2 of 2 files");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(runDesignSystemImport).toHaveBeenCalledTimes(2));
  });
});

describe("DesignStudio waiting states", () => {
  beforeEach(() => {
    contentMock.mockRejectedValue(new Error("404 Not Found"));
  });

  it("waits with the working indicator, then says the deck was not written", async () => {
    setSession({ sessionStatus: "running", loadingConversation: false });
    renderStudio({ fresh: true });

    expect(await screen.findByText("Waiting for the first slide")).toBeInTheDocument();
    expect(screen.getByTestId("working-indicator")).toBeInTheDocument();

    setSession({ sessionStatus: "idle" });

    expect(
      await screen.findByText(
        (_, el) =>
          el?.tagName === "P" && el.textContent === `The agent has not written ${PATH} yet`,
      ),
    ).toBeInTheDocument();
    expect(screen.getByTestId("side-chat")).toBeInTheDocument();
  });

  it("keeps waiting on a fresh session before its turn starts", async () => {
    renderStudio({ fresh: true });
    expect(await screen.findByText("Waiting for the first slide")).toBeInTheDocument();
    expect(screen.queryByTestId("working-indicator")).toBeNull();
  });

  it("says not written for a reopened idle session", async () => {
    setSession({
      sessionStatus: "idle",
      loadingConversation: false,
      blocks: [{ type: "user_message", ctx: { itemId: "u1" }, content: [] }] as never,
    });
    renderStudio();
    expect(await screen.findByText(/has not written/)).toBeInTheDocument();
  });

  it("shows other read errors", async () => {
    contentMock.mockRejectedValue(new Error("403 Forbidden"));
    renderStudio();
    expect(await screen.findByText(/403 Forbidden/, {}, { timeout: 4000 })).toBeInTheDocument();
  });
});

describe("DesignStudio live preview", () => {
  it("refetches the deck when its query is invalidated by a file change", async () => {
    renderStudio();
    await screen.findByTestId("slides-viewer");
    contentMock.mockResolvedValue(deckFile("<section>Title</section><section>Two</section>"));

    await act(() => client.invalidateQueries({ queryKey: ["design-deck", SESSION] }));

    expect(await screen.findByText(/Two/)).toBeInTheDocument();
  });

  it("refetches the deck when the turn ends", async () => {
    setSession({ sessionStatus: "running" });
    renderStudio();
    await screen.findByTestId("slides-viewer");
    const calls = contentMock.mock.calls.length;

    setSession({ sessionStatus: "idle" });

    await waitFor(() => expect(contentMock.mock.calls.length).toBeGreaterThan(calls));
  });
});

describe("DesignStudio on a phone", () => {
  beforeEach(() => {
    mobileRef.current = true;
  });

  it("shows the preview with a Chat button", async () => {
    const { onView } = renderStudio();
    expect(await screen.findByTestId("slides-viewer")).toBeInTheDocument();
    expect(screen.queryByTestId("side-chat")).toBeNull();
    expect(screen.queryByRole("button", { name: "Full" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Chat" }));
    expect(onView).toHaveBeenCalledWith("chat");
  });

  it("shows the chat full screen and closes back to the preview", () => {
    const { onView } = renderStudio({ view: "chat" });
    expect(screen.getByTestId("side-chat")).toBeInTheDocument();
    expect(screen.queryByTestId("slides-viewer")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Close chat" }));
    expect(onView).toHaveBeenCalledWith("preview");
  });
});
