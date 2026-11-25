// What a chat attachment shows while its file uploads: render state only. Each owner keeps its
// own controller and decides what Cancel means; the entry carries the owner's callback to the view.
import { create } from "zustand";

export interface UploadProgress {
  name: string;
  /** The file's size, shown while the transport can't say how much has gone. */
  size: number;
  loaded: number;
  /** Bytes in the request, or null while unknown (an indeterminate bar). */
  total: number | null;
  /** Stops the upload; absent once the owner no longer allows cancelling it. */
  cancel?: () => void;
}

interface UploadProgressState {
  uploads: Readonly<Record<string, UploadProgress>>;
}

export const useUploadProgressStore = create<UploadProgressState>(() => ({ uploads: {} }));

/** The progress key of one attachment in one message: its message's id and the file's pending id. */
export function uploadProgressKey(messageId: string, fileId: string): string {
  return `${messageId}/${fileId}`;
}

/** An upload under `key` begins: nothing has been sent yet. */
export function beginUploadProgress(
  key: string,
  upload: Pick<UploadProgress, "name" | "size" | "cancel">,
): void {
  useUploadProgressStore.setState((state) => ({
    uploads: { ...state.uploads, [key]: { ...upload, loaded: 0, total: null } },
  }));
}

/** `loaded` of `total` bytes have gone. Ignored once the upload has ended. */
export function reportUploadProgress(key: string, loaded: number, total: number | null): void {
  const current = useUploadProgressStore.getState().uploads[key];
  if (current === undefined) return;
  // Progress events arrive far more often than the bar can show: re-render only on a whole-percent step.
  const nextTotal = total ?? current.total;
  if (
    nextTotal === current.total &&
    loaded !== nextTotal &&
    wholePercent(loaded, nextTotal) === wholePercent(current.loaded, current.total)
  ) {
    return;
  }
  useUploadProgressStore.setState((state) => ({
    uploads: { ...state.uploads, [key]: { ...current, loaded, total: nextTotal } },
  }));
}

function wholePercent(loaded: number, total: number | null): number | null {
  return total === null || total <= 0 ? null : Math.floor((loaded / total) * 100);
}

/** The upload under `key` ended, by success, failure or cancel: the view stops showing it. */
export function endUploadProgress(key: string): void {
  if (useUploadProgressStore.getState().uploads[key] === undefined) return;
  useUploadProgressStore.setState((state) => ({
    uploads: Object.fromEntries(Object.entries(state.uploads).filter(([id]) => id !== key)),
  }));
}

/** The upload under `key`, as a React subscription; undefined when none is running. */
export function useUploadProgress(key: string): UploadProgress | undefined {
  return useUploadProgressStore((state) => state.uploads[key]);
}
