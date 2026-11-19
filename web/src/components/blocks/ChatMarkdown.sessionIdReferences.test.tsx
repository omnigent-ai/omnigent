// Bare session ids in chat (an orchestrator's "Open `<id>` and …") render as a
// pill with that session's title and open it in the app; an id that names no
// readable session stays as written.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router-dom";
import type * as NativeBridge from "@/lib/nativeBridge";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { FilePathAwareMessageResponse } from "./ChatMarkdown";

const nativeShell = vi.hoisted(() => ({ native: false }));

vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridge>()),
  isNativeShell: () => nativeShell.native,
}));

const WORKER = "e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9";
const MISSING = "0123456789abcdef0123456789abcdef";
const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  nativeShell.native = false;
});

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    json: async () => body,
  } as unknown as Response;
}

const FILE_VIEWER = {
  openFile: () => {},
  openGithubTab: () => {},
  isChangedPath: () => false,
  conversationId: undefined,
  workspaceRoot: "/home/u/ws",
  workspaceHome: "/home/u",
};

function LocationProbe() {
  return <span data-testid="location">{useLocation().pathname}</span>;
}

function renderMarkdown(markdown: string, client = new QueryClient()): QueryClient {
  render(
    <MemoryRouter initialEntries={["/c/coordinator"]}>
      <QueryClientProvider client={client}>
        <FileViewerContext.Provider value={FILE_VIEWER}>
          <FilePathAwareMessageResponse>{markdown}</FilePathAwareMessageResponse>
        </FileViewerContext.Provider>
      </QueryClientProvider>
      <LocationProbe />
    </MemoryRouter>,
  );
  return client;
}

describe("bare session ids in chat", () => {
  it.each([
    ["inline code", `Open \`${WORKER}\` and tell it to go again.`],
    ["prose", `Open ${WORKER} and tell it to go again.`],
  ])("shows the session's title for an id in %s", async (_label, markdown) => {
    fetchMock.mockResolvedValue(
      jsonResponse({ id: WORKER, object: "session", title: "Build worker" }),
    );
    renderMarkdown(markdown);

    const pill = await screen.findByTestId("mention-pill");
    expect(pill.textContent).toBe("@Build worker");
    expect(pill).toHaveAttribute("href", `/c/${WORKER}`);
    expect(pill).toHaveAttribute("title", `Session ${WORKER}`);
    expect(pill.closest("p")?.textContent).toBe("Open @Build worker and tell it to go again.");
    expect(String(fetchMock.mock.calls[0]![0])).toContain(`/v1/sessions/${WORKER}`);
  });

  it.each([
    ["a browser", false],
    ["the desktop shell", true],
  ])("opens the session in the same view in %s", async (_label, native) => {
    nativeShell.native = native;
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);
    const client = new QueryClient();
    client.setQueryData(["session", WORKER], { title: "Build worker", parentSessionId: null });
    renderMarkdown(`Open \`${WORKER}\`.`, client);

    const pill = screen.getByTestId("mention-pill");
    // A plain click is cancelled and routed in-app: no page load, no new tab or window.
    expect(fireEvent.click(pill)).toBe(false);
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(`/c/${WORKER}`));
    expect(openSpy).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("marks a subagent's pill as a subagent", () => {
    const client = new QueryClient();
    client.setQueryData(["session", WORKER], { title: "researcher", parentSessionId: "s_parent" });
    renderMarkdown(`Ask ${WORKER}.`, client);
    expect(screen.getByTestId("mention-pill").querySelector("svg")).toHaveClass("lucide-bot");
  });

  it("keeps an id that names no readable session as written", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "not found" }, 404));
    const client = renderMarkdown(`Hash ${MISSING} and \`${MISSING}\`.`);

    await waitFor(() => expect(client.getQueryState(["session", MISSING])?.status).toBe("error"));
    // One lookup serves both mentions of the same id.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("mention-pill")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    const paragraph = screen.getByText(/^Hash/);
    expect(paragraph.textContent).toBe(`Hash ${MISSING} and ${MISSING}.`);
    expect(paragraph.querySelector("code")?.textContent).toBe(MISSING);
  });

  it("leaves ids in fenced code, links and paths alone without looking them up", () => {
    renderMarkdown(
      [
        "```",
        WORKER,
        "```",
        "",
        `[${WORKER}](https://example.com/run) and /tmp/${WORKER}/out.log`,
      ].join("\n"),
    );
    expect(screen.queryByTestId("mention-pill")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
