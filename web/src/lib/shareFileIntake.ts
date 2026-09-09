// OS-share FILE intake: an ACTION_SEND / ACTION_SEND_MULTIPLE the user
// shared into the native Android shell, delivered live over the bridge (see
// NativeBridgeScript.kt's __omnigentNativeEmitSharedFiles / MainActivity's
// SharedFileReceiver).
//
// This intentionally does NOT mirror shareIntake.ts's sessionStorage-durable
// capture step: that exists because a URL fragment is cheap to persist and
// the unauthenticated-share hard-navigation through /login would otherwise
// wipe it. A shared file's payload is base64 binary (up to several MB per
// file) — not something to round-trip through sessionStorage. Instead this
// relies on two mechanisms already proven in this codebase for the surviving
// concern that actually applies here (an Android app cold start, not an
// in-app auth redirect): the native shell defers emission until its page has
// loaded (MainActivity.flushPendingSharedFiles), and the bridge itself queues
// delivery until the first subscriber (mirrors the notification-tap
// cold-start replay). The unauthenticated-share-hard-navigation race that
// affects text is a known, explicitly out-of-scope gap for files in this
// pass — see the follow-up note in the PR description.
//
// A share landing while a conversation is already open is held in memory
// (not discarded) until conversationId next becomes null — a share's
// intended recipient is always the new-chat composer, the same convention
// `shareIntake.ts` uses for text.

import { useEffect, useRef } from "react";
import { useChatStore } from "@/store/chatStore";
import { onNativeSharedFiles, type NativeSharedFile } from "@/lib/nativeBridge";

/** Decode one base64 payload into a real `File`; null on malformed input. */
function decodeSharedFile(file: NativeSharedFile): File | null {
  try {
    const binary = atob(file.base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new File([bytes], file.name || "shared-file", {
      type: file.mimeType || "application/octet-stream",
    });
  } catch {
    return null;
  }
}

/**
 * Subscribes once (app-wide, alongside `useShareIntake`) to native OS-share
 * file hand-offs and queues decoded `File`s into
 * {@link import("@/store/chatStore").ChatState.pendingComposerFiles} once the
 * user is looking at a new-chat composer. The composer drains that field into
 * its existing upload/attachment flow (`addFiles`), so type/size validation,
 * cancellation (remove-attachment), and no-autosend all come for free — this
 * hook only decides WHEN a share is safe to hand to the composer.
 */
export function useSharedFileIntake(): void {
  const conversationId = useChatStore((s) => s.conversationId);
  // Files that arrived while a conversation was open — held here (not the
  // store) since they're irrelevant to any conversation-scoped rendering
  // until re-queued below.
  const heldRef = useRef<File[]>([]);

  useEffect(() => {
    return onNativeSharedFiles((incoming) => {
      const decoded = incoming.map(decodeSharedFile).filter((f): f is File => f !== null);
      if (decoded.length === 0) return;
      if (useChatStore.getState().conversationId === null) {
        useChatStore.getState().setPendingComposerFiles(decoded);
      } else {
        heldRef.current = [...heldRef.current, ...decoded];
      }
    });
    // Subscribe exactly once for the app's lifetime; the callback above
    // always reads live state via getState() rather than closing over props.
  }, []);

  useEffect(() => {
    if (conversationId !== null) return;
    if (heldRef.current.length === 0) return;
    const held = heldRef.current;
    heldRef.current = [];
    useChatStore.getState().setPendingComposerFiles(held);
  }, [conversationId]);
}
