import { useEffect } from "react";
import { useChatStore } from "@/store/chatStore";
import { onNativeSharedText } from "@/lib/nativeBridge";

/** Matches an OS-share hand-off URL fragment, e.g. `#shared-text=hello%20world`. */
const SHARE_TEXT_PATTERN = /^#shared-text=(.*)$/;

/** sessionStorage key backing the durable share handoff (see module doc below). */
const STORAGE_KEY = "omnigent:pendingShareText";

/**
 * Write `text` into the durable slot, APPENDING onto whatever is already
 * there rather than replacing it. The slot has room for exactly one pending
 * share, but two shares can legitimately land before either is consumed --
 * e.g. two native OS-shares in a row while the composer still shows the
 * first one's recoverable banner (see `ChatPage.tsx`'s drain effect, which
 * merges into an already-open banner the same way). A plain `setItem` would
 * silently discard the first, unconsumed share the moment the second
 * arrived -- the exact class of bug this module exists to prevent.
 */
function persistPendingShareText(text: string): void {
  if (text === "") return;
  try {
    const existing = window.sessionStorage.getItem(STORAGE_KEY);
    window.sessionStorage.setItem(STORAGE_KEY, existing ? `${existing}\n${text}` : text);
  } catch {
    // Storage unavailable: nothing further we can do here.
  }
}

/**
 * Capture a `#shared-text=` OS-share hand-off fragment (see the companion
 * `dev.ozzieba.geckowebapps.ShareActivity` Android intake) into
 * `sessionStorage`, before anything else runs.
 *
 * MUST be called synchronously at module-evaluation time in `main.tsx`,
 * BEFORE `resolveIdentity()` is invoked. An unauthenticated share
 * hard-navigates through `/login` and back (`identity.ts`'s
 * `redirectToLogin` builds `return_to` from `pathname + search` only --
 * the fragment is dropped -- and `LoginPage.tsx` hard-navigates via
 * `window.location.href` on success). A hard navigation reloads the
 * document and wipes every in-memory JS value, Zustand included, so
 * `sessionStorage` (tab-scoped, survives same-origin navigation, cleared
 * on tab close) is the only thing that can carry the text across that
 * round trip. Capturing this early -- before the async identity probe
 * that decides whether a redirect happens -- means the fragment is safe
 * in storage before a redirect can possibly fire.
 *
 * Deliberately hash-based, not a query parameter: this repo's routes are
 * path-based (`react-router` `BrowserRouter`), so the hash is otherwise
 * unused here and can't collide with route matching, and a fragment is
 * never sent in an HTTP request (never reaches a server access log).
 *
 * Idempotent: a page with no share fragment is a no-op. Never throws --
 * `sessionStorage` access can fail in a locked-down WebView/private
 * browsing context, in which case the share is lost the same way it
 * would have been without this durability layer.
 */
export function captureIncomingShareFragment(): void {
  const match = SHARE_TEXT_PATTERN.exec(window.location.hash);
  if (match === null) return;
  let text: string;
  try {
    text = decodeURIComponent(match[1]);
  } catch {
    // Malformed percent-encoding: drop it rather than queue garbage text.
    text = "";
  }
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
  persistPendingShareText(text);
}

/** Read the durably-queued share text without consuming it. Never throws. */
export function peekPendingShareText(): string | null {
  try {
    return window.sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

/**
 * Consume the durably-queued share text: the composer calls this exactly
 * when it has actually handled the text (inserted/appended it, or the user
 * explicitly dismissed it) -- never merely because an effect ran. Removing
 * it eagerly on every effect run is what silently dropped shares that
 * arrived while the composer already held a draft (see `ChatPage.tsx`'s
 * drain effect and its recoverable-banner fallback).
 */
export function takePendingShareText(): void {
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Best-effort; a stale leftover key is harmless -- the next capture
    // appends onto it rather than losing it (see persistPendingShareText),
    // and it never gets applied to the wrong conversation (see the
    // conversationId gate below).
  }
}

/**
 * Receive text shared directly into the native Android shell (an
 * ACTION_SEND whose EXTRA_TEXT the OS delivered live, with no EXTRA_STREAM
 * file attached -- see `MainActivity.handleShareIntent` /
 * `NativeBridgeScript.kt`'s `__omnigentNativeEmitSharedText`). Routed
 * through the SAME durable slot and the SAME composer UI as a web
 * `#shared-text=` fragment share, rather than a parallel mechanism, so
 * insert/banner/no-autosend/intended-recipient behavior is identical
 * regardless of where the share came from.
 *
 * Unlike the fragment path, this can fire long after the app has already
 * mounted (the app was simply running when the OS delivered the share), so
 * -- mirroring `shareFileIntake.ts`'s live-callback handling -- it applies
 * immediately when the new-chat composer is already current, rather than
 * relying solely on `useShareIntake`'s mount-time effect to notice it.
 */
export function receiveNativeSharedText(text: string): void {
  if (text === "") return;
  persistPendingShareText(text);
  if (useChatStore.getState().conversationId === null) {
    const merged = peekPendingShareText();
    if (merged !== null) useChatStore.getState().setPendingComposerText(merged);
  }
}

/**
 * Queues a durably-captured share (see {@link captureIncomingShareFragment})
 * into the active composer's {@link ChatState.pendingComposerText} -- but
 * only while `conversationId` is `null`, i.e. the new-chat composer is the
 * one being shown. A share's intended recipient is always a fresh
 * conversation, never whatever conversation happens to already be open
 * (e.g. resuming a `/c/:id` tab, or a URL restored from browser history);
 * applying it there would put someone else's expected message into the
 * wrong thread. If a share arrives while a real conversation is current,
 * the durable copy stays in `sessionStorage` untouched and simply waits:
 * re-runs (this hook depends on `conversationId`) the next time the
 * new-chat composer becomes current, so nothing is lost, but nothing
 * bleeds into an unrelated conversation either.
 *
 * Does not remove the durable copy itself -- see {@link takePendingShareText}.
 *
 * Also subscribes (once, for the app's lifetime) to native EXTRA_TEXT
 * shares via {@link receiveNativeSharedText} -- the native equivalent of
 * {@link captureIncomingShareFragment}, routed through this same hook so
 * both origins share one gate, one durable slot, and one composer UI.
 */
export function useShareIntake(): void {
  const conversationId = useChatStore((s) => s.conversationId);
  useEffect(() => {
    if (conversationId !== null) return;
    const text = peekPendingShareText();
    if (text !== null) {
      useChatStore.getState().setPendingComposerText(text);
    }
  }, [conversationId]);

  useEffect(() => {
    return onNativeSharedText(receiveNativeSharedText);
    // Subscribe exactly once for the app's lifetime; receiveNativeSharedText
    // always reads live state via getState() rather than closing over props.
  }, []);
}
