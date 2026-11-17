export function projectHomePath(projectName: string): string {
  return `/projects/${encodeURIComponent(projectName)}`;
}

/** The project a `…/projects/<name>` pathname opens, under any mount path; null elsewhere. */
export function projectNameFromHomePath(pathname: string): string | null {
  const match = /\/projects\/([^/]+)\/?$/.exec(pathname);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}
