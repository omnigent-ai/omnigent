import { useEffect, useRef } from "react";
import type { StaticSandbox } from "@/lib/staticSandboxes";

/**
 * Call `wake` once when the user first types into a composer whose host is
 * paused, so a ~30s resume overlaps typing instead of starting on Send.
 *
 * `pausedKey` names the paused period: `null` while the host is up (or can't
 * be woken in place). The wake fires on the first non-empty text for a given
 * key and not again until the key changes, e.g. the host comes back and later
 * pauses again (`key → null → key`). Focus, clicks, and whitespace never count.
 */
export function useWakeOnFirstInput(
  pausedKey: string | null,
  text: string,
  wake: () => void,
): void {
  const firedFor = useRef<string | null>(null);
  const wakeRef = useRef(wake);
  wakeRef.current = wake;
  useEffect(() => {
    if (pausedKey === null) {
      firedFor.current = null;
      return;
    }
    if (firedFor.current === pausedKey || text.trim() === "") return;
    firedFor.current = pausedKey;
    wakeRef.current();
  }, [pausedKey, text]);
}

/**
 * The New Session flavour: a paused static sandbox picked as the host wakes
 * on the first keystroke (there is no session yet, so it wakes by sandbox id).
 * Wake errors are ignored here; Start surfaces them.
 */
export function useWakeStaticSandboxOnInput(
  target: StaticSandbox | null,
  text: string,
  wake: (provider: string, sandboxId: string) => Promise<void>,
): void {
  const paused = target !== null && target.status === "paused" ? target : null;
  useWakeOnFirstInput(paused !== null ? `${paused.provider}/${paused.id}` : null, text, () => {
    if (paused !== null) void wake(paused.provider, paused.id).catch(() => undefined);
  });
}

/**
 * The chat flavour: a session on a resume-in-place host (`host_resumable`) whose
 * host is offline wakes on the first keystroke via `POST /v1/sessions/{id}/wake`.
 * Hosts without in-place resume never wake here (a keystroke must not create a
 * sandbox), and neither does a gone VM.
 */
export function useWakeSessionOnInput(
  session: {
    id: string;
    hostOffline: boolean;
    hostResumable: boolean;
    sandboxState?: "running" | "paused" | "gone" | null;
  } | null,
  text: string,
  wake: (sessionId: string) => Promise<void>,
): void {
  const pausedKey =
    session !== null &&
    session.hostOffline &&
    session.hostResumable &&
    session.sandboxState !== "gone"
      ? session.id
      : null;
  useWakeOnFirstInput(pausedKey, text, () => {
    if (pausedKey !== null) void wake(pausedKey).catch(() => undefined);
  });
}
