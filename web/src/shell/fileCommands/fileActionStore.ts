// Requests to carry out a file action, from any surface. The app-wide
// FileActionHost resolves each one against the target's current facts.

import { create } from "zustand";
import type { FileCommandId } from "./model";
import type { FileCommandTarget } from "./target";

/** The ways to open a file, which stay with the asking surface: it knows where it shows files. */
export type SurfaceFileCommandId =
  "open" | "open-in-new-tab" | "open-as-root" | "open-on-code-host" | "copy-previous-path";

/** The actions the host carries out. */
export type HostedFileCommandId = Exclude<FileCommandId, SurfaceFileCommandId>;

export interface FileActionRequest {
  target: FileCommandTarget;
  command: HostedFileCommandId;
  /** Query parameters a copied link keeps besides the file, such as the viewer's diff state. */
  linkState: string;
  /** New per request, so asking twice for the same action runs it twice. */
  key: number;
}

interface FileActionState {
  /** Requests not yet taken, oldest first, so one made before the last runs still runs. */
  pending: readonly FileActionRequest[];
}

export const useFileActionStore = create<FileActionState>(() => ({ pending: [] }));

/** The request to run next, if any. */
export const nextFileActionRequest = (state: FileActionState): FileActionRequest | null =>
  state.pending[0] ?? null;

let nextKey = 0;

/**
 * Carry out `command` on `target`. Nothing happens if the action no longer
 * applies by the time it's handled.
 */
export function requestFileAction(
  target: FileCommandTarget,
  command: HostedFileCommandId,
  { linkState = "" }: { linkState?: string } = {},
): void {
  nextKey += 1;
  const request = { target, command, linkState, key: nextKey };
  useFileActionStore.setState(({ pending }) => ({ pending: [...pending, request] }));
}

/**
 * Take `request` to run it. Only the first claim wins, so an effect that runs again
 * can't run it twice; the other requests stay queued.
 */
export function claimFileAction(request: FileActionRequest): boolean {
  const { pending } = useFileActionStore.getState();
  if (!pending.some((queued) => queued.key === request.key)) return false;
  useFileActionStore.setState({ pending: pending.filter((queued) => queued.key !== request.key) });
  return true;
}
