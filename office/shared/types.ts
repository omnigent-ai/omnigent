// Wire types shared by the office service and the browser app.

export type Priority = "normal" | "high" | "critical";

/** One project as the building sees it, assembled by the office service. */
export interface RoomSummary {
  projectId: string;
  name: string;
  icon?: string;
  /** Theme of the active branch, or null for an unthemed project. */
  theme: "sdlc" | null;
  /** Unix epoch seconds. */
  createdAt: number;
  priority: Priority;
  keepUpstairs: boolean;
  /** Epoch seconds of the latest meaningful interaction, or null when none is known. */
  latestInteractionAt: number | null;
  /** Epoch seconds of the office's first successful sync of this project. */
  firstSyncAt: number;
  /** False when some session history could not be scanned far enough. */
  historyComplete: boolean;
  pendingRequestCount: number;
  workerCount: number;
  activeWorkerCount: number;
}

export interface BuildingSnapshot {
  /** Server time (epoch seconds) at which the snapshot was assembled. */
  serverNow: number;
  rooms: RoomSummary[];
  connection: { omnigentUrl: string; ok: boolean; error?: string };
}

export interface RoomSettingsPatch {
  priority?: Priority;
  keepUpstairs?: boolean;
}
