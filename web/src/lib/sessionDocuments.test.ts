import { beforeEach, expect, it, vi } from "vitest";
import type * as IdentityModule from "./identity";
import type * as HostModule from "./host";
import {
  addSessionDocument,
  loadSessionDocuments,
  orderedSessionDocuments,
  removeSessionDocument,
  sessionDocumentsScope,
  type SessionDocumentEntry,
} from "./sessionDocuments";

// Only the HTTP transport and current connection are replaced; real helpers run below.
const context = vi.hoisted(() => ({
  user: "alice" as string | null,
  server: "workspace-a" as string | null,
  fetcher: undefined as ((path: string, init?: RequestInit) => Promise<Response>) | undefined,
}));
vi.mock("./identity", async (original) => ({
  ...(await original<typeof IdentityModule>()),
  authenticatedFetch: (...args: Parameters<typeof fetch>) => fetch(...args),
  getCurrentUserId: () => context.user,
}));
vi.mock("./host", async (original) => ({
  ...(await original<typeof HostModule>()),
  getOmnigentServerIdentity: () => context.server,
  getOmnigentHostConfig: () => ({ fetcher: context.fetcher }),
}));

const fetchMock = vi.fn<typeof fetch>();
const A = "https://docs.google.com/document/d/A/edit";
const B = "https://docs.google.com/document/d/B/edit?tab=t.two";
const entry = { documentUrl: A, addedAt: 10 };
let session: string;
let sequence = 0;
function response(entries: SessionDocumentEntry[] = [], id = session) {
  return Response.json({
    session_id: id,
    entries: entries.map((item) => ({ document_url: item.documentUrl, added_at: item.addedAt })),
  });
}
function methods() {
  return fetchMock.mock.calls.map(([, init]) => init?.method ?? "GET");
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { resolve, promise };
}

beforeEach(() => {
  session = `session-${++sequence}`;
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  context.user = "alice";
  context.server = "workspace-a";
  context.fetcher = undefined;
  window.__OMNIGENT_BASE_PATH__ = "";
});

it("loads shared references without persistent browser storage and orders timestamp ties by identity", async () => {
  const read = vi.spyOn(Storage.prototype, "getItem");
  const write = vi.spyOn(Storage.prototype, "setItem");
  fetchMock.mockResolvedValueOnce(response([entry, { documentUrl: B, addedAt: 20 }]));
  const collection = await loadSessionDocuments(session).result;
  expect(collection.entries.map((item) => item.documentUrl)).toEqual([B, A]);
  expect(methods()).toEqual(["GET"]);
  expect(fetchMock.mock.calls[0][0]).toContain("/session-documents");
  expect(fetchMock.mock.calls[0][1]?.cache).toBe("no-store");
  expect(read).not.toHaveBeenCalled();
  expect(write).not.toHaveBeenCalled();
  read.mockRestore();
  write.mockRestore();
  expect(
    orderedSessionDocuments([
      { documentUrl: A.replace("A", "a"), addedAt: 1 },
      { documentUrl: B, addedAt: 1 },
    ])[0].documentUrl,
  ).toBe(B);
});

it("explicit add returns the committed collection and sends the original URL without timestamps", async () => {
  const original = B + "&usp=sharing";
  const saved = [entry, { documentUrl: original, addedAt: 20 }];
  fetchMock.mockResolvedValueOnce(response(saved));
  expect((await addSessionDocument(session, original).result).entries).toEqual([
    saved[1],
    saved[0],
  ]);
  expect(methods()).toEqual(["POST"]);
  expect(JSON.parse(fetchMock.mock.calls[0][1]?.body as string)).toEqual({ url: original });
});

it("duplicate add accepts the stored reference to another tab without rewriting it", async () => {
  fetchMock.mockResolvedValueOnce(response([entry]));
  expect((await addSessionDocument(session, A + "?tab=t.other").result).entries).toEqual([entry]);
  expect(methods()).toEqual(["POST"]);
});

it("rejects unsupported and oversized targets before any HTTP operation", () => {
  for (const url of [
    A + "#heading=h.one",
    A + "?resourcekey=example",
    "https://example.test/file",
    A + "?usp=" + "x".repeat(2048),
  ])
    expect(() => addSessionDocument(session, url)).toThrow();
  expect(fetchMock).not.toHaveBeenCalled();
});

it.each(["network", "server", "timeout", "malformed"])(
  "reconciles an uncertain add (%s) by GET without another POST",
  async (failure) => {
    if (failure === "network") fetchMock.mockRejectedValueOnce(new Error("response lost"));
    else if (failure === "malformed")
      fetchMock.mockResolvedValueOnce(Response.json({ unexpected: true }));
    else
      fetchMock.mockResolvedValueOnce(
        Response.json({ error: "uncertain" }, { status: failure === "server" ? 500 : 408 }),
      );
    const request = addSessionDocument(session, B);
    await expect(request.result).rejects.toThrow();
    fetchMock.mockResolvedValueOnce(response([entry, { documentUrl: B, addedAt: 20 }]));
    expect((await request.retry().result).entries).toHaveLength(2);
    expect(methods()).toEqual(["POST", "GET"]);
  },
);

it("does not resurrect a document removed after an uncertain add", async () => {
  fetchMock.mockRejectedValueOnce(new Error("response lost"));
  const request = addSessionDocument(session, A);
  await expect(request.result).rejects.toThrow("lost");
  fetchMock.mockResolvedValueOnce(response());
  await expect(request.retry().result).rejects.toThrow("removed");
  expect(methods()).toEqual(["POST", "GET"]);
});

it.each([false, true])(
  "reconciles uncertain removal without deleting a subsequently added reference: present=%s",
  async (present) => {
    fetchMock.mockRejectedValueOnce(new Error("response lost"));
    const request = removeSessionDocument(session, entry);
    await expect(request.result).rejects.toThrow("lost");
    fetchMock.mockResolvedValueOnce(response(present ? [{ ...entry, addedAt: 20 }] : []));
    if (present) await expect(request.retry().result).rejects.toThrow("added again");
    else expect((await request.retry().result).entries).toEqual([]);
    expect(methods()).toEqual(["DELETE", "GET"]);
    expect(fetchMock.mock.calls[0][0]).toContain("/session-documents/A");
  },
);

it.each(["add", "remove"])(
  "allows an explicit retry after a definitive rejection of %s",
  async (operation) => {
    fetchMock.mockResolvedValueOnce(Response.json({ error: "rejected" }, { status: 409 }));
    const request =
      operation === "add" ? addSessionDocument(session, A) : removeSessionDocument(session, entry);
    await expect(request.result).rejects.toThrow();
    fetchMock.mockResolvedValueOnce(response(operation === "add" ? [entry] : []));
    await request.retry().result;
    expect(methods()).toEqual(operation === "add" ? ["POST", "POST"] : ["DELETE", "DELETE"]);
  },
);

it("serializes operations in a scope and ignores superseded or different-session responses", async () => {
  const pending = deferred<Response>();
  fetchMock.mockReturnValueOnce(pending.promise);
  const first = loadSessionDocuments(session);
  const remove = removeSessionDocument(session, entry);
  const otherId = session + "-other";
  fetchMock.mockResolvedValueOnce(response([], otherId));
  await loadSessionDocuments(otherId).result;
  expect(methods()).toEqual(["GET", "GET"]);
  fetchMock.mockResolvedValueOnce(response());
  pending.resolve(response([entry]));
  await first.result;
  await remove.result;
  expect(first.isCurrent(session)).toBe(false);
  expect(remove.isCurrent(otherId)).toBe(false);
  expect(remove.isCurrent(session)).toBe(true);
  expect(methods()).toEqual(["GET", "GET", "DELETE"]);
});

it.each(["user", "server", "fetcher", "base", "disconnect"])(
  "rejects late responses and queued writes after a %s change",
  async (change) => {
    const pending = deferred<Response>();
    fetchMock.mockReturnValueOnce(pending.promise);
    const load = loadSessionDocuments(session);
    const queued = addSessionDocument(session, A);
    const failedLoad = expect(load.result).rejects.toThrow("changed");
    const failedWrite = expect(queued.result).rejects.toThrow("changed");
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    if (change === "user") context.user = "bob";
    if (change === "server") context.server = "workspace-b";
    if (change === "fetcher") context.fetcher = fetch;
    if (change === "base") window.__OMNIGENT_BASE_PATH__ = "/other";
    if (change === "disconnect") context.server = null;
    pending.resolve(response([entry]));
    await failedLoad;
    await failedWrite;
    expect(load.isCurrent(session)).toBe(false);
    expect(methods()).toEqual(["GET"]);
    await expect(queued.retry().result).rejects.toThrow("changed");
    expect(methods()).toEqual(["GET"]);
  },
);

it("checks connection identity again after consuming an asynchronously delivered body", async () => {
  const body = deferred<unknown>();
  fetchMock.mockResolvedValueOnce({ ok: true, json: () => body.promise } as Response);
  const request = loadSessionDocuments(session);
  const failure = expect(request.result).rejects.toThrow("changed");
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
  context.user = "bob";
  body.resolve({ session_id: session, entries: [] });
  await failure;
});

it.each([
  {
    name: "zero timestamp",
    entries: [{ document_url: A, added_at: 0 }],
    error: /Invalid session document entry/,
  },
  {
    name: "fractional timestamp",
    entries: [{ document_url: A, added_at: 1.5 }],
    error: /Invalid session document entry/,
  },
  {
    name: "unsafe timestamp",
    entries: [{ document_url: A, added_at: 2 ** 53 }],
    error: /Invalid session document entry/,
  },
  {
    name: "string timestamp",
    entries: [{ document_url: A, added_at: "1" }],
    error: /Invalid session document entry/,
  },
  {
    name: "unsupported URL",
    entries: [{ document_url: "https://example.test/file", added_at: 1 }],
    error: /Expected a supported Google Docs editor URL/,
  },
  {
    name: "duplicate document identity across tabs",
    entries: [
      { document_url: A, added_at: 1 },
      { document_url: A + "?tab=t.other", added_at: 2 },
    ],
    error: /Duplicate session document identity/,
  },
  {
    name: "null entry",
    entries: [null],
    error: /Invalid session document entry/,
  },
])("rejects $name", async ({ entries, error }) => {
  fetchMock.mockResolvedValueOnce(Response.json({ session_id: session, entries }));
  await expect(loadSessionDocuments(session).result).rejects.toThrow(error);
});

it("rejects a collection returned for the wrong session and an unidentified embedded backend", async () => {
  fetchMock.mockResolvedValueOnce(response([entry], "wrong"));
  await expect(loadSessionDocuments(session).result).rejects.toThrow("Wrong session");
  context.server = null;
  expect(() => sessionDocumentsScope(session)).toThrow("backend identity");
});
