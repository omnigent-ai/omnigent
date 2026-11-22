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
  request: FileActionRequest | null;
}

export const useFileActionStore = create<FileActionState>(() => ({ request: null }));

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
  useFileActionStore.setState({ request: { target, command, linkState, key: nextKey } });
}

/**
 * Take `request` to run it. Only the first claim wins, so an effect that runs again
 * can't run it twice; a newer request is left alone.
 */
export function claimFileAction(request: FileActionRequest): boolean {
  if (useFileActionStore.getState().request?.key !== request.key) return false;
  useFileActionStore.setState({ request: null });
  return true;
}
