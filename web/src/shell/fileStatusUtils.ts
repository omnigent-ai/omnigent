import type { WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";

export function gitStatusLetter(status: WorkspaceChangedFile["status"]): string {
  switch (status) {
    case "created":
      return "A";
    case "deleted":
      return "D";
    case "modified":
      return "M";
  }
}

export function gitStatusLabel(status: WorkspaceChangedFile["status"]): string {
  switch (status) {
    case "created":
      return "Added";
    case "deleted":
      return "Deleted";
    case "modified":
      return "Modified";
  }
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  for (let i = 0; i < units.length; i += 1) {
    if (value < 1024 || i === units.length - 1) {
      return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[i]}`;
    }
    value /= 1024;
  }
  return `${bytes} B`;
}
