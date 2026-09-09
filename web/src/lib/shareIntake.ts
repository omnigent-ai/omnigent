import { useEffect } from "react";
import { useChatStore } from "@/store/chatStore";

/** Matches an OS-share hand-off URL fragment, e.g. `#shared-text=hello%20world`. */
const SHARE_TEXT_PATTERN = /^#shared-text=(.*)$/;

/** sessionStorage key backing the durable share handoff (see module doc below). */
const STORAGE_KEY = "omnigent:pendingShareText";

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
  if (text === "") return;
  try {
    window.sessionStorage.setItem(STORAGE_KEY, text);
  } catch {
    // Storage unavailable: nothing further we can do here.
  }
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
    // overwrites it, and it never gets applied to the wrong conversation
    // (see the conversationId gate below).
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
}
