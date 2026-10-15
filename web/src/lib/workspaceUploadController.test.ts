import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "./identity";
import { uploadTotals, useUploadStore, type UploadRow } from "@/store/uploadStore";
import {
  adaptiveChunk,
  cancelUpload,
  discardUploads,
  pumpUploads,
  enqueueUploads,
  refreshUploadedFiles,
  retryDelay,
  retryUpload,
  transferUpload,
  waitForRetry,
} from "./workspaceUploadController";
import { UploadError } from "./workspaceUploadsApi";
import { toast } from "sonner";

vi.mock("./identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("sonner", () => ({ toast: Object.assign(vi.fn(), { error: vi.fn() }) }));
const fetchMock = vi.mocked(authenticatedFetch);
const client = new QueryClient();
let row: UploadRow;
let offset: number;
let requests: { method: string; offset?: number; size?: number }[];
const wait = vi.fn(async () => {});

function errorResponse(status: number, code: string, headers?: HeadersInit) {
  return new Response(JSON.stringify({ error: { code, message: code } }), { status, headers });
}
function json(data: unknown) {
  return new Response(JSON.stringify(data));
}
async function server(_input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const method = init?.method ?? "GET";
  const body = init?.body as Blob | undefined;
  const start = Number(new Headers(init?.headers).get("Upload-Offset"));
  requests.push({ method, ...(method === "PATCH" ? { offset: start, size: body?.size } : {}) });
  if (method === "DELETE") return new Response(null, { status: 204 });
  if (method === "HEAD")
    return new Response(null, {
      headers: { "Upload-Offset": String(offset), "Upload-Length": String(row.size) },
    });
  if (method === "PATCH") {
    expect(new Headers(init?.headers).get("Content-Type")).toBe("application/offset+octet-stream");
    expect(start).toBe(offset);
    offset += body?.size ?? 0;
    return new Response(null, { status: 204, headers: { "Upload-Offset": String(offset) } });
  }
  if (String(_input).endsWith("/complete"))
    return json({
      path: "src/a.bin",
      final_name: "a.bin",
      size: row.size,
      sha256: "digest",
      outcome: "created",
    });
  expect(JSON.parse(String(init?.body)).on_conflict).toBe(
    useUploadStore.getState().rows[row.key]?.conflict ?? "fail",
  );
  offset = 0;
  return json({ upload_id: "remote", offset: 0, chunk_bytes: 4 });
}

beforeEach(() => {
  vi.mocked(toast).mockClear();
  row = {
    origin: "http://server",
    identity: "alice",
    session: "s",
    environment: "default",
    id: "id",
    key: "id",
    createKey: "a".repeat(32),
    batch: crypto.randomUUID(),
    file: new File([new Uint8Array(20)], "a.bin"),
    name: "a.bin",
    size: 20,
    directory: "src",
    destination: "src",
    offset: 0,
    controller: new AbortController(),
    status: "queued",
    conflict: "fail",
    limits: {
      chunk: { min: 2, initial: 4, max: 8 },
      max_file_bytes: 100,
      max_files_per_batch: 10,
      max_batch_bytes: 200,
      max_open_uploads: 16,
      max_open_bytes: 200,
      parallel_files: 2,
    },
  };
  offset = 0;
  requests = [];
  useUploadStore.setState({ rows: { [row.key]: row }, announcement: "" });
  fetchMock.mockImplementation(server);
  wait.mockClear();
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  fetchMock.mockReset();
  useUploadStore.setState({ rows: {} });
  client.clear();
});

it("creates, sends only slices sequentially, reports acknowledged bytes, then completes", async () => {
  const slice = vi.spyOn(row.file!, "slice");
  await transferUpload(row.key, client, wait);
  expect(requests.map((request) => request.method)).toEqual([
    "POST",
    "PATCH",
    "PATCH",
    "PATCH",
    "POST",
  ]);
  expect(slice.mock.calls).toEqual([
    [0, 8],
    [8, 16],
    [16, 20],
  ]);
  expect(useUploadStore.getState().rows.id).toMatchObject({
    status: "done",
    offset: 20,
    file: undefined,
  });
});

it("resumes a lost PATCH response with HEAD and never resends acknowledged bytes", async () => {
  let lost = false;
  fetchMock.mockImplementation(async (url, init) => {
    const response = await server(url, init);
    if (init?.method === "PATCH" && !lost) {
      lost = true;
      throw new TypeError("Lost response");
    }
    return response;
  });
  await transferUpload(row.key, client, wait);
  expect(requests.map((request) => request.method)).toEqual([
    "POST",
    "PATCH",
    "HEAD",
    "PATCH",
    "PATCH",
    "POST",
  ]);
  expect(
    requests.filter((request) => request.method === "PATCH").map((request) => request.offset),
  ).toEqual([0, 8, 16]);
});

it("resynchronizes directly from Upload-Offset on 409 offset_mismatch", async () => {
  let mismatch = false;
  fetchMock.mockImplementation(async (url, init) => {
    if (init?.method === "PATCH" && !mismatch) {
      mismatch = true;
      offset = 5;
      return errorResponse(409, "offset_mismatch", { "Upload-Offset": "5" });
    }
    return server(url, init);
  });
  await transferUpload(row.key, client, wait);
  expect(requests.find((request) => request.method === "PATCH")?.offset).toBe(5);
  expect(useUploadStore.getState().rows.id.status).toBe("done");
});

it.each(["busy", "upload_limit"])("honours Retry-After for 429 %s without HEAD", async (code) => {
  let busy = false;
  fetchMock.mockImplementation(async (url, init) => {
    if (init?.method === "PATCH" && !busy) {
      busy = true;
      return errorResponse(429, code, { "Retry-After": code === "busy" ? "2" : "60" });
    }
    return server(url, init);
  });
  await transferUpload(row.key, client, wait);
  expect(wait).toHaveBeenCalledWith(code === "busy" ? 2000 : 60_000, row.controller.signal);
  expect(requests.some((request) => request.method === "HEAD")).toBe(false);
  expect(useUploadStore.getState().rows.id.status).toBe("done");
});

it.each([404, 410])(
  "restarts an expired %s file once, then fails if it expires again",
  async (status) => {
    let creates = 0;
    fetchMock.mockImplementation(async (url, init) => {
      if (init?.method === "PATCH") return errorResponse(status, "gone");
      if (init?.method === "POST" && !String(url).endsWith("complete")) creates++;
      return server(url, init);
    });
    await transferUpload(row.key, client, wait);
    expect(creates).toBe(2);
    expect(useUploadStore.getState().rows.id).toMatchObject({ status: "failed", offset: 0 });
  },
);

it.each([501, 503])("stops non-retryable %s capability errors", async (status) => {
  fetchMock.mockResolvedValue(errorResponse(status, "runner_capability_mismatch"));
  await transferUpload(row.key, client, wait);
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(wait).not.toHaveBeenCalled();
  expect(useUploadStore.getState().rows.id.status).toBe("failed");
});

it("retries a timed-out idempotent complete after HEAD and gives up after three transient attempts", async () => {
  let completes = 0;
  fetchMock.mockImplementation(async (url, init) => {
    if (String(url).endsWith("complete") && ++completes < 3)
      return errorResponse(504, "upload_timeout");
    return server(url, init);
  });
  await transferUpload(row.key, client, wait);
  expect(completes).toBe(3);
  expect(requests.filter((request) => request.method === "HEAD")).toHaveLength(2);
  expect(useUploadStore.getState().rows.id.status).toBe("done");
  useUploadStore
    .getState()
    .update(row.key, { status: "queued", file: row.file, uploadId: undefined });
  fetchMock.mockClear().mockResolvedValue(errorResponse(502, "upload_relay_failed"));
  await transferUpload(row.key, client, wait);
  expect(fetchMock).toHaveBeenCalledTimes(3);
  expect(useUploadStore.getState().rows.id.status).toBe("failed");
});

it("aborts the in-flight request before DELETE and releases the File handle", async () => {
  useUploadStore.getState().update(row.key, { uploadId: "remote", status: "uploading" });
  fetchMock.mockImplementation(async (url, init) => {
    expect(row.controller.signal.aborted).toBe(true);
    return server(url, init);
  });
  await cancelUpload(row.key);
  expect(requests).toEqual([{ method: "DELETE" }]);
  expect(useUploadStore.getState().rows.id).toMatchObject({ status: "cancelled", file: undefined });
});

it("clamps adaptive chunks at capability bounds, doubles fast chunks and halves slow ones", () => {
  expect(adaptiveChunk(3, 2000, row.limits)).toBe(6);
  expect(adaptiveChunk(8, 2000, row.limits)).toBe(8);
  expect(adaptiveChunk(6, 9000, row.limits)).toBe(3);
  expect(adaptiveChunk(2, 9000, row.limits)).toBe(2);
  expect(adaptiveChunk(4, 5000, row.limits)).toBe(4);
});

it("limits parallel files and prechecks batch and file limits", async () => {
  useUploadStore.setState({ rows: {} });
  const release: (() => void)[] = [];
  let pending = 0;
  let maximum = 0;
  fetchMock.mockImplementation(async (url, init) => {
    if (init?.method === "POST" && !String(url).endsWith("complete")) {
      pending++;
      maximum = Math.max(maximum, pending);
      await new Promise<void>((resolve) => {
        release.push(resolve);
      });
      pending--;
      return errorResponse(503, "runner_capability_mismatch");
    }
    return server(url, init);
  });
  enqueueUploads(row, [row.file!, row.file!, row.file!], "frozen", "frozen", row.limits, client);
  const createKeys = Object.values(useUploadStore.getState().rows).map((item) => item.createKey);
  expect(createKeys.every((key) => /^[0-9a-f]{32}$/.test(key ?? ""))).toBe(true);
  expect(new Set(createKeys).size).toBe(3);
  expect(pending).toBe(2);
  release.shift()!();
  await vi.waitFor(() => expect(release.length).toBe(2));
  release.splice(0).forEach((resolve) => resolve());
  await vi.waitFor(() =>
    expect(
      Object.values(useUploadStore.getState().rows).every((item) => item.status === "failed"),
    ).toBe(true),
  );
  expect(maximum).toBe(2);
  const calls = fetchMock.mock.calls.length;
  enqueueUploads(row, [new File([new Uint8Array(101)], "large")], "", "root", row.limits, client);
  enqueueUploads(
    row,
    Array.from({ length: 11 }, () => row.file!),
    "",
    "root",
    row.limits,
    client,
  );
  enqueueUploads(
    row,
    [row.file!, row.file!],
    "",
    "root",
    { ...row.limits, max_batch_bytes: 30 },
    client,
  );
  expect(fetchMock).toHaveBeenCalledTimes(calls);
});

it("collects file conflicts, resolves through a new create, and treats directories as hard errors", async () => {
  fetchMock.mockResolvedValueOnce(errorResponse(409, "file_exists"));
  await transferUpload(row.key, client, wait);
  expect(useUploadStore.getState().rows.id.status).toBe("conflict");
  await retryUpload(row.key, client, "rename");
  await vi.waitFor(() => expect(useUploadStore.getState().rows.id.status).toBe("done"));
  expect(
    fetchMock.mock.calls.some(([, init]) => String(init?.body).includes('"on_conflict":"rename"')),
  ).toBe(true);
  useUploadStore
    .getState()
    .update(row.key, { file: row.file, uploadId: undefined, status: "queued" });
  fetchMock.mockResolvedValueOnce(errorResponse(409, "is_directory"));
  await transferUpload(row.key, client, wait);
  expect(useUploadStore.getState().rows.id.status).toBe("failed");
});

it("debounces refresh by directory, actively refetches the target and replaced files, marks listings stale", async () => {
  vi.useFakeTimers();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const result = {
    path: "src/a.bin",
    final_name: "a.bin",
    size: 20,
    sha256: "hash",
    outcome: "replaced" as const,
  };
  refreshUploadedFiles(client, row, result);
  await vi.advanceTimersByTimeAsync(200);
  refreshUploadedFiles(client, row, result);
  refreshUploadedFiles(client, { ...row, directory: "other" }, { ...result, outcome: "created" });
  await vi.advanceTimersByTimeAsync(299);
  expect(invalidate).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(1);
  for (const queryKey of [
    ["workspace-dir", "s", "src"],
    ["workspace-dir", "s", "other"],
    ["workspace-all-files", "s"],
    ["workspace-changed-files", "s"],
    ["workspace-file-search", "s"],
    ["file-content", "s", "src/a.bin"],
    ["file-diff", "s", "src/a.bin"],
  ])
    expect(invalidate).toHaveBeenCalledWith({ queryKey, refetchType: "active" });
  expect(invalidate).toHaveBeenCalledWith({
    queryKey: ["workspace-dir-listing", "s"],
    refetchType: "none",
  });
  expect(
    invalidate.mock.calls.filter(([arg]) => arg?.queryKey?.[0] === "workspace-dir"),
  ).toHaveLength(4);
});

it("coalesces session-wide refetches across folders and refreshes each folder listing once", async () => {
  vi.useFakeTimers();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const result = {
    path: "a.bin",
    final_name: "a.bin",
    size: 20,
    sha256: "hash",
    outcome: "created" as const,
  };
  for (const directory of ["one", "two", "three"])
    refreshUploadedFiles(client, { ...row, directory }, result);
  await vi.advanceTimersByTimeAsync(300);
  const count = (queryKey: unknown[]) =>
    invalidate.mock.calls.filter(
      ([arg]) => JSON.stringify(arg?.queryKey) === JSON.stringify(queryKey),
    ).length;
  for (const queryKey of [
    ["workspace-all-files", "s"],
    ["workspace-changed-files", "s"],
    ["workspace-file-search", "s"],
    ["workspace-dir-listing", "s"],
    ["workspace-dir", "s", "one"],
    ["workspace-dir", "s", "two"],
    ["workspace-dir", "s", "three"],
  ])
    expect(count(queryKey)).toBe(1);
});

it("flushes session-wide refetches during a sustained completion stream", async () => {
  discardUploads(row.origin, row.identity);
  vi.useFakeTimers();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const result = {
    path: "a.bin",
    final_name: "a.bin",
    size: 20,
    sha256: "hash",
    outcome: "created" as const,
  };
  const sessionKeys = [
    ["workspace-all-files", "s"],
    ["workspace-changed-files", "s"],
    ["workspace-file-search", "s"],
    ["workspace-dir-listing", "s"],
  ];
  const counts = () =>
    sessionKeys.map(
      (queryKey) =>
        invalidate.mock.calls.filter(
          ([arg]) => JSON.stringify(arg?.queryKey) === JSON.stringify(queryKey),
        ).length,
    );
  // Completions 200 ms apart for 4 s never let the 300 ms trailing debounce settle.
  for (let i = 0; i < 20; i++)
    setTimeout(
      () => refreshUploadedFiles(client, { ...row, directory: `d${i % 3}` }, result),
      i * 200,
    );
  await vi.advanceTimersByTimeAsync(4000);
  const during = counts();
  for (const count of during) {
    expect(count).toBeGreaterThanOrEqual(1);
    expect(count).toBeLessThanOrEqual(3);
  }
  await vi.advanceTimersByTimeAsync(100);
  expect(counts()).toEqual(during.map((count) => count + 1));
  await vi.advanceTimersByTimeAsync(5000);
  expect(counts()).toEqual(during.map((count) => count + 1));

  refreshUploadedFiles(client, row, result);
  discardUploads(row.origin, row.identity);
  await vi.advanceTimersByTimeAsync(5000);
  expect(counts()).toEqual(during.map((count) => count + 1));
});

it("refreshes an active folder and its replaced files during a sustained same-folder stream", async () => {
  discardUploads(row.origin, row.identity);
  vi.useFakeTimers();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  let fetches = 0;
  const observer = new QueryObserver(client, {
    queryKey: ["workspace-dir", "s", "src"],
    queryFn: async () => ++fetches,
  });
  const unsubscribe = observer.subscribe(() => {});
  await vi.advanceTimersByTimeAsync(0);
  expect(fetches).toBe(1);
  const path = (i: number) => `src/f${i}.bin`;
  const refreshed = (i: number) =>
    ["file-content", "file-diff"].map(
      (kind) =>
        invalidate.mock.calls.filter(
          ([arg]) => JSON.stringify(arg?.queryKey) === JSON.stringify([kind, "s", path(i)]),
        ).length,
    );
  // Replaced files land in one folder 200 ms apart for 4 s, so the 300 ms debounce never settles.
  for (let i = 0; i < 20; i++)
    setTimeout(
      () =>
        refreshUploadedFiles(client, row, {
          path: path(i),
          final_name: `f${i}.bin`,
          size: 20,
          sha256: "hash",
          outcome: "replaced",
        }),
      i * 200,
    );
  await vi.advanceTimersByTimeAsync(4000);
  const during = fetches - 1;
  expect(during).toBeGreaterThanOrEqual(1);
  expect(during).toBeLessThanOrEqual(3);
  for (let i = 0; i < 16; i++) expect(refreshed(i)).toEqual([1, 1]);
  for (let i = 16; i < 20; i++) expect(refreshed(i)).toEqual([0, 0]);
  await vi.advanceTimersByTimeAsync(100);
  expect(fetches).toBe(during + 2);
  for (let i = 0; i < 20; i++) expect(refreshed(i)).toEqual([1, 1]);
  await vi.advanceTimersByTimeAsync(5000);
  expect(fetches).toBe(during + 2);
  unsubscribe();
});

it("cancels retry waits and distinguishes long upload-limit backoff from busy", async () => {
  vi.useFakeTimers();
  const controller = new AbortController();
  const waiting = waitForRetry(60_000, controller.signal);
  const assertion = expect(waiting).rejects.toMatchObject({ name: "AbortError" });
  controller.abort();
  await assertion;
  vi.spyOn(Math, "random").mockReturnValue(0.5);
  expect(retryDelay(new UploadError("busy", 429, "busy", new Headers()), 0)).toBe(500);
  expect(retryDelay(new UploadError("limit", 429, "upload_limit", new Headers()), 0)).toBe(60_000);
});

it.each(["cancel", "discard"])(
  "cleans up an admitted create after %s while POST is pending",
  async (action) => {
    let release!: (response: Response) => void;
    fetchMock.mockImplementation((url, init) => {
      if (init?.method === "POST")
        return new Promise<Response>((resolve, reject) => {
          release = resolve;
          init.signal?.addEventListener("abort", () =>
            reject(new DOMException("Cancelled", "AbortError")),
          );
        });
      return server(url, init);
    });
    const transfer = transferUpload(row.key, client, wait);
    const cancellation = action === "cancel" ? cancelUpload(row.key) : undefined;
    if (action === "discard") discardUploads(row.origin, row.identity);
    release(json({ upload_id: "admitted", offset: 0, chunk_bytes: 4 }));
    await transfer;
    await cancellation;
    await vi.waitFor(() =>
      expect(fetchMock.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(true),
    );
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) => init?.method === "DELETE" && String(url).endsWith("/admitted"),
      ),
    ).toBe(true);
    expect(requests.some((request) => request.method === "PATCH")).toBe(false);
    if (action === "cancel") expect(useUploadStore.getState().rows.id.status).toBe("cancelled");
    else expect(useUploadStore.getState().rows.id).toBeUndefined();
  },
);

it("reconciles a committed DELETE receipt after cancellation wins the response race", async () => {
  vi.useFakeTimers();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const result = {
    path: "src/a.bin",
    final_name: "a.bin",
    size: 20,
    sha256: "digest",
    outcome: "replaced",
  };
  let finalizing!: () => void;
  const reachedComplete = new Promise<void>((resolve) => {
    finalizing = resolve;
  });
  fetchMock.mockImplementation((url, init) => {
    if (String(url).endsWith("/complete"))
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () =>
          reject(new DOMException("Cancelled", "AbortError")),
        );
        finalizing();
      });
    if (init?.method === "DELETE") return Promise.resolve(json(result));
    return server(url, init);
  });
  const transfer = transferUpload(row.key, client, wait);
  await reachedComplete;
  await cancelUpload(row.key);
  await transfer;
  expect(useUploadStore.getState().rows.id).toMatchObject({
    status: "done",
    offset: 20,
    file: undefined,
    result,
  });
  expect(toast).toHaveBeenCalledWith("1 of 1 uploaded");
  expect(toast).not.toHaveBeenCalledWith(expect.stringContaining("cancelled"));
  await vi.advanceTimersByTimeAsync(300);
  expect(invalidate).toHaveBeenCalledWith({
    queryKey: ["workspace-dir", "s", "src"],
    refetchType: "active",
  });
});

it("does not resurrect a conflict cancelled while retry DELETE is pending", async () => {
  useUploadStore.getState().update(row.key, { status: "conflict", uploadId: "remote" });
  let release!: (response: Response) => void;
  const deleted = new Promise<Response>((resolve) => {
    release = resolve;
  });
  fetchMock.mockReturnValue(deleted);
  let resurrected = false;
  const unsubscribe = useUploadStore.subscribe((state) => {
    const live = state.rows[row.key];
    if (live?.status === "queued" && !live.file) {
      resurrected = true;
      state.update(row.key, { status: "failed", error: "File missing" });
    }
  });
  try {
    const retry = retryUpload(row.key, client, "rename");
    const cancel = cancelUpload(row.key);
    release(new Response(null, { status: 204 }));
    await Promise.all([retry, cancel]);
    expect(resurrected).toBe(false);
    expect(useUploadStore.getState().rows.id).toMatchObject({
      status: "cancelled",
      file: undefined,
    });
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "DELETE")).toHaveLength(1);
  } finally {
    unsubscribe();
  }
});

it("serializes conflict retries while DELETE is pending", async () => {
  useUploadStore.getState().update(row.key, { status: "conflict", uploadId: "remote" });
  let release!: (response: Response) => void;
  const deleted = new Promise<Response>((resolve) => {
    release = resolve;
  });
  fetchMock.mockReturnValue(deleted);
  const first = retryUpload(row.key, client, "rename");
  const second = retryUpload(row.key, client, "replace");
  const deletes = fetchMock.mock.calls.length;
  const cancel = cancelUpload(row.key);
  release(new Response(null, { status: 204 }));
  await Promise.allSettled([first, second, cancel]);
  discardUploads(row.origin, row.identity);
  expect(deletes).toBe(1);
});

it("fails a queued row without a File instead of repumping it", () => {
  useUploadStore.getState().update(row.key, { file: undefined });
  try {
    pumpUploads(row, client);
    expect(useUploadStore.getState().rows.id).toMatchObject({
      status: "failed",
      error: expect.stringMatching(/file.*(unavailable|missing|select)/i),
    });
    expect(fetchMock).not.toHaveBeenCalled();
  } finally {
    discardUploads(row.origin, row.identity);
  }
});

it.each(["busy", "upload_limit"])(
  "completes after four consecutive %s replies without spending the transient budget",
  async (code) => {
    let replies = 0;
    fetchMock.mockImplementation((url, init) => {
      if (init?.method === "PATCH" && replies++ < 4)
        return Promise.resolve(
          errorResponse(429, code, { "Retry-After": code === "busy" ? "2" : "60" }),
        );
      return server(url, init);
    });
    await transferUpload(row.key, client, wait);
    expect(useUploadStore.getState().rows.id.status).toBe("done");
    expect(wait).toHaveBeenCalledTimes(4);
    expect(wait).toHaveBeenCalledWith(code === "busy" ? 2000 : 60_000, row.controller.signal);
    expect(requests.some((request) => request.method === "HEAD")).toBe(false);
  },
);

it.each([
  [1, "Uploading 1 file to src"],
  [2, "Uploading 2 files to src"],
])("announces a grammatical start for %s files", (count, message) => {
  fetchMock.mockResolvedValue(errorResponse(503, "runner_capability_mismatch"));
  useUploadStore.setState({ rows: {} });
  enqueueUploads(
    row,
    Array.from({ length: Number(count) }, () => row.file!),
    "src",
    "src",
    row.limits,
    client,
  );
  expect(useUploadStore.getState().announcement).toBe(message);
});

it.each(["dismiss", "discard", "discard after receipt"])(
  "settles a delayed committed cancellation receipt across %s",
  async (action) => {
    vi.useFakeTimers();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const result = {
      path: "src/a.bin",
      final_name: "a.bin",
      size: 20,
      sha256: "digest",
      outcome: "replaced",
    };
    let reached!: () => void;
    const finalizing = new Promise<void>((resolve) => {
      reached = resolve;
    });
    let release!: (response: Response) => void;
    const deletion = new Promise<Response>((resolve) => {
      release = resolve;
    });
    fetchMock.mockImplementation((url, init) => {
      if (String(url).endsWith("/complete"))
        return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("Cancelled", "AbortError")),
          );
          reached();
        });
      if (init?.method === "DELETE") return deletion;
      return server(url, init);
    });
    const transfer = transferUpload(row.key, client, wait);
    await finalizing;
    const cancellation = cancelUpload(row.key);
    if (action === "dismiss") useUploadStore.getState().dismiss([row.key]);
    if (action === "discard") discardUploads(row.origin, row.identity);
    release(json(result));
    await Promise.all([transfer, cancellation]);
    if (action === "discard after receipt") {
      useUploadStore.getState().dismiss([row.key]);
      discardUploads(row.origin, row.identity);
    }
    await vi.advanceTimersByTimeAsync(300);
    if (action === "dismiss") {
      for (const queryKey of [
        ["workspace-dir", "s", "src"],
        ["file-content", "s", "src/a.bin"],
        ["file-diff", "s", "src/a.bin"],
      ])
        expect(invalidate).toHaveBeenCalledWith({ queryKey, refetchType: "active" });
      expect(useUploadStore.getState().rows.id).toMatchObject({
        status: "done",
        result,
        file: undefined,
      });
      expect(toast).toHaveBeenCalledWith("1 of 1 uploaded");
      useUploadStore.getState().dismiss([row.key]);
    } else {
      expect(invalidate).not.toHaveBeenCalled();
      if (action === "discard") expect(toast).not.toHaveBeenCalledWith("1 of 1 uploaded");
    }
    expect(useUploadStore.getState().rows.id).toBeUndefined();
  },
);

it("allows three offset resyncs separated by acknowledged PATCH progress", async () => {
  let resyncs = 0;
  const rejectedOffsets = new Set<number>();
  fetchMock.mockImplementation((url, init) => {
    if (init?.method === "PATCH" && !rejectedOffsets.has(offset)) {
      rejectedOffsets.add(offset);
      resyncs++;
      return Promise.resolve(
        errorResponse(409, "offset_mismatch", { "Upload-Offset": String(offset) }),
      );
    }
    return server(url, init);
  });
  await transferUpload(row.key, client, wait);
  expect(resyncs).toBe(3);
  expect(useUploadStore.getState().rows.id).toMatchObject({ status: "done", offset: 20 });
});

it.each([
  ["request", 20, 150_001],
  ["request", 1024 ** 3, 450_000],
  ["receipt body", 20, 150_001],
])("bounds a stalled cancellation %s for %s bytes", async (stall, size, deadline) => {
  vi.useFakeTimers();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  useUploadStore
    .getState()
    .update(row.key, { uploadId: "remote", status: "conflict", size: Number(size) });
  let release!: (response: Response) => void;
  const pending = new Promise<Response>((resolve) => {
    release = resolve;
  });
  let cleanupSignal: AbortSignal | undefined;
  fetchMock.mockImplementation(async (_url, init) => {
    cleanupSignal = init?.signal ?? undefined;
    if (stall === "receipt body") {
      const response = json({});
      vi.spyOn(response, "json").mockImplementation(async () => (await pending).json());
      return response;
    }
    return pending;
  });
  let settled = false;
  const cancellation = cancelUpload(row.key).then(() => {
    settled = true;
  });
  try {
    await vi.advanceTimersByTimeAsync(Number(deadline) - 1);
    expect(settled).toBe(false);
    useUploadStore.getState().dismiss([row.key]);
    expect(useUploadStore.getState().rows.id.cancellationPending).toBe(true);
    await vi.advanceTimersByTimeAsync(1);
    expect(settled).toBe(true);
    expect(cleanupSignal?.aborted).toBe(true);
    const live = useUploadStore.getState().rows.id;
    expect(live).toMatchObject({
      status: "cancelled",
      cancellationPending: false,
      file: undefined,
    });
    expect(live.error).toMatch(/server did not confirm cancellation.*staging expires/i);
    expect(uploadTotals([live]).active).toBe(false);
    expect(toast).toHaveBeenCalledWith("0 of 1 uploaded, 1 cancelled");
    useUploadStore.getState().dismiss([row.key]);
    expect(useUploadStore.getState().rows.id).toBeUndefined();
  } finally {
    release(
      json({
        path: "src/a.bin",
        final_name: "a.bin",
        size: Number(size),
        sha256: "digest",
        outcome: "replaced",
      }),
    );
    await cancellation;
    await vi.advanceTimersByTimeAsync(300);
  }
  expect(invalidate).not.toHaveBeenCalled();
});

it("starts the queued file immediately on cancel and reconciles a receipt within the deadline", async () => {
  vi.useFakeTimers();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  useUploadStore.getState().update(row.key, { limits: { ...row.limits, parallel_files: 1 } });
  const next = {
    ...row,
    id: "next",
    key: "next",
    controller: new AbortController(),
    file: new File([new Uint8Array(20)], "next.bin"),
  };
  useUploadStore.getState().add([next]);
  let reached!: () => void;
  const patchReached = new Promise<void>((resolve) => {
    reached = resolve;
  });
  let release!: (response: Response) => void;
  const deletion = new Promise<Response>((resolve) => {
    release = resolve;
  });
  let first = true;
  fetchMock.mockImplementation((url, init) => {
    if (init?.method === "PATCH" && first) {
      first = false;
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () =>
          reject(new DOMException("Cancelled", "AbortError")),
        );
        reached();
      });
    }
    if (init?.method === "DELETE") return deletion;
    return server(url, init);
  });
  let transferSettled = false;
  const transfer = transferUpload(row.key, client, wait).then(() => {
    transferSettled = true;
  });
  await patchReached;
  const cancellation = cancelUpload(row.key);
  const result = {
    path: "src/a.bin",
    final_name: "a.bin",
    size: 20,
    sha256: "digest",
    outcome: "replaced",
  };
  try {
    await vi.advanceTimersByTimeAsync(0);
    expect(transferSettled).toBe(true);
    expect(useUploadStore.getState().rows.next.status).toBe("done");
    expect(useUploadStore.getState().rows.id).toMatchObject({
      status: "cancelled",
      file: undefined,
      cancellationPending: true,
    });
    useUploadStore.getState().dismiss([row.key]);
    expect(useUploadStore.getState().rows.id).toBeDefined();
    expect(toast).not.toHaveBeenCalledWith(expect.stringContaining("of 2 uploaded"));
    await vi.advanceTimersByTimeAsync(149_000);
  } finally {
    release(json(result));
    await Promise.all([cancellation, transfer]);
    await vi.advanceTimersByTimeAsync(300);
  }
  expect(useUploadStore.getState().rows.id).toMatchObject({
    status: "done",
    result,
    cancellationPending: false,
  });
  expect(invalidate).toHaveBeenCalledWith({
    queryKey: ["workspace-dir", "s", "src"],
    refetchType: "active",
  });
  expect(invalidate).toHaveBeenCalledWith({
    queryKey: ["file-content", "s", "src/a.bin"],
    refetchType: "active",
  });
  expect(toast).toHaveBeenCalledWith("2 of 2 uploaded");
});

it.each(["offline", "relay timeout"])(
  "settles an unconfirmed %s cancellation without retaining its deadline",
  async (fault) => {
    vi.useFakeTimers();
    useUploadStore.getState().update(row.key, { uploadId: "remote", status: "conflict" });
    if (fault === "offline") fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    else fetchMock.mockResolvedValue(errorResponse(504, "upload_timeout"));
    await cancelUpload(row.key);
    expect(useUploadStore.getState().rows.id).toMatchObject({
      status: "cancelled",
      file: undefined,
      cancellationPending: false,
    });
    expect(useUploadStore.getState().rows.id.error).toMatch(
      /server did not confirm cancellation.*staging expires/i,
    );
    expect(toast).toHaveBeenCalledWith("0 of 1 uploaded, 1 cancelled");
    expect(vi.getTimerCount()).toBe(0);
    useUploadStore.getState().dismiss([row.key]);
    expect(useUploadStore.getState().rows.id).toBeUndefined();
  },
);

it.each(["headers", "body"])(
  "bounds cancellation of stalled create %s and deletes a late id once",
  async (stall) => {
    vi.useFakeTimers();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const slice = vi.spyOn(row.file!, "slice");
    let release!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => {
      release = resolve;
    });
    let createSignal: AbortSignal | null | undefined;
    fetchMock.mockImplementation((_url, init) => {
      if (init?.method === "DELETE") return Promise.resolve(new Response(null, { status: 204 }));
      createSignal = init?.signal;
      if (stall === "body") {
        const response = json({});
        vi.spyOn(response, "json").mockImplementation(async () => (await pending).json());
        return Promise.resolve(response);
      }
      return pending;
    });
    let transferSettled = false;
    const transfer = transferUpload(row.key, client, wait).then(() => {
      transferSettled = true;
    });
    await vi.advanceTimersByTimeAsync(0);
    const cancellation = cancelUpload(row.key);
    await vi.advanceTimersByTimeAsync(0);
    expect(useUploadStore.getState().rows.id.file).toBeUndefined();
    const releasedTransfer = transferSettled;
    await vi.advanceTimersByTimeAsync(150_000);
    expect(useUploadStore.getState().rows.id.cancellationPending).toBe(true);
    useUploadStore.getState().dismiss([row.key]);
    expect(useUploadStore.getState().rows.id).toBeDefined();
    await vi.advanceTimersByTimeAsync(1);
    const settled = useUploadStore.getState().rows.id;
    useUploadStore.getState().dismiss([row.key]);
    const dismissed = !useUploadStore.getState().rows.id;
    release(json({ upload_id: "late", offset: 0, chunk_bytes: 4 }));
    await transfer;
    await vi.advanceTimersByTimeAsync(300);
    await cancellation;
    expect(createSignal).toBeUndefined();
    expect(releasedTransfer).toBe(true);
    expect(settled).toMatchObject({
      status: "cancelled",
      cancellationPending: false,
      file: undefined,
    });
    expect(settled.error).toMatch(/server did not confirm cancellation.*staging expires/i);
    expect(dismissed).toBe(true);
    expect(toast).toHaveBeenCalledWith("0 of 1 uploaded, 1 cancelled");
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "DELETE")).toHaveLength(1);
    expect(slice).not.toHaveBeenCalled();
    expect(invalidate).not.toHaveBeenCalled();
  },
);

it("releases a discarded stalled create before late metadata cleanup without touching a new identity", async () => {
  vi.useFakeTimers();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const slice = vi.spyOn(row.file!, "slice");
  let release!: (response: Response) => void;
  const pending = new Promise<Response>((resolve) => {
    release = resolve;
  });
  fetchMock.mockImplementation((_url, init) =>
    init?.method === "DELETE"
      ? Promise.resolve(
          json({
            path: "src/a.bin",
            final_name: "a.bin",
            size: 20,
            sha256: "digest",
            outcome: "replaced",
          }),
        )
      : pending,
  );
  let transferSettled = false;
  const transfer = transferUpload(row.key, client, wait).then(() => {
    transferSettled = true;
  });
  discardUploads(row.origin, row.identity);
  const replacement = {
    ...row,
    identity: "bob",
    batch: "new-identity-batch",
    controller: new AbortController(),
    file: undefined,
    status: "done" as const,
  };
  useUploadStore.getState().add([replacement]);
  await vi.advanceTimersByTimeAsync(0);
  const releasedTransfer = transferSettled;
  release(json({ upload_id: "late", offset: 0, chunk_bytes: 4 }));
  await transfer;
  await vi.advanceTimersByTimeAsync(300);
  expect(releasedTransfer).toBe(true);
  expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "DELETE")).toHaveLength(1);
  expect(slice).not.toHaveBeenCalled();
  expect(useUploadStore.getState().rows.id).toEqual(replacement);
  expect(invalidate).not.toHaveBeenCalled();
  expect(toast).not.toHaveBeenCalled();
});

it("starts a queued file while a cancelled create stays pending", async () => {
  vi.useFakeTimers();
  useUploadStore.getState().update(row.key, { limits: { ...row.limits, parallel_files: 1 } });
  const next = {
    ...row,
    id: "next",
    key: "next",
    controller: new AbortController(),
    file: new File([new Uint8Array(20)], "next.bin"),
  };
  useUploadStore.getState().add([next]);
  let release!: (response: Response) => void;
  const pending = new Promise<Response>((resolve) => {
    release = resolve;
  });
  let first = true;
  fetchMock.mockImplementation((url, init) => {
    if (init?.method === "POST" && first) {
      first = false;
      return pending;
    }
    return server(url, init);
  });
  let transferSettled = false;
  const transfer = transferUpload(row.key, client, wait).then(() => {
    transferSettled = true;
  });
  const cancellation = cancelUpload(row.key);
  await vi.advanceTimersByTimeAsync(0);
  const releasedTransfer = transferSettled;
  const nextStatus = useUploadStore.getState().rows.next.status;
  release(json({ upload_id: "late", offset: 0, chunk_bytes: 4 }));
  await Promise.all([transfer, cancellation]);
  expect(releasedTransfer).toBe(true);
  expect(nextStatus).toBe("done");
  expect(
    fetchMock.mock.calls.filter(
      ([url, init]) => init?.method === "DELETE" && String(url).endsWith("/late"),
    ),
  ).toHaveLength(1);
});

it("shares the create-cancellation deadline with delayed DELETE reconciliation", async () => {
  vi.useFakeTimers();
  let release!: (response: Response) => void;
  const pending = new Promise<Response>((resolve) => {
    release = resolve;
  });
  fetchMock.mockImplementation((_url, init) =>
    init?.method === "DELETE" ? new Promise<Response>(() => {}) : pending,
  );
  const transfer = transferUpload(row.key, client, wait);
  const cancellation = cancelUpload(row.key);
  await vi.advanceTimersByTimeAsync(149_000);
  release(json({ upload_id: "late", offset: 0, chunk_bytes: 4 }));
  await vi.advanceTimersByTimeAsync(1000);
  expect(useUploadStore.getState().rows.id.cancellationPending).toBe(true);
  await vi.advanceTimersByTimeAsync(1);
  expect(useUploadStore.getState().rows.id.cancellationPending).toBe(false);
  await Promise.all([transfer, cancellation]);
});

it("keeps an expired create cancellation settled when a late cleanup returns a receipt", async () => {
  vi.useFakeTimers();
  const invalidate = vi.spyOn(client, "invalidateQueries");
  let release!: (response: Response) => void;
  const pending = new Promise<Response>((resolve) => {
    release = resolve;
  });
  fetchMock.mockImplementation((_url, init) =>
    init?.method === "DELETE"
      ? Promise.resolve(
          json({
            path: "src/a.bin",
            final_name: "a.bin",
            size: 20,
            sha256: "digest",
            outcome: "replaced",
          }),
        )
      : pending,
  );
  const transfer = transferUpload(row.key, client, wait);
  const cancellation = cancelUpload(row.key);
  await vi.advanceTimersByTimeAsync(150_001);
  const pendingAtDeadline = useUploadStore.getState().rows.id.cancellationPending;
  release(json({ upload_id: "late", offset: 0, chunk_bytes: 4 }));
  await transfer;
  await vi.advanceTimersByTimeAsync(300);
  await cancellation;
  expect(pendingAtDeadline).toBe(false);
  expect(useUploadStore.getState().rows.id).toMatchObject({
    status: "cancelled",
    cancellationPending: false,
  });
  expect(invalidate).not.toHaveBeenCalled();
  expect(toast).toHaveBeenCalledExactlyOnceWith("0 of 1 uploaded, 1 cancelled");
});

it.each(["dismiss", "discard"])(
  "releases an admitted failed upload exactly once before %s",
  async (action) => {
    vi.useFakeTimers();
    let patches = 0;
    fetchMock.mockImplementation(async (url, init) => {
      if (init?.method === "PATCH" && ++patches > 1)
        return errorResponse(502, "upload_relay_failed");
      return server(url, init);
    });
    await transferUpload(row.key, client, wait);
    await vi.advanceTimersByTimeAsync(0);
    if (action === "dismiss") useUploadStore.getState().dismiss([row.key]);
    else discardUploads(row.origin, row.identity);
    await vi.advanceTimersByTimeAsync(0);
    expect(
      fetchMock.mock.calls.filter(
        ([url, init]) => init?.method === "DELETE" && String(url).endsWith("/remote"),
      ),
    ).toHaveLength(1);
    expect(useUploadStore.getState().rows.id).toBeUndefined();
  },
);

it("retries a cleaned-up failed upload with a new create and offset zero", async () => {
  vi.useFakeTimers();
  let patches = 0;
  fetchMock.mockImplementation(async (url, init) => {
    if (init?.method === "PATCH" && ++patches > 1) return errorResponse(502, "upload_relay_failed");
    return server(url, init);
  });
  await transferUpload(row.key, client, wait);
  await vi.advanceTimersByTimeAsync(0);
  expect(useUploadStore.getState().rows.id).toMatchObject({
    status: "failed",
    uploadId: undefined,
    offset: 0,
  });
  fetchMock.mockImplementation(server);
  const before = requests.length;
  await retryUpload(row.key, client);
  await vi.advanceTimersByTimeAsync(0);
  expect(requests.slice(before).map((request) => request.method)).toEqual([
    "POST",
    "PATCH",
    "PATCH",
    "PATCH",
    "POST",
  ]);
  expect(requests.slice(before).find((request) => request.method === "PATCH")?.offset).toBe(0);
  expect(useUploadStore.getState().rows.id.status).toBe("done");
});

it.each(["dismiss", "discard"])(
  "reconciles a failed upload's delayed committed cleanup across %s",
  async (action) => {
    vi.useFakeTimers();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    let patches = 0;
    let release!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => {
      release = resolve;
    });
    const result = {
      path: "src/a.bin",
      final_name: "a.bin",
      size: 20,
      sha256: "digest",
      outcome: "replaced",
    };
    fetchMock.mockImplementation(async (url, init) => {
      if (init?.method === "DELETE") return pending;
      if (init?.method === "PATCH" && ++patches > 1)
        return errorResponse(502, "upload_relay_failed");
      return server(url, init);
    });
    await transferUpload(row.key, client, wait);
    await vi.advanceTimersByTimeAsync(0);
    let guarded = false;
    if (action === "dismiss") {
      useUploadStore.getState().dismiss([row.key]);
      guarded = Boolean(useUploadStore.getState().rows.id);
      await retryUpload(row.key, client);
    } else discardUploads(row.origin, row.identity);
    release(json(result));
    await vi.advanceTimersByTimeAsync(300);
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "DELETE")).toHaveLength(1);
    if (action === "dismiss") {
      expect(guarded).toBe(true);
      expect(useUploadStore.getState().rows.id).toMatchObject({
        status: "done",
        result,
        cancellationPending: false,
        file: undefined,
      });
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: ["workspace-dir", "s", "src"],
        refetchType: "active",
      });
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: ["file-content", "s", "src/a.bin"],
        refetchType: "active",
      });
      expect(toast).toHaveBeenCalledWith("1 of 1 uploaded");
    } else {
      expect(useUploadStore.getState().rows.id).toBeUndefined();
      expect(invalidate).not.toHaveBeenCalled();
      expect(toast).not.toHaveBeenCalledWith("1 of 1 uploaded");
    }
  },
);

it("bounds failed-upload cleanup and refuses retry until reconciliation settles", async () => {
  vi.useFakeTimers();
  let patches = 0;
  let release!: (response: Response) => void;
  const pending = new Promise<Response>((resolve) => {
    release = resolve;
  });
  fetchMock.mockImplementation(async (url, init) => {
    if (init?.method === "DELETE") return pending;
    if (init?.method === "PATCH" && ++patches > 1) return errorResponse(502, "upload_relay_failed");
    return server(url, init);
  });
  await transferUpload(row.key, client, wait);
  const before = fetchMock.mock.calls.length;
  await retryUpload(row.key, client);
  const callsAfterRetry = fetchMock.mock.calls.length;
  await vi.advanceTimersByTimeAsync(150_001);
  const failed = useUploadStore.getState().rows.id;
  useUploadStore.getState().dismiss([row.key]);
  release(new Response(null, { status: 204 }));
  await vi.advanceTimersByTimeAsync(0);
  expect(callsAfterRetry).toBe(before);
  expect(failed).toMatchObject({
    status: "failed",
    cancellationPending: false,
    uploadId: undefined,
    offset: 0,
  });
  expect(useUploadStore.getState().rows.id).toBeUndefined();
  expect(toast).toHaveBeenCalledWith("0 of 1 uploaded, 1 failed");
});

it("reuses one create key after a lost browser response", async () => {
  const ids = new Map<string, string>();
  const keys: string[] = [];
  let lost = false;
  fetchMock.mockImplementation(async (url, init) => {
    if (init?.method === "POST" && !String(url).endsWith("/complete")) {
      const key = new Headers(init.headers).get("Idempotency-Key") ?? "";
      keys.push(key);
      ids.set(key, ids.get(key) ?? "remote");
      if (!lost) {
        lost = true;
        throw new TypeError("Lost response");
      }
      return json({ upload_id: ids.get(key), offset: 0, chunk_bytes: 4 });
    }
    return server(url, init);
  });
  await transferUpload(row.key, client, wait);
  expect(keys).toHaveLength(2);
  expect(keys[0]).toMatch(/^[0-9a-f]{32}$/);
  expect(keys[1]).toBe(keys[0]);
  expect(ids.size).toBe(1);
  expect(useUploadStore.getState().rows.id.status).toBe("done");
});

it("renews the create key when the conflict choice changes", async () => {
  fetchMock.mockResolvedValueOnce(errorResponse(409, "file_exists"));
  await transferUpload(row.key, client, wait);
  const first = new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get("Idempotency-Key");
  await retryUpload(row.key, client, "rename");
  await vi.waitFor(() => expect(useUploadStore.getState().rows.id.status).toBe("done"));
  const creates = fetchMock.mock.calls.filter(
    ([url, init]) => init?.method === "POST" && !String(url).endsWith("/complete"),
  );
  const second = new Headers(creates[1]?.[1]?.headers).get("Idempotency-Key");
  expect(first).toMatch(/^[0-9a-f]{32}$/);
  expect(second).toMatch(/^[0-9a-f]{32}$/);
  expect(second).not.toBe(first);
});

it("resumes from a keyed create replay offset", async () => {
  const keys: string[] = [];
  fetchMock.mockImplementation(async (url, init) => {
    if (init?.method === "POST" && !String(url).endsWith("/complete")) {
      keys.push(new Headers(init.headers).get("Idempotency-Key") ?? "");
      if (keys.length === 1) throw new TypeError("Lost response");
      offset = 8;
      return json({ upload_id: "remote", offset: 8, chunk_bytes: 4 });
    }
    return server(url, init);
  });
  await transferUpload(row.key, client, wait);
  expect(keys[0]).toMatch(/^[0-9a-f]{32}$/);
  expect(keys[1]).toBe(keys[0]);
  expect(
    requests.filter((request) => request.method === "PATCH").map((request) => request.offset),
  ).toEqual([8, 16]);
});

it("stores a create key for a row without one", async () => {
  delete row.createKey;
  let lost = false;
  fetchMock.mockImplementation(async (url, init) => {
    const response = await server(url, init);
    if (init?.method === "POST" && !String(url).endsWith("/complete") && !lost) {
      lost = true;
      throw new TypeError("Lost response");
    }
    return response;
  });
  await transferUpload(row.key, client, wait);
  const keys = fetchMock.mock.calls
    .filter(([url, init]) => init?.method === "POST" && !String(url).endsWith("/complete"))
    .map(([, init]) => new Headers(init?.headers).get("Idempotency-Key"));
  expect(keys[0]).toMatch(/^[0-9a-f]{32}$/);
  expect(keys[1]).toBe(keys[0]);
  expect(useUploadStore.getState().rows.id.createKey).toBe(keys[0]);
});
