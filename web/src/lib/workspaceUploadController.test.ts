import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "./identity";
import { useUploadStore, type UploadRow } from "@/store/uploadStore";
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
    if (action === "cancel") await cancelUpload(row.key);
    else discardUploads(row.origin, row.identity);
    release(json({ upload_id: "admitted", offset: 0, chunk_bytes: 4 }));
    await transfer;
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
