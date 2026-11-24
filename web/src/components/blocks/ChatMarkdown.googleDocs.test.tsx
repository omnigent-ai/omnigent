import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useMemo, type ReactNode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setOmnigentHostConfig } from "@/lib/host";
import { setSessionHostResolver } from "@/lib/identity";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { GoogleDocsSessionProvider } from "@/shell/GoogleDocsSessionContext";
import { GoogleDocsWorkspacePane } from "@/shell/GoogleDocsWorkspacePane";
import { FilePathAwareMessageResponse } from "./ChatMarkdown";

const A = "https://docs.google.com/document/d/example/edit?tab=t.one";
const B = "https://docs.google.com/document/u/2/d/other/edit?usp=sharing#tab=t.two";
let sessionId: string;
let sequence = 0;
let entries: { document_url: string; added_at: number }[];
const fetcher = vi.fn<typeof fetch>();
const reveal = vi.fn();
const methods = () => fetcher.mock.calls.map(([, init]) => init?.method ?? "GET");
function response(id = sessionId) {
  return Response.json({ session_id: id, entries });
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
function SessionViewer({ id, children }: { id: string; children: ReactNode }) {
  const value = useMemo(
    () => ({
      conversationId: id,
      openFile: () => {},
      openGithubTab: () => {},
      isChangedPath: () => false,
      workspaceRoot: null,
      workspaceHome: null,
    }),
    [id],
  );
  return <FileViewerContext.Provider value={value}>{children}</FileViewerContext.Provider>;
}
function tree({
  id = sessionId,
  permission = 2,
  href = B,
  pane = true,
  user = false,
  onReveal = reveal,
} = {}) {
  return (
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}
    >
      <SessionViewer id={id}>
        <GoogleDocsSessionProvider sessionId={id} permissionLevel={permission} onReveal={onReveal}>
          <FilePathAwareMessageResponse breaks={user} literalHtml={user}>
            {`[Document](${href})`}
          </FilePathAwareMessageResponse>
          {pane && <GoogleDocsWorkspacePane sessionId={id} visible={false} />}
        </GoogleDocsSessionProvider>
      </SessionViewer>
    </QueryClientProvider>
  );
}
beforeEach(() => {
  sessionId = `chat_docs_${++sequence}`;
  entries = [{ document_url: A, added_at: 1 }];
  reveal.mockReset();
  setOmnigentHostConfig({ serverIdentity: "backend-a" });
  setSessionHostResolver(null);
  fetcher.mockReset().mockImplementation(async (_input, init) => {
    if (init?.method === "POST") {
      entries = [{ document_url: JSON.parse(String(init.body)).url, added_at: 2 }, ...entries];
    }
    return response();
  });
  vi.stubGlobal("fetch", fetcher);
  vi.spyOn(window, "confirm").mockReturnValue(true);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it.each([false, true])(
  "preserves ordinary external navigation without writes or pane reveal (user=%s)",
  async (user) => {
    render(tree({ user }));
    await screen.findByRole("button", { name: "Add to session" });
    const link = screen.getByRole("link", { name: "Document" });
    expect(link).toHaveAttribute("href", B);
    expect(link).toHaveAttribute("target", "_blank");
    const popup = vi.spyOn(window, "open").mockReturnValue(null);
    const external = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    fireEvent.click(link);
    expect(popup).toHaveBeenCalledWith("about:blank", "_blank");
    expect(external).toHaveBeenCalledOnce();
    for (const modifiers of [
      { ctrlKey: true },
      { metaKey: true },
      { shiftKey: true },
      { altKey: true },
      { button: 1 },
    ]) {
      const event = new MouseEvent("click", { bubbles: true, cancelable: true, ...modifiers });
      link.dispatchEvent(event);
      expect(event.defaultPrevented).toBe(false);
    }
    expect(methods()).toEqual(["GET"]);
    expect(reveal).not.toHaveBeenCalled();
    expect(screen.getByTitle("Google Docs editor")).toHaveAttribute("src", A + "&embedded=true");
  },
);

it.each([false, true])(
  "attaches without confirmation and reveals only after committed success (user=%s)",
  async (user) => {
    render(tree({ user }));
    const add = await screen.findByRole("button", { name: "Add to session" });
    const editor = screen.getByTitle("Google Docs editor");
    vi.mocked(window.confirm).mockReturnValue(false);
    const saved = deferred<Response>();
    fetcher.mockImplementationOnce(() => saved.promise);
    fireEvent.click(add);
    await waitFor(() => expect(methods()).toEqual(["GET", "POST"]));
    expect(screen.getByTitle("Google Docs editor")).toBe(editor);
    expect(reveal).not.toHaveBeenCalled();
    entries = [{ document_url: B, added_at: 2 }, ...entries];
    await act(async () => saved.resolve(response()));
    await screen.findByRole("button", { name: "Open in Docs pane" });
    expect(screen.getByTitle("Google Docs editor")).toHaveAttribute(
      "src",
      "https://docs.google.com/document/d/other/edit?tab=t.two&embedded=true",
    );
    expect(reveal).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Open in Docs pane" }));
    expect(methods()).toEqual(["GET", "POST"]);
    expect(window.confirm).not.toHaveBeenCalled();
  },
);

it("lets a reader open an already linked document ID and tab without a write", async () => {
  vi.mocked(window.confirm).mockReturnValue(false);
  render(tree({ permission: 1, href: A.replace("t.one", "t.other") }));
  fireEvent.click(await screen.findByRole("button", { name: "Open in Docs pane" }));
  expect(screen.getByTitle("Google Docs editor")).toHaveAttribute(
    "src",
    A.replace("t.one", "t.other") + "&embedded=true",
  );
  expect(methods()).toEqual(["GET"]);
  expect(reveal).toHaveBeenCalledOnce();
  expect(window.confirm).not.toHaveBeenCalled();
});

it.each([
  B + "&resourcekey=access",
  B + "&heading=h.example",
  "https://docs.google.com/document/d/other/edit#heading=h.example",
  "https://docs.google.com/document/d/other/preview",
  "https://example.test/document/d/other/edit",
])("preserves unsupported/deep targets without attachment actions: %s", async (href) => {
  render(tree({ href }));
  await screen.findByTitle("Google Docs editor");
  expect(screen.getByRole("link", { name: "Document" })).toHaveAttribute("href", href);
  expect(screen.queryByRole("button", { name: "Add to session" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Open in Docs pane" })).toBeNull();
  expect(methods()).toEqual(["GET"]);
});

it("hides unlinked writes for initial loading, failures, and readers; retains the action during background refresh", async () => {
  const loading = deferred<Response>();
  fetcher.mockImplementationOnce(() => loading.promise);
  const view = render(tree());
  expect(screen.queryByRole("button", { name: "Add to session" })).toBeNull();
  await act(async () =>
    loading.resolve(Response.json({ error: { message: "List unavailable" } }, { status: 503 })),
  );
  expect(screen.queryByRole("button", { name: "Add to session" })).toBeNull();
  fireEvent.focus(window);
  const add = await screen.findByRole("button", { name: "Add to session" });
  const refreshing = deferred<Response>();
  fetcher.mockImplementationOnce(() => refreshing.promise);
  fireEvent.focus(window);
  expect(screen.getByRole("button", { name: "Add to session" })).toBe(add);
  await act(async () => refreshing.resolve(response()));
  expect(screen.getByRole("button", { name: "Add to session" })).toBe(add);
  view.rerender(tree({ permission: 1 }));
  expect(screen.queryByRole("button", { name: "Add to session" })).toBeNull();
  expect(methods().every((method) => method === "GET")).toBe(true);
});

it("shows failed explicit adds beside the chat link and leaves its editor hidden and unchanged", async () => {
  vi.mocked(window.confirm).mockReturnValue(false);
  render(tree());
  const add = await screen.findByRole("button", { name: "Add to session" });
  const editor = screen.getByTitle("Google Docs editor");
  fetcher.mockResolvedValueOnce(
    Response.json({ error: { message: "Cannot link document" } }, { status: 403 }),
  );
  fireEvent.click(add);
  await screen.findByText("Cannot link document", { selector: "span" });
  expect(screen.getByTitle("Google Docs editor")).toBe(editor);
  expect(editor).toHaveAttribute("src", A + "&embedded=true");
  expect(reveal).not.toHaveBeenCalled();
  expect(window.confirm).not.toHaveBeenCalled();
});

it.each(["session", "backend", "unmount"])(
  "ignores an add completion after leaving its %s",
  async (change) => {
    const view = render(tree());
    const add = await screen.findByRole("button", { name: "Add to session" });
    const saved = deferred<Response>();
    fetcher.mockImplementationOnce(() => saved.promise);
    fireEvent.click(add);
    await waitFor(() => expect(methods()).toContain("POST"));
    const oldId = sessionId;
    if (change === "session") {
      view.rerender(tree({ id: "other_session" }));
      view.rerender(tree());
    } else if (change === "backend") {
      act(() => setOmnigentHostConfig({ serverIdentity: "backend-b" }));
      view.rerender(tree());
    } else view.unmount();
    await act(async () => saved.resolve(response(oldId)));
    expect(reveal).not.toHaveBeenCalled();
  },
);

it("reveals with the current layout callback when a committed add outlives a layout change", async () => {
  const latestReveal = vi.fn();
  const view = render(tree());
  const add = await screen.findByRole("button", { name: "Add to session" });
  const saved = deferred<Response>();
  fetcher.mockImplementationOnce(() => saved.promise);
  fireEvent.click(add);
  await waitFor(() => expect(methods()).toContain("POST"));
  // The root changes its layout callback while preserving the same session/controller.
  view.rerender(tree({ onReveal: latestReveal }));
  entries = [{ document_url: B, added_at: 2 }, ...entries];
  await act(async () => saved.resolve(response()));
  expect(latestReveal).toHaveBeenCalledOnce();
  expect(reveal).not.toHaveBeenCalled();
});
