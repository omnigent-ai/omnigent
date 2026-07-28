export const DEFAULT_WORKSPACE_ENVIRONMENT_ID = "default";

const SCOPED_FILE_PREFIX = "omnigent-file:";

export interface WorkspaceFileIdentity {
  environmentId: string;
  path: string;
}

/**
 * Build the stable identity stored for an open file tab.
 *
 * Default-root files keep their historical path-only identity. Additional
 * roots use an encoded prefix so identical relative paths in two projects do
 * not collide in tabs or persisted workspace state.
 */
export function workspaceFileKey(environmentId: string, path: string): string {
  if (environmentId === DEFAULT_WORKSPACE_ENVIRONMENT_ID) return path;
  return `${SCOPED_FILE_PREFIX}${encodeURIComponent(environmentId)}:${path}`;
}

/** Decode an open-tab identity, including legacy path-only values. */
export function parseWorkspaceFileKey(key: string): WorkspaceFileIdentity {
  if (!key.startsWith(SCOPED_FILE_PREFIX)) {
    return { environmentId: DEFAULT_WORKSPACE_ENVIRONMENT_ID, path: key };
  }
  const separator = key.indexOf(":", SCOPED_FILE_PREFIX.length);
  if (separator === -1) {
    return { environmentId: DEFAULT_WORKSPACE_ENVIRONMENT_ID, path: key };
  }
  const encodedEnvironmentId = key.slice(SCOPED_FILE_PREFIX.length, separator);
  const path = key.slice(separator + 1);
  try {
    return { environmentId: decodeURIComponent(encodedEnvironmentId), path };
  } catch {
    return { environmentId: DEFAULT_WORKSPACE_ENVIRONMENT_ID, path: key };
  }
}
