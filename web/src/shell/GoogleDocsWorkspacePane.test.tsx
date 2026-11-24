import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { initChatStore, useChatStore } from "@/store/chatStore";
import { writeAlwaysSteer } from "@/lib/alwaysSteerPreferences";
import { setOmnigentHostConfig } from "@/lib/host";
import { resolveIdentity, setSessionHostResolver } from "@/lib/identity";
import { Link, useNavigate } from "@/lib/routing";
import { GoogleDocsWorkspacePane } from "./GoogleDocsWorkspacePane";
import { ResponsiveViewerSlot } from "./ResponsiveViewerSlot";
import {
  GoogleDocsSessionProvider,
  useGoogleDocsSession,
  type GoogleDocsSession,
} from "./GoogleDocsSessionContext";

const A = "https://docs.google.com/document/u/2/d/A/edit?usp=sharing#tab=t.one";
const B = "https://docs.google.com/document/d/B/edit?tab=t.two";
const canonicalA = "https://docs.google.com/document/d/A/edit?tab=t.one&embedded=true";
interface Entry {
  document_url: string;
  added_at: number;
}
const fetchMock = vi.fn<typeof fetch>();
let entries: Entry[];
let sessionId: string;
let sequence = 0;
let observedSession: GoogleDocsSession;
const response = (items = entries, id = sessionId) =>
  Response.json({ session_id: id, entries: items });
const methods = () => fetchMock.mock.calls.map(([, init]) => init?.method ?? "GET");
const frame = () => screen.getByTitle("Google Docs editor");
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
async function server(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const id = decodeURIComponent(String(input).split("/sessions/")[1].split("/")[0]);
  if (init?.method === "POST") {
    const url = JSON.parse(init.body as string).url as string;
    const doc = url.split("/d/")[1].split(/[/?#]/)[0];
    if (!entries.some((entry) => entry.document_url.includes(`/d/${doc}/`)))
      entries = [{ document_url: url, added_at: 30 }, ...entries];
  }
  if (init?.method === "DELETE") {
    const doc = String(input).split("/").at(-1)!;
    entries = entries.filter((entry) => !entry.document_url.includes(`/d/${doc}/`));
  }
  return response(entries, id);
}
function Intents() {
  const state = useGoogleDocsSession()!;
  observedSession = state;
  return (
    <>
      <output aria-label="B membership">{state.membership(B)}</output>
      <button type="button" onClick={() => state.openLinked(B)}>
        Open B intent
      </button>
      <button type="button" onClick={() => void state.addAndOpen(B)}>
        Add B intent
      </button>
      <button
        type="button"
        onClick={() =>
          void state.addAndOpen("https://docs.google.com/document/d/A/edit?tab=t.other")
        }
      >
        Add A tab intent
      </button>
      <button
        type="button"
        onClick={() => {
          const entry = state.entries.find((item) => item.documentUrl === B);
          if (entry) void state.remove(entry);
        }}
      >
        Remove B intent
      </button>
    </>
  );
}
function tree(id = sessionId, permission = 2, visible = true) {
  return (
    <GoogleDocsSessionProvider sessionId={id} permissionLevel={permission}>
      <Intents />
      <GoogleDocsWorkspacePane sessionId={id} visible={visible} permissionLevel={permission} />
    </GoogleDocsSessionProvider>
  );
}
function DocsRoute() {
  const navigate = useNavigate();
  return (
    <>
      {tree()}
      <Link to="/settings">Settings</Link>
      <button type="button" onClick={() => navigate("/c/other")}>
        Other session
      </button>
    </>
  );
}
beforeEach(() => {
  sessionId = `docs_${++sequence}`;
  entries = [
    { document_url: A, added_at: 20 },
    { document_url: B, added_at: 10 },
  ];
  fetchMock.mockReset().mockImplementation(server);
  vi.stubGlobal("fetch", fetchMock);
  setOmnigentHostConfig({ serverIdentity: "backend-a" });
  setSessionHostResolver(null);
  vi.spyOn(window, "confirm").mockReturnValue(true);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("restores newest, preserves the original external URL, and retains the same hidden iframe", async () => {
  const view = render(tree());
  const editor = await screen.findByTitle("Google Docs editor");
  expect(editor).toHaveAttribute("src", canonicalA);
  expect(editor).toHaveAttribute("sandbox", expect.stringContaining("allow-same-origin"));
  expect(
    screen.getAllByRole("link", { name: "Open in Google Docs", hidden: true })[0],
  ).toHaveAttribute("href", A);
  view.rerender(tree(sessionId, 2, false));
  expect(frame()).toBe(editor);
  view.rerender(tree());
  expect(frame()).toBe(editor);
  expect(methods()).toEqual(["GET"]);
});

it.each(["Settings", "Other session"])(
  "leaves the real Docs pane for %s without confirmation",
  async (action) => {
    vi.mocked(window.confirm).mockReturnValue(false);
    render(
      <MemoryRouter initialEntries={["/c/one"]}>
        <Routes>
          <Route path="/c/one" element={<DocsRoute />} />
          <Route path="*" element={<p>Destination</p>} />
        </Routes>
      </MemoryRouter>,
    );
    const editor = await screen.findByTitle("Google Docs editor");
    fireEvent.click(screen.getByText(action));
    expect(screen.getByText("Destination")).toBeVisible();
    expect(editor).not.toBeInTheDocument();
    expect(window.confirm).not.toHaveBeenCalled();
  },
);

it("moves the real Docs pane between layouts without confirmation and retains its iframe", async () => {
  vi.mocked(window.confirm).mockReturnValue(false);
  const target = document.createElement("div");
  document.body.appendChild(target);
  const slot = (mobile: boolean) => (
    <ResponsiveViewerSlot mobile={mobile} mobileTarget={target}>
      {tree()}
    </ResponsiveViewerSlot>
  );
  const view = render(slot(false));
  try {
    const editor = await screen.findByTitle("Google Docs editor");
    for (const mobile of [true, false, true]) {
      view.rerender(slot(mobile));
      expect(target.contains(editor)).toBe(mobile);
      expect(frame()).toBe(editor);
      expect(editor).toHaveAttribute("src", canonicalA);
      expect(screen.queryByRole("button", { name: "Move editor" })).not.toBeInTheDocument();
    }
    expect(methods()).toEqual(["GET"]);
    expect(window.confirm).not.toHaveBeenCalled();
  } finally {
    view.unmount();
    target.remove();
  }
});

it("coalesces focus and picker refreshes while the initial collection is loading", async () => {
  const pending = deferred<Response>();
  fetchMock.mockImplementationOnce(() => pending.promise);
  render(tree());
  expect(screen.getByText("Loading session documents…")).toBeVisible();
  await waitFor(() => expect(methods()).toEqual(["GET"]));
  const user = userEvent.setup();
  await user.click(screen.getByRole("combobox", { name: "Session document" }));
  fireEvent(window, new Event("focus"));
  expect(methods()).toEqual(["GET"]);
  expect(screen.getByText("Loading session documents…")).toBeVisible();
  await act(async () => pending.resolve(response()));
  await screen.findByTitle("Google Docs editor");
  expect(screen.queryByText("Loading session documents…")).toBeNull();
});

it("refreshes the open picker without a loading row or requests from internal focus moves", async () => {
  render(tree());
  const editor = await screen.findByTitle("Google Docs editor");
  const pending = deferred<Response>();
  fetchMock.mockImplementationOnce(() => pending.promise);
  const user = userEvent.setup();
  const trigger = screen.getByRole("combobox", { name: "Session document" });
  const add = screen.getByRole("button", { name: "Add document" });
  await user.click(trigger);
  await waitFor(() => expect(methods()).toEqual(["GET", "GET"]));
  await user.keyboard("{ArrowDown}{ArrowUp}");
  fireEvent(window, new Event("focus"));
  expect(methods()).toEqual(["GET", "GET"]);
  expect(screen.queryByText("Loading session documents…")).toBeNull();
  expect(frame()).toBe(editor);
  await act(async () => pending.resolve(response(entries.slice(0, 1))));
  await waitFor(() => expect(screen.queryByRole("option", { name: B })).toBeNull());
  await user.keyboard("{ArrowDown}{ArrowUp}");
  expect(methods()).toEqual(["GET", "GET"]);
  fireEvent.focus(add, { relatedTarget: trigger });
  expect(methods()).toEqual(["GET", "GET"]);
  expect(frame()).toBe(editor);
  expect(screen.queryByText("Loading session documents…")).toBeNull();
});

it("opens linked documents through the shared seam and dropdown without writing", async () => {
  vi.mocked(window.confirm).mockReturnValue(false);
  render(tree(sessionId, 1));
  await screen.findByTitle("Google Docs editor");
  expect(screen.getByLabelText("B membership")).toHaveTextContent("linked");
  expect(screen.getByRole("button", { name: "Add document" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Remove document" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Add B intent" }));
  fireEvent.click(screen.getByRole("button", { name: "Remove B intent" }));
  fireEvent.click(screen.getByRole("button", { name: "Open B intent" }));
  expect(frame()).toHaveAttribute("src", B + "&embedded=true");
  const user = userEvent.setup();
  await user.click(screen.getByRole("combobox", { name: "Session document" }));
  await user.click(await screen.findByRole("option", { name: A }));
  expect(frame()).toHaveAttribute("src", canonicalA);
  expect(methods().every((method) => method === "GET")).toBe(true);
  expect(window.confirm).not.toHaveBeenCalled();
});

it("adds without confirmation and waits for committed success before navigating even when focus refreshes", async () => {
  entries = entries.slice(0, 1);
  render(tree());
  const editor = await screen.findByTitle("Google Docs editor");
  vi.mocked(window.confirm).mockReturnValue(false);
  const saved = deferred<Response>();
  fetchMock.mockImplementationOnce(() => saved.promise);
  fireEvent.click(screen.getByRole("button", { name: "Add B intent" }));
  await waitFor(() => expect(methods()).toEqual(["GET", "POST"]));
  fireEvent(window, new Event("focus"));
  expect(methods()).toEqual(["GET", "POST"]);
  expect(frame()).toBe(editor);
  entries = [{ document_url: B, added_at: 30 }, ...entries];
  await act(async () => saved.resolve(response()));
  await waitFor(() => expect(frame()).toHaveAttribute("src", B + "&embedded=true"));
  await waitFor(() => expect(methods()).toEqual(["GET", "POST", "GET"]));
  expect(window.confirm).not.toHaveBeenCalled();
});

it("keeps the shared entry unchanged while explicitly opening another tab of its document", async () => {
  render(tree());
  await screen.findByTitle("Google Docs editor");
  const before = structuredClone(entries);
  fireEvent.click(screen.getByRole("button", { name: "Add A tab intent" }));
  await waitFor(() =>
    expect(frame()).toHaveAttribute(
      "src",
      "https://docs.google.com/document/d/A/edit?tab=t.other&embedded=true",
    ),
  );
  expect(entries).toEqual(before);
  expect(methods()).toEqual(["GET", "POST"]);
});

it("confirms active removal, retains its editor until DELETE succeeds, and never auto-selects a fallback", async () => {
  render(tree());
  const editor = await screen.findByTitle("Google Docs editor");
  vi.mocked(window.confirm).mockReturnValueOnce(false);
  fireEvent.click(screen.getByRole("button", { name: "Remove document" }));
  expect(methods()).toEqual(["GET"]);
  expect(frame()).toBe(editor);
  expect(editor).toHaveAttribute("src", canonicalA);
  expect(window.confirm).toHaveBeenCalledExactlyOnceWith(
    "Remove this document from the shared session? The Google Doc will not be deleted.",
  );
  const removed = deferred<Response>();
  fetchMock.mockImplementationOnce(() => removed.promise);
  fireEvent.click(screen.getByRole("button", { name: "Remove document" }));
  await waitFor(() => expect(methods()).toEqual(["GET", "DELETE"]));
  expect(window.confirm).toHaveBeenCalledTimes(2);
  expect(frame()).toBe(editor);
  entries = entries.slice(1);
  await act(async () => removed.resolve(response()));
  await waitFor(() => expect(screen.queryByTitle("Google Docs editor")).toBeNull());
  fireEvent(window, new Event("focus"));
  await waitFor(() => expect(methods()).toEqual(["GET", "DELETE", "GET"]));
  expect(screen.queryByTitle("Google Docs editor")).toBeNull();
});

it("preserves the active editor through another-document removal and remote membership updates", async () => {
  render(tree());
  const editor = await screen.findByTitle("Google Docs editor");
  fireEvent.click(screen.getByRole("button", { name: "Remove B intent" }));
  await waitFor(() => expect(screen.getByLabelText("B membership")).toHaveTextContent("unlinked"));
  expect(window.confirm).not.toHaveBeenCalled();
  expect(frame()).toBe(editor);
  entries = [{ document_url: B, added_at: 50 }];
  fireEvent(window, new Event("focus"));
  await screen.findByText(/no longer linked/);
  expect(frame()).toBe(editor);
  expect(editor).toHaveAttribute("src", canonicalA);
});

it.each(["add", "remove"])(
  "reconciles lost %s outcomes with GET and preserves the iframe on failure",
  async (operation) => {
    entries = entries.slice(0, 1);
    render(tree());
    const editor = await screen.findByTitle("Google Docs editor");
    fetchMock.mockRejectedValueOnce(new Error("response lost"));
    fireEvent.click(
      screen.getByRole("button", {
        name: operation === "add" ? "Add B intent" : "Remove document",
      }),
    );
    await screen.findByRole("alert");
    expect(frame()).toBe(editor);
    entries = operation === "add" ? [{ document_url: B, added_at: 30 }, ...entries] : [];
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() =>
      expect(methods()).toEqual(["GET", operation === "add" ? "POST" : "DELETE", "GET"]),
    );
    if (operation === "remove")
      await waitFor(() => expect(screen.queryByTitle("Google Docs editor")).toBeNull());
    else await waitFor(() => expect(frame()).toHaveAttribute("src", B + "&embedded=true"));
  },
);

it("does not replay an uncertain removal after another participant re-adds the document", async () => {
  render(tree());
  const editor = await screen.findByTitle("Google Docs editor");
  fetchMock.mockRejectedValueOnce(new Error("lost"));
  fireEvent.click(screen.getByRole("button", { name: "Remove document" }));
  await screen.findByRole("alert");
  entries = [{ document_url: A, added_at: 100 }];
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await screen.findByText(/added again/);
  expect(frame()).toBe(editor);
  expect(methods()).toEqual(["GET", "DELETE", "GET"]);
});

it.each(["add", "remove"])(
  "accepts a fresh explicit %s after refreshing an unconfirmed outcome",
  async (operation) => {
    entries = entries.slice(0, 1);
    render(tree());
    const editor = await screen.findByTitle("Google Docs editor");
    const action = operation === "add" ? "Add B intent" : "Remove document";
    const method = operation === "add" ? "POST" : "DELETE";
    fetchMock.mockRejectedValueOnce(new Error("response lost"));
    fireEvent.click(screen.getByRole("button", { name: action }));
    await screen.findByRole("alert");
    if (operation === "remove") entries = [{ document_url: A, added_at: 100 }];
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText(/could not be confirmed/);
    expect(methods()).toEqual(["GET", method, "GET"]);
    expect(frame()).toBe(editor);

    fireEvent(window, new Event("focus"));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(frame()).toBe(editor);
    fireEvent.click(screen.getByRole("button", { name: action }));
    if (operation === "add")
      await waitFor(() => expect(frame()).toHaveAttribute("src", B + "&embedded=true"));
    else
      await waitFor(() =>
        expect(screen.queryByTitle("Google Docs editor")).not.toBeInTheDocument(),
      );
    expect(methods()).toEqual(["GET", method, "GET", "GET", method]);
  },
);

it.each(["session", "backend", "unmount", "return"])(
  "ignores an add completion after %s changes",
  async (change) => {
    entries = entries.slice(0, 1);
    const view = render(tree());
    await screen.findByTitle("Google Docs editor");
    const pending = deferred<Response>();
    fetchMock.mockImplementationOnce(() => pending.promise);
    fireEvent.click(screen.getByRole("button", { name: "Add B intent" }));
    await waitFor(() => expect(methods()).toContain("POST"));
    const original = sessionId;
    if (change === "backend") {
      setOmnigentHostConfig({ serverIdentity: "backend-b" });
      view.rerender(tree());
    }
    if (change === "session" || change === "return") {
      view.rerender(tree(original + "_other"));
      await waitFor(() => expect(methods().filter((m) => m === "GET")).toHaveLength(2));
    }
    if (change === "return") view.rerender(tree(original));
    if (change === "unmount") view.unmount();
    await act(async () => pending.resolve(response([{ document_url: B, added_at: 30 }], original)));
    if (change === "unmount") expect(screen.queryByTitle("Google Docs editor")).toBeNull();
    else {
      await screen.findByTitle("Google Docs editor");
      expect(frame()).toHaveAttribute("src", canonicalA);
    }
  },
);

it("adds from empty state with compact controls without beforeunload warnings", async () => {
  entries = [];
  vi.mocked(window.confirm).mockReturnValue(false);
  const view = render(tree());
  await waitFor(() => expect(screen.queryByText("Loading session documents…")).toBeNull());
  expect(screen.queryByTitle("Google Docs editor")).toBeNull();
  fireEvent.change(screen.getByLabelText("Google Docs URL"), { target: { value: B } });
  fireEvent.click(screen.getByRole("button", { name: "Add to session" }));
  await screen.findByTitle("Google Docs editor");
  const unload = new Event("beforeunload", { cancelable: true });
  const preventDefault = vi.spyOn(unload, "preventDefault");
  window.dispatchEvent(unload);
  expect(unload.defaultPrevented).toBe(false);
  expect(preventDefault).not.toHaveBeenCalled();
  expect(window.confirm).not.toHaveBeenCalled();
  for (const name of [/Reload/, /Refresh history/, /Create/, /Close editor/])
    expect(screen.queryByRole("button", { name })).toBeNull();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Document actions" }));
  expect(await screen.findByRole("menuitem", { name: "Open in Google Docs" })).toHaveAttribute(
    "href",
    B,
  );
  view.unmount();
  const after = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(after);
  expect(after.defaultPrevented).toBe(false);
});

it("preserves the editor after a failed list update and retries without a write", async () => {
  render(tree());
  const editor = await screen.findByTitle("Google Docs editor");
  fetchMock.mockRejectedValueOnce(new Error("list unavailable"));
  fireEvent(window, new Event("focus"));
  await screen.findByRole("alert");
  expect(frame()).toBe(editor);
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  expect(methods().every((method) => method === "GET")).toBe(true);
  expect(frame()).toBe(editor);
});

it("rejects an identity change while a mutation is outstanding using the real identity subscription", async () => {
  render(tree());
  await screen.findByTitle("Google Docs editor");
  const pending = deferred<Response>();
  fetchMock.mockImplementationOnce(() => pending.promise);
  fireEvent.click(screen.getByRole("button", { name: "Add B intent" }));
  await waitFor(() => expect(methods()).toContain("POST"));
  fetchMock.mockResolvedValueOnce(
    Response.json({ user_id: "viewer@example.test", is_admin: false }),
  );
  await act(async () => {
    await resolveIdentity();
  });
  await act(async () => pending.resolve(response([{ document_url: B, added_at: 30 }])));
  await screen.findByTitle("Google Docs editor");
  expect(frame()).toHaveAttribute("src", canonicalA);
  expect(screen.queryByRole("alert")).toBeNull();
});

it("rejects captured intents after leaving and returning to the same session", async () => {
  const original = sessionId;
  const view = render(tree());
  await screen.findByTitle("Google Docs editor");
  const staleAdd = observedSession.addAndOpen;
  const staleOpen = observedSession.openLinked;
  view.rerender(tree(original + "_other"));
  await waitFor(() => expect(methods().filter((method) => method === "GET")).toHaveLength(2));
  view.rerender(tree(original));
  await waitFor(() => expect(methods().filter((method) => method === "GET")).toHaveLength(3));
  await screen.findByTitle("Google Docs editor");
  const before = fetchMock.mock.calls.length;
  expect(await staleAdd(B)).toBe(false);
  expect(staleOpen(B)).toBe(false);
  expect(fetchMock).toHaveBeenCalledTimes(before);
  expect(frame()).toHaveAttribute("src", canonicalA);
});

describe("Request agent review", () => {
  beforeEach(() => {
    writeAlwaysSteer(false);
    initChatStore(new QueryClient({ defaultOptions: { queries: { retry: false } } }));
    useChatStore.setState({
      conversationId: sessionId,
      boundAgentId: "agent",
      abortController: new AbortController(),
      blocks: [],
      pendingUserMessages: [],
      queuedMessages: [],
      status: "idle",
      sessionStatus: "idle",
      startupPending: false,
    });
    fetchMock.mockImplementation(async (input, init) =>
      String(input).endsWith("/events")
        ? Response.json({ queued: true, item_id: "review_input" })
        : server(input, init),
    );
  });
  afterEach(() => {
    useChatStore.getState().abortController?.abort();
  });
  const posts = () =>
    fetchMock.mock.calls.filter(
      ([input, init]) => String(input).endsWith("/events") && init?.method === "POST",
    );

  it("captures the displayed original URL while queued, even when another document opens", async () => {
    useChatStore.setState({ status: "streaming", sessionStatus: "running" });
    render(tree());
    const editor = await screen.findByTitle("Google Docs editor");
    fireEvent.click(screen.getByRole("button", { name: "Request agent review" }));
    await waitFor(() => expect(useChatStore.getState().queuedMessages).toHaveLength(1));
    expect(useChatStore.getState().queuedMessages[0].text).toContain(A);
    expect(frame()).toBe(editor);
    fireEvent.click(screen.getByRole("button", { name: "Open B intent" }));
    useChatStore.setState({ status: "idle", sessionStatus: "idle" });
    useChatStore.getState().maybeFlushQueuedHead();
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(String(posts()[0][1]?.body)).toContain(A);
    expect(String(posts()[0][1]?.body)).not.toContain(B);
    expect(frame()).toHaveAttribute("src", B + "&embedded=true");
  });

  it("provides the same captured review action in the compact menu and disables it for readers", async () => {
    const view = render(tree(sessionId, 1));
    await screen.findByTitle("Google Docs editor");
    expect(screen.getByRole("button", { name: "Request agent review" })).toBeDisabled();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Document actions" }));
    expect(screen.getByRole("menuitem", { name: "Request agent review" })).toHaveAttribute(
      "data-disabled",
    );
    await user.keyboard("{Escape}");
    view.rerender(tree());
    await user.click(screen.getByRole("button", { name: "Document actions" }));
    await user.click(screen.getByRole("menuitem", { name: "Request agent review" }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(String(posts()[0][1]?.body)).toContain(A);
  });

  it("surfaces policy denial without replacing the iframe", async () => {
    render(tree());
    const editor = await screen.findByTitle("Google Docs editor");
    fetchMock.mockImplementation(async (input, init) =>
      String(input).endsWith("/events") ? Response.json({ denied: true }) : server(input, init),
    );
    fireEvent.click(screen.getByRole("button", { name: "Request agent review" }));
    await screen.findByText("The session’s policy blocked the document review request.");
    expect(frame()).toBe(editor);
  });

  it("rejects queued review after permission loss and a departed activation", async () => {
    useChatStore.setState({ status: "streaming", sessionStatus: "running" });
    const view = render(tree());
    await screen.findByTitle("Google Docs editor");
    fireEvent.click(screen.getByRole("button", { name: "Request agent review" }));
    await waitFor(() => expect(useChatStore.getState().queuedMessages).toHaveLength(1));
    view.rerender(tree(sessionId, 1));
    useChatStore.setState({ status: "idle", sessionStatus: "idle" });
    useChatStore.getState().maybeFlushQueuedHead();
    await waitFor(() => expect(useChatStore.getState().queuedMessages).toEqual([]));
    expect(posts()).toHaveLength(0);
  });
});

it("rejects captured mutation callbacks after session edit access is revoked", async () => {
  const view = render(tree());
  await screen.findByTitle("Google Docs editor");
  const captured = observedSession;
  view.rerender(tree(sessionId, 1));
  await act(async () => {
    expect(await captured.addAndOpen(B)).toBe(false);
    expect(await captured.remove(captured.entries[0])).toBe(false);
  });
  expect(methods()).toEqual(["GET"]);
});
