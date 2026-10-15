import { create } from "zustand";
import type { UploadLimits, UploadOwner, UploadResult } from "@/lib/workspaceUploadsApi";

export type UploadStatus =
  | "queued"
  | "connecting"
  | "uploading"
  | "finalizing"
  | "done"
  | "conflict"
  | "failed"
  | "cancelled";
export type ConflictChoice = "fail" | "replace" | "rename";

export interface UploadRow extends UploadOwner {
  key: string;
  createKey?: string;
  id: string;
  batch: string;
  file?: File;
  name: string;
  size: number;
  directory: string;
  destination: string;
  offset: number;
  uploadId?: string;
  controller: AbortController;
  limits: UploadLimits;
  status: UploadStatus;
  cancellationPending?: boolean;
  conflict: ConflictChoice;
  error?: string;
  result?: UploadResult;
}

export function ownerKey(owner: UploadOwner): string {
  return JSON.stringify([owner.origin, owner.identity, owner.session, owner.environment]);
}

export function isActiveUpload(row: UploadRow): boolean {
  return Boolean(row.cancellationPending) || !["done", "failed", "cancelled"].includes(row.status);
}

interface UploadStore {
  rows: Record<string, UploadRow>;
  visibleTrays: Record<string, number>;
  announcement: string;
  add: (rows: UploadRow[]) => void;
  update: (key: string, update: Partial<UploadRow>) => void;
  announce: (message: string) => void;
  dismiss: (keys: string[]) => void;
  discardOwner: (origin: string, identity: string) => void;
  showTray: (key: string) => () => void;
}

export const useUploadStore = create<UploadStore>((set) => ({
  rows: {},
  visibleTrays: {},
  announcement: "",
  add: (rows) =>
    set((state) => ({
      rows: { ...state.rows, ...Object.fromEntries(rows.map((row) => [row.key, row])) },
    })),
  update: (key, update) =>
    set((state) => {
      if (!state.rows[key]) return state;
      return { rows: { ...state.rows, [key]: { ...state.rows[key], ...update } } };
    }),
  announce: (announcement) => set({ announcement }),
  showTray: (key) => {
    set((state) => ({
      visibleTrays: { ...state.visibleTrays, [key]: (state.visibleTrays[key] ?? 0) + 1 },
    }));
    return () =>
      set((state) => ({
        visibleTrays: {
          ...state.visibleTrays,
          [key]: Math.max(0, (state.visibleTrays[key] ?? 0) - 1),
        },
      }));
  },
  dismiss: (keys) =>
    set((state) => ({
      rows: Object.fromEntries(
        Object.entries(state.rows).filter(
          ([key, row]) => !keys.includes(key) || isActiveUpload(row),
        ),
      ),
    })),
  discardOwner: (origin, identity) =>
    set((state) => {
      const rows = Object.fromEntries(
        Object.entries(state.rows).filter(([, row]) => {
          if (row.origin !== origin || row.identity !== identity) return true;
          row.controller.abort();
          return false;
        }),
      );
      return { rows };
    }),
}));

export function uploadTotals(rows: UploadRow[]) {
  return {
    total: rows.reduce((sum, row) => sum + row.size, 0),
    acknowledged: rows.reduce((sum, row) => sum + row.offset, 0),
    committed: rows.reduce((sum, row) => sum + (row.status === "done" ? row.size : 0), 0),
    done: rows.filter((row) => row.status === "done").length,
    active: rows.some(isActiveUpload),
  };
}
