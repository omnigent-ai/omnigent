import type { FilePosition, OpenFileOptions } from "./FileViewerContext";

// Requests outlive viewer mounts; fresh citation clicks create fresh objects.
const stopped = new WeakSet<FilePosition>();
const dismissed = new WeakSet<FilePosition>();

export function isFilePositionPending(position: FilePosition | undefined): boolean {
  return !!position && !stopped.has(position);
}

export function stopFilePosition(position: FilePosition): void {
  stopped.add(position);
}

export function dismissFilePosition(position: FilePosition | undefined): void {
  if (!position) return;
  stopped.add(position);
  dismissed.add(position);
}

export function isFilePositionDismissed(position: FilePosition | undefined): boolean {
  return !!position && dismissed.has(position);
}

/** The valid position a citation names; `endLine` is kept only when it follows `line`. */
export function citedFilePosition(options?: OpenFileOptions): FilePosition | undefined {
  const { line, column, endLine } = options ?? {};
  if (!isPositiveInteger(line)) return undefined;
  return {
    line,
    ...(isPositiveInteger(column) ? { column } : {}),
    ...(isPositiveInteger(endLine) && endLine > line ? { endLine } : {}),
  };
}

function isPositiveInteger(value: number | undefined): value is number {
  return Number.isSafeInteger(value) && value! > 0;
}
