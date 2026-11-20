import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { writeAlwaysSteer } from "@/lib/alwaysSteerPreferences";
import { GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX, GOOGLE_DOCS_SELECTION_LABEL } from "@/lib/googleDocs";
import { type SessionUpdatesFrame, sessionUpdatesSocket } from "@/lib/sessionUpdatesSocket";
import { bindConversationForTest, initChatStore, useChatStore } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import { GoogleDocsWorkspacePane } from "./GoogleDocsWorkspacePane";

const SESSION_ID = "conv_docs_child";
const OTHER_SESSION_ID = "conv_docs_other";
const SESSION_PATH = `/v1/sessions/${SESSION_ID}`;
const FIRST_URL = "https://docs.google.com/document/d/first/edit";
const SECOND_URL = "https://docs.google.com/document/d/second/edit?tab=t.two";
const fetchMock = vi.fn<typeof fetch>();

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

function sessionResponse(id: string, url: string = FIRST_URL) {
  return Response.json({ id, labels: { [GOOGLE_DOCS_SELECTION_LABEL]: url } });
}

function confirmedSecondDocument() {
  return Response.json({
    id: SESSION_ID,
    labels: {
      [GOOGLE_DOCS_SELECTION_LABEL]: SECOND_URL,
      [GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX + "second"]: SECOND_URL,
    },
  });
}

function requests(method: string) {
  return fetchMock.mock.calls.filter(([, options]) => (options?.method ?? "GET") === method);
}

function renderWorkspace(
  overrides: Partial<React.ComponentProps<typeof GoogleDocsWorkspacePane>> = {},
) {
  const props = {
    sessionId: SESSION_ID,
    active: true,
    canEditReference: true,
    visible: true,
    ...overrides,
  };
  return { ...render(<GoogleDocsWorkspacePane {...props} />), props };
}

function click(name: string) {
  fireEvent.click(screen.getByRole("button", { name }));
}

function updateDraft(url: string) {
  click("Change document");
  fireEvent.change(screen.getByRole("textbox", { name: "Google Docs URL" }), {
    target: { value: url },
  });
  click("Open document");
}

beforeEach(() => {
  initChatStore(new QueryClient({ defaultOptions: { queries: { retry: false } } }));
  bindConversationForTest(SESSION_ID, {
    boundAgentId: "agent_child",
    abortController: new AbortController(),
    blocks: [],
    pendingUserMessages: [],
    status: "idle",
    sessionStatus: "idle",
  });
  useChatStore.setState({ queuedMessages: [] });
  localStorage.clear();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  useChatStore.getState().abortController?.abort();
  conversationRegistry.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("restores a legacy saved Doc for a viewer and keeps the same iframe while hidden", async () => {
  fetchMock.mockResolvedValueOnce(
    sessionResponse(SESSION_ID, FIRST_URL + "?usp=sharing#tab=t.one"),
  );
  const pane = renderWorkspace({ canEditReference: false });
  const iframe = await screen.findByTitle("Google Docs editor");
  expect(iframe).toHaveAttribute("src", FIRST_URL + "?tab=t.one&embedded=true");
  expect(screen.getByRole("link", { name: "Open in Google Docs" })).toHaveAttribute(
    "href",
    FIRST_URL + "?tab=t.one",
  );
  expect(screen.queryByRole("textbox", { name: "Google Docs URL" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Request agent revision" })).not.toBeInTheDocument();

  pane.rerender(<GoogleDocsWorkspacePane {...pane.props} visible={false} />);
  expect(iframe).not.toBeVisible();
  pane.rerender(<GoogleDocsWorkspacePane {...pane.props} />);
  expect(screen.getByTitle("Google Docs editor")).toBe(iframe);
  expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual([SESSION_PATH + "/labels"]);
});

it("preserves the editor and draft until a failed save is explicitly retried and confirmed", async () => {
  fetchMock
    .mockResolvedValueOnce(sessionResponse(SESSION_ID))
    .mockResolvedValueOnce(sessionResponse(SESSION_ID))
    .mockRejectedValueOnce(new Error("Connection lost"))
    .mockResolvedValueOnce(sessionResponse(SESSION_ID))
    .mockResolvedValueOnce(confirmedSecondDocument());
  vi.spyOn(window, "confirm").mockReturnValue(true);
  renderWorkspace();
  const frame = await screen.findByTitle("Google Docs editor");
  const draft = SECOND_URL.replace("?tab=", "#tab=");
  updateDraft(draft);

  expect(await screen.findByRole("alert")).toHaveTextContent("Connection lost");
  expect(screen.getByRole("textbox", { name: "Google Docs URL" })).toHaveValue(draft);
  expect(screen.getByTitle("Google Docs editor")).toBe(frame);
  click("Retry saving document");
  await waitFor(() =>
    expect(screen.getByTitle("Google Docs editor")).toHaveAttribute(
      "src",
      SECOND_URL + "&embedded=true",
    ),
  );
  expect(
    requests("PATCH").map(([url, options]) => [
      url,
      JSON.parse(String(options?.body)).labels[GOOGLE_DOCS_SELECTION_LABEL],
    ]),
  ).toEqual([
    [SESSION_PATH, SECOND_URL],
    [SESSION_PATH, SECOND_URL],
  ]);
});

it("keeps the confirmed editor on a failed refresh and offers a switch after retry", async () => {
  let receiveFrame: ((frame: SessionUpdatesFrame) => void) | undefined;
  vi.spyOn(sessionUpdatesSocket, "subscribe").mockImplementation((listener) => {
    receiveFrame = listener;
    return () => {
      receiveFrame = undefined;
    };
  });
  fetchMock
    .mockResolvedValueOnce(sessionResponse(SESSION_ID))
    .mockRejectedValueOnce(new Error("Load unavailable"))
    .mockResolvedValueOnce(sessionResponse(SESSION_ID, SECOND_URL));
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  renderWorkspace();
  const frame = await screen.findByTitle("Google Docs editor");

  act(() =>
    receiveFrame?.({
      type: "changed",
      items: [{ id: SESSION_ID, labels: { [GOOGLE_DOCS_SELECTION_LABEL]: SECOND_URL } }],
    }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent("Load unavailable");
  expect(screen.getByTitle("Google Docs editor")).toBe(frame);
  click("Retry loading document");
  const switchButton = await screen.findByRole("button", { name: "Switch to saved document" });
  fireEvent.click(switchButton);
  expect(screen.getByTitle("Google Docs editor")).toBe(frame);
  confirm.mockReturnValue(true);
  fireEvent.click(switchButton);
  expect(screen.getByTitle("Google Docs editor")).toHaveAttribute(
    "src",
    SECOND_URL + "&embedded=true",
  );
  expect(requests("PATCH")).toEqual([]);
});

it("handles an incoming link once, through confirmation and the session save", async () => {
  const request = { id: "open-doc", sessionId: SESSION_ID, canonicalUrl: SECOND_URL };
  const acknowledge = vi.fn();
  fetchMock.mockImplementation(async (url, options) => {
    if (url === SESSION_PATH + "/labels") return sessionResponse(SESSION_ID);
    expect(url).toBe(SESSION_PATH);
    expect(options?.method).toBe("PATCH");
    return confirmedSecondDocument();
  });
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const pane = renderWorkspace({ onOpenRequestHandled: acknowledge });
  const frame = await screen.findByTitle("Google Docs editor");
  pane.rerender(
    <GoogleDocsWorkspacePane
      {...pane.props}
      openRequest={{ ...request, sessionId: OTHER_SESSION_ID }}
    />,
  );
  expect(acknowledge).not.toHaveBeenCalled();

  pane.rerender(<GoogleDocsWorkspacePane {...pane.props} openRequest={request} />);
  expect(acknowledge).toHaveBeenCalledWith(request);
  expect(confirm).toHaveBeenCalledOnce();
  expect(requests("PATCH")).toHaveLength(0);
  expect(screen.getByTitle("Google Docs editor")).toBe(frame);

  confirm.mockReturnValue(true);
  const nextRequest = { ...request, id: "open-doc-again" };
  pane.rerender(<GoogleDocsWorkspacePane {...pane.props} openRequest={nextRequest} />);
  await waitFor(() =>
    expect(screen.getByTitle("Google Docs editor")).toHaveAttribute(
      "src",
      SECOND_URL + "&embedded=true",
    ),
  );
  pane.rerender(<GoogleDocsWorkspacePane {...pane.props} openRequest={{ ...nextRequest }} />);
  pane.rerender(
    <GoogleDocsWorkspacePane
      {...pane.props}
      openRequest={{ ...nextRequest, id: "already-open" }}
    />,
  );
  pane.rerender(<GoogleDocsWorkspacePane {...pane.props} openRequest={request} />);
  expect(acknowledge).toHaveBeenCalledTimes(3);
  expect(confirm).toHaveBeenCalledTimes(2);
  expect(requests("PATCH")).toHaveLength(1);
});

it("keeps a late save in its own session after the active session changes", async () => {
  const patch = deferred<Response>();
  fetchMock.mockImplementation(async (url, options) => {
    if (url === SESSION_PATH + "/labels") return sessionResponse(SESSION_ID);
    if (url === SESSION_PATH && options?.method === "PATCH") return patch.promise;
    expect(url).toBe(`/v1/sessions/${OTHER_SESSION_ID}/labels`);
    return sessionResponse(OTHER_SESSION_ID, "");
  });
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const pane = renderWorkspace();
  await screen.findByTitle("Google Docs editor");
  updateDraft(SECOND_URL);
  await waitFor(() => expect(requests("PATCH")).toHaveLength(1));

  pane.rerender(<GoogleDocsWorkspacePane {...pane.props} sessionId={OTHER_SESSION_ID} />);
  await screen.findByRole("textbox", { name: "Google Docs URL" });
  await act(async () => patch.resolve(confirmedSecondDocument()));
  expect(screen.queryByTitle("Google Docs editor")).not.toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "Google Docs URL" })).toHaveValue("");
});

it("posts one normal user message to the active child chat with the URL captured before a change", async () => {
  const post = deferred<Response>();
  fetchMock.mockImplementation(async (url, options) => {
    if (url === SESSION_PATH + "/labels") return sessionResponse(SESSION_ID);
    if (url === SESSION_PATH && options?.method === "PATCH") return confirmedSecondDocument();
    expect(url).toBe(SESSION_PATH + "/events");
    return post.promise;
  });
  vi.spyOn(window, "confirm").mockReturnValue(true);
  renderWorkspace();
  await screen.findByTitle("Google Docs editor");

  const request = screen.getByRole("button", { name: "Request agent revision" });
  fireEvent.click(request);
  fireEvent.click(request);
  await waitFor(() => expect(requests("POST")).toHaveLength(1));
  updateDraft(SECOND_URL);
  await waitFor(() =>
    expect(screen.getByTitle("Google Docs editor")).toHaveAttribute(
      "src",
      SECOND_URL + "&embedded=true",
    ),
  );
  await act(async () => post.resolve(Response.json({ queued: true, item_id: "ci_revision" })));

  const [[url, options]] = requests("POST");
  expect(url).toBe(SESSION_PATH + "/events");
  expect(JSON.parse(String(options?.body))).toMatchObject({
    type: "message",
    data: {
      role: "user",
      content: [{ type: "input_text", text: expect.stringContaining(FIRST_URL) }],
    },
  });
  expect(JSON.parse(String(options?.body)).data.content[0].text).toContain(
    "native comment threads",
  );
  expect(JSON.parse(String(options?.body)).data.content[0].text).not.toContain(SECOND_URL);
});

it("queues only one revision for a busy chat when always-steer is disabled", async () => {
  writeAlwaysSteer(false);
  useChatStore.setState({ status: "streaming", sessionStatus: "running" });
  fetchMock.mockResolvedValueOnce(sessionResponse(SESSION_ID));
  renderWorkspace();
  await screen.findByTitle("Google Docs editor");

  await act(async () => click("Request agent revision"));
  await act(async () => click("Request agent revision"));
  expect(useChatStore.getState().queuedMessages).toEqual([
    expect.objectContaining({
      conversationId: SESSION_ID,
      text: expect.stringContaining(FIRST_URL),
    }),
  ]);
  expect(requests("POST")).toEqual([]);
});
