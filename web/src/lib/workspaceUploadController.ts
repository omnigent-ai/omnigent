import type { QueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  isActiveUpload,
  ownerKey,
  useUploadStore,
  type ConflictChoice,
  type UploadRow,
} from "@/store/uploadStore";
import {
  acknowledgedOffset,
  uploadRequest,
  UploadError,
  type UploadLimits,
  type UploadOwner,
  type UploadResult,
} from "./workspaceUploadsApi";

const transfers = new Map<string, AbortController>();
const deciding = new Set<string>();
const clients = new WeakMap<AbortController, QueryClient>();
const deletions = new WeakMap<AbortController, Map<string, Promise<void>>>();
const refreshes = new Map<
  string,
  {
    timer: ReturnType<typeof setTimeout>;
    paths: Set<string>;
    origin: string;
    identity: string;
  }
>();
const reportedBatches = new Set<string>();

export function refreshUploadedFiles(
  client: QueryClient,
  row: UploadRow,
  result: UploadResult,
): void {
  const key = `${ownerKey(row)}:${row.directory}`;
  const previous = refreshes.get(key);
  if (previous) clearTimeout(previous.timer);
  const paths = previous?.paths ?? new Set<string>();
  if (result.outcome === "replaced") paths.add(result.path);
  const timer = setTimeout(() => {
    refreshes.delete(key);
    const sid = row.session;
    const keys: unknown[][] = [
      ["workspace-dir", sid, row.directory],
      ...(row.directory ? [["workspace-dir", sid, "", row.directory]] : []),
      ["workspace-all-files", sid],
      ["workspace-changed-files", sid],
      ["workspace-file-search", sid],
      ...Array.from(paths).flatMap((path) => [
        ["file-content", sid, path],
        ["file-diff", sid, path],
      ]),
    ];
    for (const queryKey of keys) void client.invalidateQueries({ queryKey, refetchType: "active" });
    void client.invalidateQueries({
      queryKey: ["workspace-dir-listing", sid],
      refetchType: "none",
    });
  }, 300);
  refreshes.set(key, { timer, paths, origin: row.origin, identity: row.identity });
}

export function adaptiveChunk(bytes: number, elapsedMs: number, limits: UploadLimits): number {
  const next = elapsedMs < 3000 ? bytes * 2 : elapsedMs > 8000 ? Math.floor(bytes / 2) : bytes;
  return Math.max(limits.chunk.min, Math.min(next, limits.chunk.max));
}

export function retryDelay(error: unknown, attempt: number): number {
  const retry = error instanceof UploadError ? error.headers.get("Retry-After") : null;
  if (retry) {
    const seconds = Number(retry);
    const delay = Number.isFinite(seconds) ? seconds * 1000 : Date.parse(retry) - Date.now();
    if (Number.isFinite(delay)) return Math.max(0, delay);
  }
  const base = error instanceof UploadError && error.code === "upload_limit" ? 60_000 : 500;
  return base * 2 ** attempt * (0.75 + Math.random() * 0.5);
}

export function waitForRetry(delay: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", abort);
      reject(new DOMException("Upload cancelled", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, delay);
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
  });
}

function retryable(error: unknown): boolean {
  if (!(error instanceof UploadError)) return error instanceof TypeError;
  if (error.status === 501 || (error.status === 503 && error.code === "runner_capability_mismatch"))
    return false;
  return error.status === 429 || error.status >= 500;
}

export async function transferUpload(
  key: string,
  client: QueryClient,
  wait = waitForRetry,
): Promise<void> {
  const store = useUploadStore.getState;
  const initial = store().rows[key];
  if (!initial?.file) return;
  const file = initial.file;
  const signal = initial.controller.signal;
  let chunk = Math.max(initial.limits.chunk.min, Math.min(1024 ** 2, initial.limits.chunk.max));
  let restarts = 0;
  let failures = 0;
  let mismatches = 0;
  let needsHead = Boolean(initial.uploadId);
  let admitted = initial;
  const throttledSince = new Map<string, number>();
  transfers.set(key, initial.controller);
  clients.set(initial.controller, client);
  const ownsFile = () => {
    const live = store().rows[key];
    return live?.controller === initial.controller && live.file === file && !signal.aborted;
  };
  const checkOwnership = () => {
    signal.throwIfAborted();
    if (!ownsFile()) throw new DOMException("Upload no longer owns this file", "AbortError");
  };
  const update = (value: Partial<UploadRow>) => store().update(key, value);
  try {
    const step = async (): Promise<void> => {
      checkOwnership();
      const row = store().rows[key];
      if (!row) return;
      try {
        if (!row.uploadId) {
          update({ status: "connecting", error: undefined });
          const response = await uploadRequest(row, "", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              dir: row.directory,
              name: row.name,
              size: row.size,
              on_conflict: row.conflict,
            }),
          });
          const created = (await response.json()) as {
            upload_id: string;
            offset: number;
            chunk_bytes: number;
          };
          admitted = { ...row, uploadId: created.upload_id };
          update({ uploadId: created.upload_id, offset: created.offset });
          checkOwnership();
          failures = 0;
          throttledSince.clear();
          return step();
        }
        const suffix = `/${encodeURIComponent(row.uploadId)}`;
        if (needsHead) {
          const response = await uploadRequest(row, suffix, { method: "HEAD", signal });
          checkOwnership();
          if (response.headers.get("Upload-Length") !== String(file.size))
            throw new Error("Upload length changed");
          update({ offset: acknowledgedOffset(response, file.size) });
          needsHead = false;
          return step();
        }
        if (row.offset < file.size) {
          update({ status: "uploading" });
          const start = performance.now();
          const body = file.slice(row.offset, Math.min(file.size, row.offset + chunk));
          const response = await uploadRequest(row, suffix, {
            method: "PATCH",
            headers: {
              "Content-Type": "application/offset+octet-stream",
              "Upload-Offset": String(row.offset),
            },
            body,
            signal,
          });
          checkOwnership();
          const offset = acknowledgedOffset(response, file.size);
          if (offset !== row.offset + body.size)
            throw new Error("Unexpected upload acknowledgement");
          update({ offset });
          chunk = adaptiveChunk(chunk, performance.now() - start, row.limits);
          failures = 0;
          mismatches = 0;
          throttledSince.clear();
          return step();
        }
        update({ status: "finalizing" });
        const response = await uploadRequest(row, `${suffix}/complete`, { method: "POST", signal });
        checkOwnership();
        const result = (await response.json()) as UploadResult;
        checkOwnership();
        finishUpload(row, result, client);
        return;
      } catch (error) {
        checkOwnership();
        if (error instanceof UploadError && error.status === 409 && error.code === "file_exists") {
          update({ status: "conflict", error: undefined });
          return;
        }
        if (
          error instanceof UploadError &&
          error.status === 409 &&
          error.code === "offset_mismatch"
        ) {
          if (++mismatches >= 3) throw error;
          update({ offset: acknowledgedOffset(error, file.size) });
          return step();
        }
        if (
          error instanceof UploadError &&
          [404, 410].includes(error.status) &&
          row.uploadId &&
          restarts++ === 0
        ) {
          update({ uploadId: undefined, offset: 0 });
          needsHead = false;
          return step();
        }
        if (!retryable(error)) throw error;
        const throttled = error instanceof UploadError && error.status === 429;
        if (throttled) {
          const since = throttledSince.get(error.code) ?? Date.now();
          throttledSince.set(error.code, since);
          const budget = error.code === "upload_limit" ? 15 * 60_000 : 5 * 60_000;
          if (Date.now() - since >= budget) throw error;
        } else if (++failures >= 3) throw error;
        update({
          error:
            error instanceof UploadError && error.code === "upload_limit"
              ? "Waiting for session upload capacity…"
              : "Reconnecting…",
        });
        await wait(retryDelay(error, throttled ? 0 : failures - 1), signal);
        checkOwnership();
        needsHead =
          Boolean(row.uploadId) && !(error instanceof UploadError && error.status === 429);
      }
      return step();
    };
    await step();
  } catch (error) {
    if (signal.aborted || !ownsFile()) {
      const live = store().rows[key];
      if (live?.controller === initial.controller && live.status !== "done")
        update({ status: "cancelled", file: undefined });
      const cleanup = { ...admitted, file: undefined };
      void deleteUpload(cleanup, client).finally(() => settleCancellation(cleanup));
    } else {
      const message = error instanceof Error ? error.message : "Upload failed";
      update({ status: "failed", error: message });
      store().announce(`${initial.name} upload failed: ${message}`);
    }
  } finally {
    if (transfers.get(key) === initial.controller) transfers.delete(key);
    batchSummary(initial.batch);
  }
}

function finishUpload(row: UploadRow, result: UploadResult, client?: QueryClient): void {
  const store = useUploadStore.getState();
  const live = store.rows[row.key];
  if (live?.controller !== row.controller || live.status === "done") return;
  store.update(row.key, {
    status: "done",
    result,
    offset: result.size,
    file: undefined,
    error: undefined,
  });
  if (client) refreshUploadedFiles(client, row, result);
  store.announce(`${result.final_name} uploaded to ${row.destination}`);
}

function settleCancellation(row: UploadRow): void {
  const live = useUploadStore.getState().rows[row.key];
  if (live?.controller !== row.controller) return;
  if (live.cancellationPending)
    useUploadStore.getState().update(row.key, { cancellationPending: false });
  batchSummary(row.batch);
}

function deleteUpload(row: UploadRow, client = clients.get(row.controller)): Promise<void> {
  const id = row.uploadId;
  if (!id) return Promise.resolve();
  const pending = deletions.get(row.controller) ?? new Map<string, Promise<void>>();
  deletions.set(row.controller, pending);
  const previous = pending.get(id);
  if (previous) return previous;
  const abort = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  // Cover the default finalize budget (120s + 300s/GiB), 5s relay and 25s transport.
  const deadline = 150_000 + Math.ceil((300_000 * row.size) / 1024 ** 3);
  const timeout = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => {
      abort.abort(new DOMException("Upload cancellation timed out", "TimeoutError"));
      reject(abort.signal.reason);
    }, deadline);
  });
  const deletion = (async () => {
    try {
      const receipt = uploadRequest(row, `/${encodeURIComponent(id)}`, {
        method: "DELETE",
        keepalive: true,
        signal: abort.signal,
      }).then(async (response) =>
        response.status === 200 ? ((await response.json()) as UploadResult) : undefined,
      );
      const result = await Promise.race([receipt, timeout]);
      // Publication can win cancellation; DELETE then carries the committed receipt.
      if (result) finishUpload(row, result, client);
    } catch (error) {
      if (error instanceof UploadError && [404, 410].includes(error.status)) return;
      const live = useUploadStore.getState().rows[row.key];
      if (live?.controller !== row.controller) return;
      const message =
        "Server did not confirm cancellation. Temporary staging expires automatically.";
      if (live.status === "cancelled")
        useUploadStore.getState().update(row.key, { error: message });
      toast.error(`${row.name}: ${message}`);
    } finally {
      clearTimeout(timer);
      settleCancellation(row);
    }
  })();
  pending.set(id, deletion);
  return deletion;
}

export function cancelUpload(key: string): Promise<void> {
  const row = useUploadStore.getState().rows[key];
  if (!row || !isActiveUpload(row) || row.cancellationPending) return Promise.resolve();
  const transferring = transfers.get(key) === row.controller;
  row.controller.abort();
  useUploadStore.getState().update(key, {
    status: "cancelled",
    file: undefined,
    cancellationPending: true,
  });
  if (transferring) transfers.delete(key);
  const client = clients.get(row.controller);
  const cleanup = { ...row, file: undefined };
  if (client) pumpUploads(cleanup, client);
  return deleteUpload(cleanup, client).finally(() => {
    if (cleanup.uploadId || !transferring) settleCancellation(cleanup);
  });
}

function batchSummary(batch: string): void {
  const rows = Object.values(useUploadStore.getState().rows).filter((row) => row.batch === batch);
  if (
    !rows.length ||
    rows.some((row) => isActiveUpload(row) || transfers.get(row.key) === row.controller) ||
    reportedBatches.has(batch)
  )
    return;
  reportedBatches.add(batch);
  const done = rows.filter((row) => row.status === "done").length;
  const failed = rows.filter((row) => row.status === "failed").length;
  const cancelled = rows.filter((row) => row.status === "cancelled").length;
  const message = `${done} of ${rows.length} uploaded${failed ? `, ${failed} failed` : ""}${cancelled ? `, ${cancelled} cancelled` : ""}`;
  toast(message);
  useUploadStore.getState().announce(message);
}

export function pumpUploads(owner: UploadOwner, client: QueryClient): void {
  const group = ownerKey(owner);
  const rows = Object.values(useUploadStore.getState().rows).filter(
    (row) => ownerKey(row) === group,
  );
  const limit = Math.max(1, rows[0]?.limits.parallel_files ?? 2);
  let active = rows.filter((row) => transfers.get(row.key) === row.controller).length;
  for (const row of rows) {
    if (active >= limit) break;
    if (row.status !== "queued" || transfers.has(row.key)) continue;
    if (!row.file) {
      const message = "File is unavailable. Select it again to upload.";
      useUploadStore.getState().update(row.key, { status: "failed", error: message });
      useUploadStore.getState().announce(`${row.name} upload failed: ${message}`);
      batchSummary(row.batch);
      continue;
    }
    active += 1;
    void transferUpload(row.key, client).finally(() => {
      pumpUploads(owner, client);
      batchSummary(row.batch);
    });
  }
}

export function enqueueUploads(
  owner: UploadOwner,
  files: File[],
  directory: string,
  destination: string,
  limits: UploadLimits,
  client: QueryClient,
): void {
  if (!files.length) return;
  if (
    files.length > limits.max_files_per_batch ||
    files.some((file) => file.size > limits.max_file_bytes) ||
    files.reduce((sum, file) => sum + file.size, 0) > limits.max_batch_bytes
  ) {
    toast.error("Selection exceeds the workspace upload size or file-count limit.");
    return;
  }
  const batch = crypto.randomUUID();
  const rows = files.map((file): UploadRow => {
    const id = crypto.randomUUID();
    return {
      ...owner,
      id,
      key: `${ownerKey(owner)}:${id}`,
      batch,
      file,
      name: file.name,
      size: file.size,
      directory,
      destination,
      offset: 0,
      controller: new AbortController(),
      limits,
      status: "queued",
      conflict: "fail",
    };
  });
  useUploadStore.getState().add(rows);
  useUploadStore
    .getState()
    .announce(
      `Uploading ${files.length} ${files.length === 1 ? "file" : "files"} to ${destination}`,
    );
  pumpUploads(owner, client);
}

export async function retryUpload(
  key: string,
  client: QueryClient,
  conflict?: ConflictChoice,
): Promise<void> {
  const row = useUploadStore.getState().rows[key];
  if (!row?.file || !["failed", "conflict"].includes(row.status) || deciding.has(key)) return;
  deciding.add(key);
  clients.set(row.controller, client);
  try {
    if (conflict && row.uploadId) await deleteUpload({ ...row, file: undefined }, client);
    const live = useUploadStore.getState().rows[key];
    if (
      live?.controller !== row.controller ||
      live.file !== row.file ||
      live.status !== row.status ||
      row.controller.signal.aborted
    )
      return;
    useUploadStore.getState().update(key, {
      status: "queued",
      error: undefined,
      controller: new AbortController(),
      ...(conflict ? { conflict, uploadId: undefined, offset: 0 } : {}),
    });
    reportedBatches.delete(row.batch);
    pumpUploads(row, client);
  } finally {
    deciding.delete(key);
  }
}

export function discardUploads(origin: string, identity: string): void {
  for (const [key, refresh] of refreshes) {
    if (refresh.origin !== origin || refresh.identity !== identity) continue;
    clearTimeout(refresh.timer);
    refreshes.delete(key);
  }
  const rows = Object.values(useUploadStore.getState().rows).filter(
    (row) => row.origin === origin && row.identity === identity,
  );
  for (const row of rows) {
    row.controller.abort();
    if (transfers.get(row.key) === row.controller) transfers.delete(row.key);
    if (row.uploadId && isActiveUpload(row)) void deleteUpload({ ...row, file: undefined });
  }
  useUploadStore.getState().discardOwner(origin, identity);
}
