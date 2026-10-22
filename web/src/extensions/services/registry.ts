import type { ExtensionCatalogItem } from "../types";

export const HOST_METHOD_PERMISSIONS = {
  "navigation.openPage": "navigation",
  "navigation.openSession": "navigation",
  "navigation.openNewSession": "navigation",
  "navigation.openExternal": "navigation",
  "theme.getCurrent": null,
  "theme.subscribe": null,
  "storage.user.get": "storage.user",
  "storage.user.set": "storage.user",
  "storage.user.delete": "storage.user",
  "sessions.getCached": "sessions.read",
  "sessions.listPage": "sessions.read",
  "sessions.pullRequest": "sessions.read",
  "projects.list": "projects.read",
  "projects.create": "projects.write",
} as const satisfies Record<string, string | null>;

export function grantedHostMethods<T extends Record<string, unknown>>(
  extension: ExtensionCatalogItem,
  implementations: T,
): Partial<T> {
  const granted: Partial<T> = {};
  for (const [method, implementation] of Object.entries(implementations)) {
    if (!Object.hasOwn(HOST_METHOD_PERMISSIONS, method)) {
      throw new Error(`Host method ${method} has no permission declaration`);
    }
    const permission = HOST_METHOD_PERMISSIONS[method as keyof typeof HOST_METHOD_PERMISSIONS];
    if (permission === null || extension.permissions.includes(permission)) {
      granted[method as keyof T] = implementation as T[keyof T];
    }
  }
  return granted;
}
