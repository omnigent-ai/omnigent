import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX, GOOGLE_DOCS_SELECTION_LABEL } from "./googleDocs";
import { loadGoogleDocsSession, saveGoogleDocsSession } from "./googleDocsSession";

const SESSION = "conv_docs";
const OTHER_SESSION = "conv_other";
const PATH = "/v1/sessions/" + SESSION;
const EDITOR = "https://docs.google.com/document/d/";
const DOC_A = EDITOR + "A/edit";
const DOC_B = EDITOR + "B/edit?tab=t.two";
const ENTRY_A = GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX + "A";
const ENTRY_B = GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX + "B";
const INITIAL_LABELS = {
  [GOOGLE_DOCS_SELECTION_LABEL]: DOC_A,
  [ENTRY_A]: DOC_A,
  "other.label": "keep",
};

const fetchMock = vi.fn<typeof fetch>();

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function sessionResponse(labels: Record<string, string>, id = SESSION): Response {
  return Response.json({ id, labels });
}

function requests(): [string, string][] {
  return fetchMock.mock.calls.map(([url, options]) => [String(url), options?.method ?? "GET"]);
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

it("orders writes and restores across callers while other sessions can load", async () => {
  const patchStarted = deferred<void>();
  const releasePatch = deferred<void>();
  let labels: Record<string, string> = { ...INITIAL_LABELS };
  const patches: unknown[] = [];
  fetchMock.mockImplementation(async (url, options) => {
    if (url === "/v1/sessions/" + OTHER_SESSION + "/labels") {
      return sessionResponse({}, OTHER_SESSION);
    }
    if (url === PATH + "/labels") return sessionResponse(labels);
    expect(url).toBe(PATH);
    expect(options?.method).toBe("PATCH");
    const body = JSON.parse(String(options?.body));
    patches.push(body);
    if (patches.length === 1) {
      patchStarted.resolve();
      await releasePatch.promise;
    }
    labels = { ...labels, ...body.labels };
    return sessionResponse(labels);
  });

  const change = saveGoogleDocsSession(SESSION, EDITOR + "B/edit#tab=t.two");
  await patchStarted.promise;
  const restore = loadGoogleDocsSession(SESSION);
  const unlink = saveGoogleDocsSession(SESSION, null);
  const other = loadGoogleDocsSession(OTHER_SESSION);
  expect(other.sessionId).toBe(OTHER_SESSION);
  expect((await other.result).selected).toBeNull();
  expect(requests()).toEqual([
    [PATH + "/labels", "GET"],
    [PATH, "PATCH"],
    ["/v1/sessions/" + OTHER_SESSION + "/labels", "GET"],
  ]);
  expect(change.sessionId).toBe(SESSION);
  expect(change.generation).toBeLessThan(restore.generation);
  expect(restore.generation).toBeLessThan(unlink.generation);

  releasePatch.resolve();
  expect((await change.result).selected).toEqual({
    documentId: "B",
    tabId: "t.two",
    canonicalUrl: DOC_B,
  });
  expect((await restore.result).selected?.canonicalUrl).toBe(DOC_B);
  expect(await unlink.result).toEqual({ selected: null, documents: new Map() });
  expect(requests().slice(3)).toEqual([
    [PATH + "/labels", "GET"],
    [PATH + "/labels", "GET"],
    [PATH, "PATCH"],
  ]);
  expect(patches).toEqual([
    { labels: { [GOOGLE_DOCS_SELECTION_LABEL]: DOC_B, [ENTRY_A]: "", [ENTRY_B]: DOC_B } },
    { labels: { [GOOGLE_DOCS_SELECTION_LABEL]: "", [ENTRY_B]: "" } },
  ]);
  expect(labels["other.label"]).toBe("keep");
});

it("reads legacy state from the labels endpoint without writing", async () => {
  const sessionId = "conv legacy";
  fetchMock.mockResolvedValueOnce(
    sessionResponse(
      { [GOOGLE_DOCS_SELECTION_LABEL]: EDITOR + "A/edit?usp=sharing#tab=t.0" },
      sessionId,
    ),
  );

  const state = await loadGoogleDocsSession(sessionId).result;
  expect(state.selected?.canonicalUrl).toBe(DOC_A + "?tab=t.0");
  expect(state.documents.get("A")).toEqual(state.selected);
  expect(requests()).toEqual([["/v1/sessions/conv%20legacy/labels", "GET"]]);
});

it("reads after a lost PATCH response before clearing what was actually committed", async () => {
  const afterLostPatch = {
    ...INITIAL_LABELS,
    [GOOGLE_DOCS_SELECTION_LABEL]: DOC_B,
    [ENTRY_A]: "",
    [ENTRY_B]: DOC_B,
  };
  fetchMock
    .mockResolvedValueOnce(sessionResponse(INITIAL_LABELS))
    .mockRejectedValueOnce(new Error("Connection lost"))
    .mockRejectedValueOnce(new Error("Labels temporarily unavailable"))
    .mockResolvedValueOnce(sessionResponse(afterLostPatch))
    .mockResolvedValueOnce(
      sessionResponse({ ...afterLostPatch, [GOOGLE_DOCS_SELECTION_LABEL]: "", [ENTRY_B]: "" }),
    );

  await expect(saveGoogleDocsSession(SESSION, DOC_B).result).rejects.toThrow("Connection lost");
  await expect(saveGoogleDocsSession(SESSION, null).result).rejects.toThrow(
    "Labels temporarily unavailable",
  );
  expect((await saveGoogleDocsSession(SESSION, null).result).selected).toBeNull();

  expect(requests()).toEqual([
    [PATH + "/labels", "GET"],
    [PATH, "PATCH"],
    [PATH + "/labels", "GET"],
    [PATH + "/labels", "GET"],
    [PATH, "PATCH"],
  ]);
  expect(JSON.parse(String(fetchMock.mock.calls[4]?.[1]?.body))).toEqual({
    labels: { [GOOGLE_DOCS_SELECTION_LABEL]: "", [ENTRY_B]: "" },
  });
});

it("rejects a PATCH response that does not confirm the change", async () => {
  fetchMock
    .mockResolvedValueOnce(sessionResponse(INITIAL_LABELS))
    .mockResolvedValueOnce(sessionResponse(INITIAL_LABELS))
    .mockResolvedValueOnce(sessionResponse(INITIAL_LABELS));

  await expect(saveGoogleDocsSession(SESSION, DOC_B).result).rejects.toThrow(
    "Could not confirm the Google Docs selection was saved",
  );
  expect((await loadGoogleDocsSession(SESSION).result).selected?.canonicalUrl).toBe(DOC_A);
});

it("stops a write if the labels response belongs to another session", async () => {
  fetchMock.mockResolvedValueOnce(sessionResponse(INITIAL_LABELS, OTHER_SESSION));

  await expect(saveGoogleDocsSession(SESSION, DOC_B).result).rejects.toThrow(
    "Could not read labels for this session",
  );
  expect(requests()).toEqual([[PATH + "/labels", "GET"]]);
});
