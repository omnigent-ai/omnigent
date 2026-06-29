// Surfaces "a session needs your attention" as OS notifications and a
// dock/taskbar badge. Rides the existing conversations poll (no new backend
// signal).
//
// Notifications fire on two "attention" TRANSITIONS, diffed against the
// previous snapshot:
//   * a turn finishing — status `running` -> `idle`/`failed`
//   * a new elicitation — `pending_elicitations_count` increased (the agent
//     is asking the user for input)
//
// The dock badge is NOT transition-based: it's recomputed from the full
// conversation list every tick using the same persistent definition as the
// sidebar — sessions with unseen activity (`isConversationUnseen`, backed by
// the localStorage last-seen baseline) plus sessions awaiting input (pending
// elicitations). That keeps it correct across reloads and counts sessions
// that finished while the app was closed. A session is suppressed only while
// the user is actively viewing it: the window is focused AND it's the open
// conversation. Notifications follow the same rule — anything that needs
// attention notifies, except the conversation you're actively looking at.
//
// Notifications are on by default — there's no settings toggle. In a plain
// browser the Web Notifications API still requires a permission grant, so we
// request it once, lazily, off the first genuine user gesture (prompting on
// load gets downgraded to Chrome's "quiet UI" and silently never appears).
// Granting permission is the opt-in; denying it is respected and never
// re-prompted. Under the Electron desktop shell the OS notification path
// manages permission, so that gate doesn't apply.

import { useEffect, useRef } from "react";
import { useNavigate } from "@/lib/routing";
import { useConversations } from "@/hooks/useConversations";
import type { Conversation } from "@/hooks/useConversations";
import {
  getNotificationPermission,
  requestNotificationPermission,
  showNotification,
} from "@/lib/browserNotifications";
import { isNativeShell, onNativeNotificationActivated, setBadgeCount } from "@/lib/nativeBridge";
import { fetchLastAssistantText } from "@/lib/lastAssistantText";
import { enablePushNotifications } from "@/lib/webPush";
import {
  buildElicitationMap,
  buildStatusMap,
  computeUnreadBadgeIds,
  type ConversationStatus,
  detectIdleTransitions,
  detectNewElicitations,
} from "@/lib/idleTransitions";
import { isConversationUnseen } from "@/hooks/useUnseenConversations";
import { conversationDisplayLabel } from "@/shell/sidebarNav";

const IDLE_BODY = "Agent finished and is ready for your input.";
const ELICITATION_BODY = "Agent is asking for your input.";

/**
 * Attach a one-shot listener that requests notification permission on the
 * first user gesture, then removes itself. Only prompts when the grant is
 * still `default` (never re-asks after grant or denial).
 */
function useLazyPermissionRequest(): void {
  useEffect(() => {
    if (getNotificationPermission() !== "default") return;
    const handler = () => {
      // Promise.resolve wraps the call so a non-Promise return is handled
      // gracefully (and keeps the unit test's simple mock happy).
      void Promise.resolve(requestNotificationPermission()).then((perm) => {
        // When the user grants notifications, also subscribe to Web Push so
        // the server can reach them while the app is backgrounded/closed (#8).
        // Best-effort and feature-detected — no-ops in the native shell or
        // when the server hasn't configured push.
        if (perm === "granted") void enablePushNotifications().catch(() => {});
      });
    };
    // `once` auto-removes the listener after it fires the first time.
    window.addEventListener("pointerdown", handler, { once: true });
    window.addEventListener("keydown", handler, { once: true });
    return () => {
      window.removeEventListener("pointerdown", handler);
      window.removeEventListener("keydown", handler);
    };
  }, []);
}

/** True when the app window currently has focus (SSR-safe default true). */
function isWindowFocused(): boolean {
  if (typeof document === "undefined") return true;
  return typeof document.hasFocus === "function" ? document.hasFocus() : true;
}

/**
 * Watch the conversations list for sessions that need attention and surface
 * them as OS notifications plus a dock/taskbar badge. Mount once, app-wide.
 *
 * Surfaces a turn finishing (`running` → `idle`/`failed`) and a new
 * elicitation. The previous-snapshot refs seed from the first observed
 * value, so sessions already idle at load never fire — only a fresh
 * transition observed by this client does.
 *
 * The badge reflects the number of unread sessions — the same definition the
 * sidebar uses: sessions with unseen activity since the user last had them
 * open (`isConversationUnseen`) plus sessions awaiting input (pending
 * elicitations). It's recomputed from the conversation list every tick (no
 * accumulated state), so it survives reloads and counts sessions that
 * finished while the app was closed. The actively-viewed session is cleared
 * the moment the user views it.
 *
 * :param activeConversationId: The conversation currently open in the UI, or
 *   undefined on a non-chat route, e.g. ``"conv_abc123"``. Used to suppress
 *   the notification/badge for the session the user is actively viewing.
 */
export function useIdleNotifications(activeConversationId?: string): void {
  const navigate = useNavigate();
  const { data } = useConversations();
  const prevStatus = useRef<Map<string, ConversationStatus>>(new Map());
  const prevElicitations = useRef<Map<string, number>>(new Map());
  // Last badge count actually sent to the shell. `null` (nothing sent yet)
  // makes the FIRST computation send unconditionally — including 0 — so a
  // badge left over in the Electron main process from before a reload (it
  // keeps a per-window count that survives in-window navigations) is
  // corrected instead of sticking stale.
  const lastSentBadge = useRef<number | null>(null);
  // Latest conversation list, so the focus listener (mounted once) can
  // recompute the badge without re-subscribing on data changes. `null` until
  // the first fetch resolves — the badge is never computed from a
  // still-loading list, so a reload doesn't flash the stale count to 0
  // before correcting it.
  const latestConversations = useRef<Conversation[] | null>(null);

  useLazyPermissionRequest();

  // Keep `activeConversationId` readable from the focus listener (mounted once)
  // without re-subscribing on every navigation.
  const activeIdRef = useRef<string | undefined>(activeConversationId);
  activeIdRef.current = activeConversationId;

  // Keep the latest `navigate` readable from the once-mounted native-click
  // listener below without re-subscribing whenever the router identity changes.
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;

  // Desktop shell only: clicking an OS notification can't run the web
  // `onClick` closure (it never crosses the IPC boundary), so the shell sends
  // back the notification's in-app path and we route to it here — making a
  // native toast click open its conversation, matching the browser path.
  useEffect(() => {
    return onNativeNotificationActivated((path) => navigateRef.current(path));
    // navigateRef is stable; the listener is mounted once for the app's life.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Send the badge count when it differs from the last one sent. No-op in a
  // plain browser (`setBadgeCount` is inert outside the desktop shell).
  const pushBadge = (count: number) => {
    if (count === lastSentBadge.current) return;
    lastSentBadge.current = count;
    void setBadgeCount(count);
  };

  // Refocusing the window (focused on the open conversation) marks that
  // conversation read: recompute from the latest list with windowFocused
  // true, which drops the actively-viewed id from the count. The persistent
  // last-seen baseline is advanced by ChatPage's `useMarkConversationSeen`,
  // so the next data tick agrees with this immediate recompute.
  useEffect(() => {
    const onFocus = () => {
      if (latestConversations.current === null) return;
      const next = computeUnreadBadgeIds(
        latestConversations.current,
        activeIdRef.current,
        true,
        isConversationUnseen,
      );
      pushBadge(next.size);
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
    // pushBadge only touches refs, so the once-mounted listener stays fresh.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    // Still loading — don't compute a badge from an absent list (and don't
    // bump lastSentBadge), so the first real computation below sends
    // unconditionally.
    if (data === undefined) return;
    const conversations = data.pages.flatMap((page) => page.data);
    latestConversations.current = conversations;

    // Badge first, before the empty-list bail below: an empty list must
    // still send 0 so a stale nonzero badge clears.
    const unread = computeUnreadBadgeIds(
      conversations,
      activeConversationId,
      isWindowFocused(),
      isConversationUnseen,
    );
    pushBadge(unread.size);

    if (conversations.length === 0) return;

    const idle = detectIdleTransitions(prevStatus.current, conversations);
    const newElicitations = detectNewElicitations(prevElicitations.current, conversations);
    prevStatus.current = buildStatusMap(conversations);
    prevElicitations.current = buildElicitationMap(conversations);

    const windowFocused = isWindowFocused();
    const grantedOrNative = isNativeShell() || getNotificationPermission() === "granted";

    // A session is "actively viewed" — and thus suppressed — only when the
    // window is focused AND it's the open conversation.
    if (grantedOrNative) {
      for (const conversation of idle) {
        if (windowFocused && conversation.id === activeConversationId) continue;
        // Show the agent's final words as the body when we can fetch them;
        // fall back to the generic IDLE_BODY. The fetch is best-effort and
        // async, so we resolve it then fire the toast (a one-item-deep
        // network round-trip, only on a genuine turn-end transition).
        notifyWithPreview(conversation, navigate);
      }
      for (const conversation of newElicitations) {
        if (windowFocused && conversation.id === activeConversationId) continue;
        // Skip a duplicate toast if this id also fired the idle branch above.
        if (idle.some((c) => c.id === conversation.id)) continue;
        notify(conversation, ELICITATION_BODY, navigate);
      }
    }
  }, [data, navigate, activeConversationId]);
}

/** Show one notification for a session transition; click opens the chat. */
function notify(
  conversation: Conversation,
  body: string,
  navigate: ReturnType<typeof useNavigate>,
): void {
  const path = `/c/${conversation.id}`;
  showNotification({
    title: conversationDisplayLabel(conversation),
    body,
    // Tag by id so a later update for the same session replaces its
    // toast instead of stacking duplicates.
    tag: `omnigent:session:${conversation.id}`,
    // Browser path: run navigation directly on click. Desktop shell path:
    // `navigatePath` is forwarded over IPC and routed on click instead, since
    // this closure can't cross the process boundary.
    onClick: () => navigate(path),
    navigatePath: path,
  });
}

/**
 * Notify a turn-end, using the agent's final message text as the body when
 * available. Fetches the session's last assistant text best-effort; on any
 * failure (or a turn that ended without trailing assistant text) it falls
 * back to the generic IDLE_BODY. Fire-and-forget: the toast is shown once the
 * preview resolves, so it never blocks the polling effect.
 */
function notifyWithPreview(
  conversation: Conversation,
  navigate: ReturnType<typeof useNavigate>,
): void {
  void fetchLastAssistantText(conversation.id).then((preview) => {
    notify(conversation, preview ?? IDLE_BODY, navigate);
  });
}
