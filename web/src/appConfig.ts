/** Application-wide defaults shared by the standalone and embedded clients. */
export interface SidebarConfig {
  mineRefreshMs: number;
  sharedRefreshMs: number;
  sharedEnabledDefault: boolean;
  inboxIncludesShared: boolean;
  pinCap: number;
  maxAutoLoads: number;
  maxRefreshSessions: number;
}

export function refreshInterval(value: string | undefined, fallback: number): number {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback;
}

export const appConfig: { readonly sidebar: Readonly<SidebarConfig> } = {
  sidebar: {
    mineRefreshMs: refreshInterval(import.meta.env.VITE_MINE_REFRESH_MS, 60_000),
    sharedRefreshMs: refreshInterval(import.meta.env.VITE_SHARED_REFRESH_MS, 180_000),
    sharedEnabledDefault: true,
    inboxIncludesShared: true,
    pinCap: 30,
    maxAutoLoads: 3,
    maxRefreshSessions: 200,
  },
};
