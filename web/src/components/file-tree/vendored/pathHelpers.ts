export function arePathSetsEqual(
  currentPaths: ReadonlySet<string>,
  nextPaths: readonly string[],
): boolean {
  if (currentPaths.size !== nextPaths.length) {
    return false;
  }

  for (const path of nextPaths) {
    if (!currentPaths.has(path)) {
      return false;
    }
  }

  return true;
}

// Expanding a nested directory should make that directory visible, so this
// helper walks its ancestor chain in canonical path form.
export function getAncestorDirectoryPaths(path: string): readonly string[] {
  const normalizedPath = path.endsWith("/") ? path.slice(0, -1) : path;
  if (normalizedPath.length === 0) {
    return [];
  }

  const segments = normalizedPath.split("/");
  return segments.slice(0, -1).map((_, index) => `${segments.slice(0, index + 1).join("/")}/`);
}

export function getImmediateParentPath(path: string): string | null {
  const ancestorPaths = getAncestorDirectoryPaths(path);
  return ancestorPaths.at(-1) ?? null;
}

export const toLowerCaseSearchPath = (path: string): string => path.toLowerCase();
