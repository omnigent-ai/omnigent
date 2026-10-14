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

const running = new Set<string>();
const refreshes = new Map<string, { timer: ReturnType<typeof setTimeout>; paths: Set<string> }>();
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
  refreshes.set(key, { timer, paths });
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
  let needsHead = Boolean(initial.uploadId);
  const update = (value: Partial<UploadRow>) => store().update(key, value);
  try {
    const step = async (): Promise<void> => {
      signal.throwIfAborted();
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
            signal,
          });
          const created = (await response.json()) as {
            upload_id: string;
            offset: number;
            chunk_bytes: number;
          };
          update({ uploadId: created.upload_id, offset: created.offset });
          signal.throwIfAborted();
          failures = 0;
          return step();
        }
        const suffix = `/${encodeURIComponent(row.uploadId)}`;
        if (needsHead) {
          const response = await uploadRequest(row, suffix, { method: "HEAD", signal });
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
          const offset = acknowledgedOffset(response, file.size);
          if (offset !== row.offset + body.size)
            throw new Error("Unexpected upload acknowledgement");
          update({ offset });
          chunk = adaptiveChunk(chunk, performance.now() - start, row.limits);
          failures = 0;
          return step();
        }
        update({ status: "finalizing" });
        const response = await uploadRequest(row, `${suffix}/complete`, { method: "POST", signal });
        const result = (await response.json()) as UploadResult;
        update({ status: "done", result, offset: result.size, file: undefined });
        refreshUploadedFiles(client, row, result);
        store().announce(`${result.final_name} uploaded to ${row.destination}`);
        return;
      } catch (error) {
        signal.throwIfAborted();
        if (error instanceof UploadError && error.status === 409 && error.code === "file_exists") {
          update({ status: "conflict", error: undefined });
          return;
        }
        if (
          error instanceof UploadError &&
          error.status === 409 &&
          error.code === "offset_mismatch"
        ) {
          if (++failures >= 3) throw error;
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
        if (!retryable(error) || ++failures >= 3) throw error;
        update({
          error:
            error instanceof UploadError && error.code === "upload_limit"
              ? "Waiting for session upload capacity…"
              : "Reconnecting…",
        });
        await wait(retryDelay(error, failures - 1), signal);
        needsHead =
          Boolean(row.uploadId) && !(error instanceof UploadError && error.status === 429);
      }
      return step();
    };
    await step();
  } catch (error) {
    if (signal.aborted) {
      update({ status: "cancelled", file: undefined });
      const row = store().rows[key];
      if (row?.uploadId) await deleteUpload(row);
    } else {
      const message = error instanceof Error ? error.message : "Upload failed";
      update({ status: "failed", error: message });
      store().announce(`${initial.name} upload failed: ${message}`);
    }
  }
}

async function deleteUpload(row: UploadRow): Promise<void> {
  if (!row.uploadId) return;
  try {
    await uploadRequest(row, `/${encodeURIComponent(row.uploadId)}`, {
      method: "DELETE",
      keepalive: true,
    });
  } catch (error) {
    if (error instanceof UploadError && [404, 410].includes(error.status)) return;
    toast.error(`Could not cancel ${row.name}. Temporary upload expires automatically.`);
  }
}

export async function cancelUpload(key: string): Promise<void> {
  const row = useUploadStore.getState().rows[key];
  if (!row || !isActiveUpload(row)) return;
  row.controller.abort();
  useUploadStore.getState().update(key, { status: "cancelled", file: undefined });
  await deleteUpload(row);
  batchSummary(row.batch);
}

function batchSummary(batch: string): void {
  const rows = Object.values(useUploadStore.getState().rows).filter((row) => row.batch === batch);
  if (!rows.length || rows.some(isActiveUpload) || reportedBatches.has(batch)) return;
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
  let active = rows.filter((row) => running.has(row.key)).length;
  for (const row of rows) {
    if (active >= limit) break;
    if (row.status !== "queued" || running.has(row.key)) continue;
    active += 1;
    running.add(row.key);
    void transferUpload(row.key, client).finally(() => {
      running.delete(row.key);
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
  useUploadStore.getState().announce(`Uploading ${files.length} files to ${destination}`);
  pumpUploads(owner, client);
}

export async function retryUpload(
  key: string,
  client: QueryClient,
  conflict?: ConflictChoice,
): Promise<void> {
  const row = useUploadStore.getState().rows[key];
  if (!row?.file || !["failed", "conflict"].includes(row.status)) return;
  if (conflict && row.uploadId) await deleteUpload(row);
  useUploadStore.getState().update(key, {
    status: "queued",
    error: undefined,
    controller: new AbortController(),
    ...(conflict ? { conflict, uploadId: undefined, offset: 0 } : {}),
  });
  reportedBatches.delete(row.batch);
  pumpUploads(row, client);
}

export function discardUploads(origin: string, identity: string): void {
  const rows = Object.values(useUploadStore.getState().rows).filter(
    (row) => row.origin === origin && row.identity === identity,
  );
  for (const row of rows) {
    row.controller.abort();
    if (row.uploadId && isActiveUpload(row)) void deleteUpload(row);
  }
  useUploadStore.getState().discardOwner(origin, identity);
}
