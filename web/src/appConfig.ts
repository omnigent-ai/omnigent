/** Application-wide defaults shared by the standalone and embedded clients. */
export interface SidebarConfig {
  /** False disables polling without disabling loading or explicit refresh. */
  mineRefreshMs: number | false;
  sharedRefreshMs: number | false;
  sharedAvailable: boolean;
  inboxIncludesShared: boolean;
  pinsIncludeShared: boolean;
  pinCap: number;
  maxAutoLoads: number;
  maxRefreshSessions: number;
}

export function refreshInterval(value: string | undefined, fallback: number): number | false {
  if (value === "false" || value === "0") return false;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback;
}

export const appConfig: { readonly sidebar: Readonly<SidebarConfig> } = {
  sidebar: {
    mineRefreshMs: refreshInterval(import.meta.env.VITE_MINE_REFRESH_MS, 60_000),
    sharedRefreshMs: refreshInterval(import.meta.env.VITE_SHARED_REFRESH_MS, 180_000),
    sharedAvailable: true,
    inboxIncludesShared: true,
    pinsIncludeShared: true,
    pinCap: 30,
    maxAutoLoads: 3,
    maxRefreshSessions: 100,
  },
};
