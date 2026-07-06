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
// A turn-end is DEFERRED by a short settle window. Agents that work in steps
// emit a `running` -> `idle` edge per step and then resume, so each step would
// otherwise look like "finished." We wait `IDLE_SETTLE_MS` and only notify if
// the session is STILL idle then — a next step (status back to `running`)
// cancels the pending cue, so a multi-step task notifies once at the end
// instead of once per step. A new elicitation ("needs response") is exempt and
// surfaced immediately: it's a stable waiting-on-you state, not a step edge.
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
import {
  buildElicitationMap,
  buildStatusMap,
  computeUnreadBadgeIds,
  type ConversationStatus,
  detectIdleTransitions,
  detectNewElicitations,
} from "@/lib/idleTransitions";
import { isConversationUnseen, useUnseenTick } from "@/hooks/useUnseenConversations";
import { conversationDisplayLabel } from "@/shell/sidebarNav";

const IDLE_BODY = "Agent finished and is ready for your input.";
const ELICITATION_BODY = "Agent is asking for your input.";

// How long a session must stay idle after a turn ends before it's treated as
// "actually done" and notified — long enough that an imminent next step (the
// agent resuming to `running`) cancels the cue, so step-by-step agents don't
// fire a notification per step.
const IDLE_SETTLE_MS = 10_000;

/**
 * Attach a one-shot listener that requests notification permission on the
 * first user gesture, then removes itself. Only prompts when the grant is
 * still `default` (never re-asks after grant or denial).
 */
function useLazyPermissionRequest(): void {
  useEffect(() => {
    if (getNotificationPermission() !== "default") return;
    const handler = () => {
      void requestNotificationPermission();
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
  // Deferred turn-end notifications, keyed by conversation id. A `running` →
  // `idle` edge schedules one; the session resuming (`running` again) clears it
  // before it fires, so only a settled idle — the real end — notifies.
  const idleNotifyTimers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  // Sessions we've already beeped a turn-end for and the user hasn't viewed
  // since. A session that finishes again while its notification is still
  // outstanding must NOT beep again — "sound only when a new banner appears,
  // not when re-ringing one that's already there." This also collapses the
  // multiple turn-ends a single async task produces (e.g. launching subagents,
  // then reporting back) into one beep. Cleared when the user views the session
  // or it drops off the list, so a later finish can notify again.
  const notifiedSessions = useRef<Set<string>>(new Set());

  useLazyPermissionRequest();

  // Keep `activeConversationId` readable from the focus listener (mounted once)
  // without re-subscribing on every navigation.
  const activeIdRef = useRef<string | undefined>(activeConversationId);
  activeIdRef.current = activeConversationId;

  // Keep the latest `navigate` readable from the once-mounted native-click
  // listener below without re-subscribing whenever the router identity changes.
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;

  // Window focus, tracked from the authoritative DOM focus/blur events (and any
  // pointer/key interaction, which implies our window has focus) instead of a
  // polled `document.hasFocus()`. In the Electron shell `document.hasFocus()`
  // can report the focused window as unfocused, which defeated the "focused +
  // open conversation → suppress" rule and let the session you were actively
  // viewing fire a notification (silent before this feature; audible once a
  // sound was added). Seeded from `isWindowFocused()`; corrected by the
  // listeners in the effect below.
  const windowFocusedRef = useRef<boolean>(isWindowFocused());

  // Desktop shell only: clicking an OS notification can't run the web
  // `onClick` closure (it never crosses the IPC boundary), so the shell sends
  // back the notification's in-app path and we route to it here — making a
  // native toast click open its conversation, matching the browser path.
  useEffect(() => {
    return onNativeNotificationActivated((path) => navigateRef.current(path));
    // navigateRef is stable; the listener is mounted once for the app's life.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Clear any deferred turn-end timers on unmount so a pending cue can't fire
  // into a torn-down tree.
  useEffect(() => {
    const timers = idleNotifyTimers.current;
    return () => {
      for (const timer of timers.values()) clearTimeout(timer);
      timers.clear();
    };
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
      windowFocusedRef.current = true;
      if (latestConversations.current === null) return;
      const next = computeUnreadBadgeIds(
        latestConversations.current,
        activeIdRef.current,
        true,
        isConversationUnseen,
      );
      pushBadge(next.size);
    };
    const onBlur = () => {
      windowFocusedRef.current = false;
    };
    // Interacting with the page means our window is focused — a backstop for
    // when it loaded already-focused (no `focus` event fires to seed the ref)
    // or when `document.hasFocus()` seeded it wrong.
    const onInteract = () => {
      windowFocusedRef.current = true;
    };
    window.addEventListener("focus", onFocus);
    window.addEventListener("blur", onBlur);
    window.addEventListener("pointerdown", onInteract);
    window.addEventListener("keydown", onInteract);
    return () => {
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("blur", onBlur);
      window.removeEventListener("pointerdown", onInteract);
      window.removeEventListener("keydown", onInteract);
    };
    // pushBadge only touches refs, so the once-mounted listener stays fresh.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Marking a row read/unread in the sidebar rewrites the last-seen map;
  // recompute the dock badge from the current list so it agrees with the
  // dot immediately, without waiting for the next conversations poll.
  const unseenTick = useUnseenTick();
  useEffect(() => {
    if (latestConversations.current === null) return;
    const next = computeUnreadBadgeIds(
      latestConversations.current,
      activeIdRef.current,
      windowFocusedRef.current,
      isConversationUnseen,
    );
    pushBadge(next.size);
    // pushBadge and the focus state are refs; rerun only when the map changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unseenTick]);

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
      windowFocusedRef.current,
      isConversationUnseen,
    );
    pushBadge(unread.size);

    if (conversations.length === 0) return;

    const idle = detectIdleTransitions(prevStatus.current, conversations);
    const newElicitations = detectNewElicitations(prevElicitations.current, conversations);
    prevStatus.current = buildStatusMap(conversations);
    prevElicitations.current = buildElicitationMap(conversations);

    const windowFocused = windowFocusedRef.current;
    const grantedOrNative = isNativeShell() || getNotificationPermission() === "granted";
    const timers = idleNotifyTimers.current;

    // Resume cancels a pending turn-end: any session back to `running` was just
    // between steps, not finished — drop its deferred cue before it fires.
    for (const conversation of conversations) {
      if (conversation.status !== "running") continue;
      const pending = timers.get(conversation.id);
      if (pending !== undefined) {
        clearTimeout(pending);
        timers.delete(conversation.id);
      }
    }

    // Clear the "already beeped" mark for a session the user is now viewing
    // (they've dealt with it) or that dropped off the list, so a later finish
    // is allowed to beep again.
    const presentIds = new Set(conversations.map((c) => c.id));
    for (const id of notifiedSessions.current) {
      if (!presentIds.has(id) || (windowFocused && id === activeConversationId)) {
        notifiedSessions.current.delete(id);
      }
    }

    // A session is "actively viewed" — and thus suppressed — only when the
    // window is focused AND it's the open conversation.
    if (grantedOrNative) {
      for (const conversation of idle) {
        if (windowFocused && conversation.id === activeConversationId) continue;
        // Skip sessions whose runner is offline. When nothing is actively
        // running, the only thing that flips a session to a terminal status is
        // the server reconciling a dead-runner session (a stale `running`
        // dropping to `failed`/`idle`) — not a real "your agent finished"
        // moment, so it must not beep. This is the phantom beep that fired
        // after the app sat idle with only stale background sessions left.
        // `undefined` connectivity is treated as online so we never
        // over-suppress a genuine completion.
        if (conversation.runner_online === false) continue;
        const id = conversation.id;
        // Already beeped for this session and the user hasn't viewed it since —
        // don't beep again for another finish (no new banner increments). This
        // is what collapses an async task's multiple turn-ends into one beep.
        if (notifiedSessions.current.has(id)) continue;
        // Defer: a turn-end notifies only if the session is STILL idle after
        // the settle window (a step-by-step agent resumes before then, which
        // the resume loop above cancels). Re-arm if one was already pending.
        const existing = timers.get(id);
        if (existing !== undefined) clearTimeout(existing);
        timers.set(
          id,
          setTimeout(() => {
            timers.delete(id);
            // Re-check suppression at fire time — the user may have opened the
            // session (or refocused the window) during the settle window.
            if (windowFocusedRef.current && id === activeIdRef.current) return;
            // Mark it beeped so a later finish stays silent until the user has
            // viewed this session.
            notifiedSessions.current.add(id);
            // Agent's final words as the body when fetchable, else IDLE_BODY.
            notifyWithPreview(conversation, navigateRef.current);
          }, IDLE_SETTLE_MS),
        );
      }
      for (const conversation of newElicitations) {
        if (windowFocused && conversation.id === activeConversationId) continue;
        // Same offline-runner guard: a prompt on a dead-runner session can't be
        // acted on and is almost always stale reconciliation.
        if (conversation.runner_online === false) continue;
        // "Needs response" is surfaced immediately. Drop any deferred turn-end
        // cue for this session — it's awaiting input, not quietly finishing.
        const pending = timers.get(conversation.id);
        if (pending !== undefined) {
          clearTimeout(pending);
          timers.delete(conversation.id);
        }
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
