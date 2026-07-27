import { useMutation } from "@tanstack/react-query";

import { authenticatedFetch } from "@/lib/identity";

/**
 * Result of a host's OS-native directory chooser.
 *
 * Mirrors the wire shape from
 * ``POST /v1/hosts/{id}/native-directory-dialog``:
 *
 * - ``"ok"`` — the user picked a folder; ``path`` is its absolute POSIX path.
 * - ``"cancelled"`` — the user dismissed the dialog (Esc / Cancel). No path.
 * - ``"unsupported"`` — this host can't show a native dialog (non-macOS, no
 *   GUI session, or an older host build that doesn't advertise the
 *   capability). The caller falls back to the in-app WorkspacePicker.
 * - ``"error"`` — the dialog invocation failed (e.g. osascript error). The
 *   caller falls back to the in-app WorkspacePicker.
 */
export type NativeDirectoryDialogStatus = "ok" | "cancelled" | "unsupported" | "error";

interface NativeDirectoryDialogResponse {
  object: "native_directory_dialog";
  status: NativeDirectoryDialogStatus;
  /** Absolute POSIX path the user picked (set when ``status === "ok"``). */
  path: string | null;
  /** Short message when ``status === "error"``; ``null`` otherwise. */
  error: string | null;
}

export interface NativeDirectoryDialogResult {
  status: NativeDirectoryDialogStatus;
  /** Absolute POSIX path, or ``null`` unless ``status === "ok"``. */
  path: string | null;
  /** Error message, or ``null`` unless ``status === "error"``. */
  error: string | null;
}

/**
 * Open a host's OS-native directory chooser via
 * ``POST /v1/hosts/{id}/native-directory-dialog``.
 *
 * The dialog runs ON THE HOST over the authenticated host tunnel and blocks
 * until the user picks a folder or cancels; the server waits up to 300s (the
 * host endpoint's timeout). Only offered for hosts that advertise the
 * ``native_directory_dialog`` capability (local + interactive macOS hosts); a
 * non-OK HTTP response should be treated as "fall back to the in-app picker".
 *
 * @param hostId Host identifier, e.g. ``"host_a1b2..."``.
 * @returns The dialog result (status + optional path/error).
 * @throws Error carrying the HTTP status line on a non-OK response.
 */
export async function openHostNativeDirectoryDialog(
  hostId: string,
): Promise<NativeDirectoryDialogResult> {
  const res = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(hostId)}/native-directory-dialog`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" },
  );
  if (!res.ok) {
    // Surface the server's detail (e.g. "host is offline" / 504 timeout)
    // so a caller can log a reason, then fall back to the in-app picker.
    let detail = `${res.status} ${res.statusText}`;
    try {
      const err = (await res.json()) as { detail?: string };
      if (typeof err.detail === "string" && err.detail) detail = err.detail;
    } catch {
      // Non-JSON error body — keep the status-line detail.
    }
    throw new Error(detail);
  }
  const body = (await res.json()) as NativeDirectoryDialogResponse;
  return { status: body.status, path: body.path, error: body.error };
}

/**
 * React Query mutation: open the host's native directory chooser.
 *
 * The OS dialog blocks (the host runs ``osascript`` synchronously), so this
 * is a mutation, not a query. Callers gate the trigger on the host's
 * ``native_directory_dialog`` capability (see the ``Host`` type) and, on a
 * non-``ok`` / thrown result, silently fall back to the in-app
 * ``WorkspacePicker``. No cache is invalidated — a chosen path flows straight
 * into the dialog's ``setWorkspace`` callback and existing session-create
 * validation.
 *
 * @returns A React Query mutation; call
 *   ``mutateAsync(hostId)`` and read ``{ status, path, error }``.
 */
export function useOpenHostNativeDirectoryDialog() {
  return useMutation({
    mutationFn: (hostId: string) => openHostNativeDirectoryDialog(hostId),
    // No retry: the dialog is user-driven; a slow click is not a transient
    // failure, and retrying would pop a second dialog on top of the first.
    retry: false,
  });
}
