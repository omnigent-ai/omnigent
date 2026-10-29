import { toast } from "sonner";

/** Receives a clone's outcome while a dialog shows the clone. */
export interface CloneListener {
  onSuccess: (forkId: string) => void;
  onError: (message: string) => void;
}

/** A clone request that outlives the dialog that started it. */
export interface CloneRequest {
  listener?: CloneListener;
  /** Reports the clone while no dialog shows it. */
  toastId?: string | number;
}

// Running clones by source and cutoff, so a reopened dialog shows the running
// clone instead of starting a duplicate.
export const pendingClones = new Map<string, CloneRequest>();

export function cloneKey(sourceSessionId: string, upToResponseId?: string | null): string {
  return `${sourceSessionId}:${upToResponseId ?? ""}`;
}

/**
 * Run a clone shown by `listener`'s dialog; once no dialog shows it, a toast
 * reports the outcome and `openClone` backs its Open action.
 */
export function startClone(
  key: string,
  run: () => Promise<string>,
  listener: CloneListener,
  openClone: (forkId: string) => void,
): CloneRequest {
  const request: CloneRequest = { listener };
  pendingClones.set(key, request);
  const settle = () => {
    if (pendingClones.get(key) === request) pendingClones.delete(key);
  };
  run().then(
    (forkId) => {
      settle();
      if (request.listener) {
        request.listener.onSuccess(forkId);
        return;
      }
      toast.success("Session created", {
        id: request.toastId,
        duration: 10_000,
        action: { label: "Open", onClick: () => openClone(forkId) },
      });
    },
    (error: unknown) => {
      settle();
      const message =
        error instanceof Error ? error.message : "Couldn't create the session. Try again.";
      if (request.listener) {
        request.listener.onError(message);
        return;
      }
      // Stays until dismissed: the user may be busy elsewhere when it lands.
      toast.error("Couldn't create the session", {
        id: request.toastId,
        description: message,
        duration: Infinity,
        closeButton: true,
      });
    },
  );
  return request;
}

/** A dialog shows the clone again and takes its outcome back from the toast. */
export function attachClone(request: CloneRequest, listener: CloneListener): void {
  request.listener = listener;
  const toastId = request.toastId;
  if (toastId === undefined) return;
  request.toastId = undefined;
  toast.dismiss(toastId);
}

/** No dialog shows the clone any more, so a toast reports it while it runs. */
export function detachClone(request: CloneRequest, key: string): void {
  request.listener = undefined;
  if (pendingClones.get(key) !== request || request.toastId !== undefined) return;
  const toastId = toast.loading("Creating session…", {
    closeButton: true,
    // Dismissing forgets the clone, so a request that never settles can't block a retry.
    onDismiss: () => {
      if (request.toastId === toastId && pendingClones.get(key) === request) {
        pendingClones.delete(key);
      }
    },
  });
  request.toastId = toastId;
}
