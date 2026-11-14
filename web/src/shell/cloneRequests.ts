import { toast } from "sonner";

/** Receives a clone's outcome while a form shows the clone. */
export interface CloneListener {
  onSuccess: (forkId: string) => void;
  onError: (message: string) => void;
}

/** A clone request that outlives the form that started it. */
export interface CloneRequest {
  key: string;
  listener?: CloneListener;
  /** Backs the success toast's Open action. */
  openClone: (forkId: string) => void;
  /** Reports the clone while no form shows it. */
  toastId?: string | number;
  settled: boolean;
}

// Running clones by source, cutoff and kind, so a reopened form shows the running
// clone instead of starting a duplicate.
export const pendingClones = new Map<string, CloneRequest>();
const subscribers = new Set<() => void>();

export function cloneKey(
  sourceSessionId: string,
  upToResponseId?: string | null,
  workspaceRecovery = false,
): string {
  return `${sourceSessionId}:${upToResponseId ?? ""}:${workspaceRecovery}`;
}

/** `useSyncExternalStore` subscription for {@link pendingClone}. */
export function subscribeClones(onChange: () => void): () => void {
  subscribers.add(onChange);
  return () => subscribers.delete(onChange);
}

export function pendingClone(key: string): CloneRequest | undefined {
  return pendingClones.get(key);
}

function notify(): void {
  for (const onChange of subscribers) onChange();
}

function forget(request: CloneRequest): void {
  if (pendingClones.get(request.key) !== request) return;
  pendingClones.delete(request.key);
  notify();
}

/** Registers a clone that `listener`'s form shows as it starts. */
export function beginClone(
  key: string,
  listener: CloneListener,
  openClone: (forkId: string) => void,
): CloneRequest {
  const request: CloneRequest = { key, listener, openClone, settled: false };
  pendingClones.set(key, request);
  notify();
  return request;
}

/** Reports a clone's outcome to its form, or to a toast once no form shows it. */
export function finishClone(
  request: CloneRequest,
  outcome: { forkId: string } | { error: string },
): void {
  if (request.settled) return;
  request.settled = true;
  // Read before `forget`: its re-render can detach the form.
  const { listener, toastId } = request;
  forget(request);
  if ("forkId" in outcome) {
    const { forkId } = outcome;
    if (listener) listener.onSuccess(forkId);
    else
      toast.success("Session created", {
        id: toastId,
        duration: 10_000,
        action: { label: "Open", onClick: () => request.openClone(forkId) },
      });
  } else if (listener) {
    listener.onError(outcome.error);
  } else {
    // Stays until dismissed: the user may be busy elsewhere when it lands.
    toast.error("Couldn't create the session", {
      id: toastId,
      description: outcome.error,
      duration: Infinity,
      closeButton: true,
    });
  }
}

/** A form shows the clone again and takes its outcome back from the toast. */
export function attachClone(request: CloneRequest, listener: CloneListener): void {
  if (request.settled) return;
  request.listener = listener;
  const { toastId } = request;
  if (toastId === undefined) return;
  request.toastId = undefined;
  toast.dismiss(toastId);
}

/** No form shows the clone any more, so a toast reports it while it runs. */
export function detachClone(request: CloneRequest): void {
  request.listener = undefined;
  // Deferred so a same-tick attach (StrictMode, a re-render) shows no toast.
  queueMicrotask(() => {
    if (request.listener || request.settled || request.toastId !== undefined) return;
    if (pendingClones.get(request.key) !== request) return;
    // Not `toast.loading`: Sonner gives loading toasts no close button.
    const toastId = toast("Creating session…", {
      duration: Infinity,
      closeButton: true,
      // Dismissing forgets the clone, so a request that never settles can't block a retry.
      onDismiss: () => {
        if (request.toastId === toastId) forget(request);
      },
    });
    request.toastId = toastId;
  });
}
