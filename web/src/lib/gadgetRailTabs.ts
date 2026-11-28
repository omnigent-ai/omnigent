// Requests to show a gadget in a session's side panel, and whether the gadget beside New session
// fills the screen. Gadget code that runs outside the panel (a gadget link, a pinned row, a quote
// sent to the composer) asks here; the session's panel takes the request once it shows.
import { create } from "zustand";

export interface GadgetRailRequest {
  conversationId: string;
  /** The side panel tab, for example `gadget:<id>`. */
  key: string;
  maximize: boolean;
  keepSidebar: boolean;
}

interface GadgetRailState {
  requested: GadgetRailRequest | null;
  /** A request to take a session's gadget tab out of full screen. */
  exitFullScreen: { conversationId: string } | null;
  /** Whether the gadget beside New session fills the screen. */
  newSessionMaximized: boolean;
  /** The gadget filling the screen now, in a session's side panel or beside New session. */
  fullScreenGadgetId: string | null;
  /** Opens the Create session menu once it mounts, before a session exists. */
  createSessionMenuRequested: boolean;
  /**
   * Text gadget code put in New session's composer for that menu's Create session to send,
   * with the gadget it came from. Full screen hides the composer, so the menu shows it for the
   * viewer to read first.
   */
  createSessionReview: GadgetPromptReview | null;
  /**
   * The gadget whose Create session menu is showing {@link createSessionReview} now. New
   * session leaves out its own copy of the draft only for that menu, so switching to another
   * gadget can't hide a message the first one wrote.
   */
  createSessionReviewShownFor: string | null;
  /**
   * How many times a frame of each gadget has connected, and how many sessions started beside
   * each full screen gadget failed to show it. The gadget beside New session covers the session
   * it started until that session's own frame connects, or its start fails.
   */
  frameConnections: Record<string, number>;
  failedStarts: Record<string, number>;
  /**
   * A gadget a session's URL names to fill the screen that the session's panel can't show (it
   * isn't linked there, or gadgets are off): the session shows instead.
   */
  fullScreenUnavailable: { conversationId: string; gadgetId: string } | null;
  /**
   * The session a full screen gadget still covers while the session it started takes over:
   * after the move to the new session's page and before the session shows the gadget itself.
   */
  handoffCover: string | null;
}

/** A message gadget code wrote for a new session, and the gadget it came from. */
export interface GadgetPromptReview {
  prompt: string;
  gadgetId: string;
  gadgetName: string;
}

export const useGadgetRailRequest = create<GadgetRailState>(() => ({
  requested: null,
  exitFullScreen: null,
  newSessionMaximized: false,
  fullScreenGadgetId: null,
  createSessionMenuRequested: false,
  createSessionReview: null,
  createSessionReviewShownFor: null,
  frameConnections: {},
  failedStarts: {},
  fullScreenUnavailable: null,
  handoffCover: null,
}));

/**
 * The query parameter naming the gadget that fills the screen, on a session's page or New
 * session's. It makes the view a link: reloading, sharing or going Forward to it opens the gadget
 * full screen again.
 */
export const GADGET_FULL_SCREEN_PARAM = "gadget";

/** `path` with `gadgetId` filling the screen, keeping its other query parameters. */
export function withGadgetFullScreen(path: string, gadgetId: string): string {
  const hashAt = path.indexOf("#");
  const hash = hashAt < 0 ? "" : path.slice(hashAt);
  const withoutHash = hashAt < 0 ? path : path.slice(0, hashAt);
  const queryAt = withoutHash.indexOf("?");
  const pathname = queryAt < 0 ? withoutHash : withoutHash.slice(0, queryAt);
  const params = new URLSearchParams(queryAt < 0 ? "" : withoutHash.slice(queryAt + 1));
  params.set(GADGET_FULL_SCREEN_PARAM, gadgetId);
  return `${pathname}?${params.toString()}${hash}`;
}

/** Tells the sidebar which gadget fills the screen, or that none does. */
export function setFullScreenGadget(gadgetId: string | null): void {
  if (useGadgetRailRequest.getState().fullScreenGadgetId !== gadgetId)
    useGadgetRailRequest.setState({ fullScreenGadgetId: gadgetId });
}

/**
 * Shows the tab `key` in the side panel of `conversationId`, opening the panel, and in full
 * screen with `maximize`, where the panel shows only that tab; `keepSidebar` leaves the sidebar
 * open then. A request made before navigating to the session waits until its panel shows the tab.
 */
export function requestGadgetRailTab(
  conversationId: string,
  key: string,
  { maximize = false, keepSidebar = false }: { maximize?: boolean; keepSidebar?: boolean } = {},
): void {
  useGadgetRailRequest.setState({ requested: { conversationId, key, maximize, keepSidebar } });
}

/**
 * Takes the gadget out of full screen so the conversation shows: in the side panel of
 * `conversationId`, or beside New session for null.
 */
export function exitGadgetRailFullScreen(conversationId: string | null): void {
  if (conversationId === null) useGadgetRailRequest.setState({ newSessionMaximized: false });
  else useGadgetRailRequest.setState({ exitFullScreen: { conversationId } });
}

/** Whether the gadget beside New session fills the screen. */
export function setGadgetNewSessionTabMaximized(maximized: boolean): void {
  useGadgetRailRequest.setState({ newSessionMaximized: maximized });
}

/**
 * Whether the gadget `gadgetId` fills the screen now: in a session's side panel, or beside New
 * session, where the full screen gadget is the only one showing.
 */
export function gadgetFillsScreen(gadgetId: string): boolean {
  const state = useGadgetRailRequest.getState();
  return state.fullScreenGadgetId === gadgetId || state.newSessionMaximized;
}

/**
 * Opens the Create session menu beside the full screen gadget that has no session yet. With
 * `review`, the menu shows the message gadget code wrote for the session before Create session
 * can send it.
 */
export function requestGadgetCreateSessionMenu(review: GadgetPromptReview | null = null): void {
  useGadgetRailRequest.setState({ createSessionMenuRequested: true, createSessionReview: review });
}

/** Forgets the message the Create session menu was showing, once its session exists. */
export function clearGadgetCreateSessionReview(): void {
  if (useGadgetRailRequest.getState().createSessionReview === null) return;
  useGadgetRailRequest.setState({ createSessionReview: null });
}

/** Notes that *gadgetId*'s Create session menu shows the message gadget code wrote. */
export function noteGadgetCreateSessionReviewShown(gadgetId: string): void {
  if (useGadgetRailRequest.getState().createSessionReviewShownFor === gadgetId) return;
  useGadgetRailRequest.setState({ createSessionReviewShownFor: gadgetId });
}

/** Notes that *gadgetId*'s menu no longer shows it; another gadget's note is left alone. */
export function forgetGadgetCreateSessionReviewShown(gadgetId: string): void {
  if (useGadgetRailRequest.getState().createSessionReviewShownFor !== gadgetId) return;
  useGadgetRailRequest.setState({ createSessionReviewShownFor: null });
}

/** Takes a request to open the Create session menu; true when there was one. */
export function takeGadgetCreateSessionMenuRequest(): boolean {
  if (!useGadgetRailRequest.getState().createSessionMenuRequested) return false;
  useGadgetRailRequest.setState({ createSessionMenuRequested: false });
  return true;
}

/** The count kept for `gadgetId`; an ID such as "constructor" is as ordinary as any other. */
export function gadgetCount(counts: Record<string, number>, gadgetId: string): number {
  return Object.hasOwn(counts, gadgetId) ? counts[gadgetId] : 0;
}

/** Notes that a frame of `gadgetId` connected to its gadget. */
export function noteGadgetFrameConnected(gadgetId: string): void {
  useGadgetRailRequest.setState((state) => ({
    frameConnections: {
      ...state.frameConnections,
      [gadgetId]: gadgetCount(state.frameConnections, gadgetId) + 1,
    },
  }));
}

/** Notes that the session started beside `gadgetId` couldn't show it. */
export function noteGadgetStartFailed(gadgetId: string): void {
  useGadgetRailRequest.setState((state) => ({
    failedStarts: {
      ...state.failedStarts,
      [gadgetId]: gadgetCount(state.failedStarts, gadgetId) + 1,
    },
  }));
}

/** Notes which full screen gadget a session's URL names that its panel can't show, if any. */
export function noteGadgetFullScreenUnavailable(
  unavailable: { conversationId: string; gadgetId: string } | null,
): void {
  const current = useGadgetRailRequest.getState().fullScreenUnavailable;
  if (
    current?.conversationId === unavailable?.conversationId &&
    current?.gadgetId === unavailable?.gadgetId
  )
    return;
  useGadgetRailRequest.setState({ fullScreenUnavailable: unavailable });
}

/**
 * Whether a gadget fills the screen over `conversationId`, or is about to: its URL names one the
 * panel hasn't shown or ruled out yet. Its new messages stay unread meanwhile.
 */
export function gadgetCoversConversation(
  state: GadgetRailState,
  conversationId: string | undefined,
  urlGadget: string | null,
): boolean {
  if (state.fullScreenGadgetId !== null) return true;
  if (conversationId !== undefined && state.handoffCover === conversationId) return true;
  if (conversationId === undefined || urlGadget === null) return false;
  const unavailable = state.fullScreenUnavailable;
  return !(unavailable?.conversationId === conversationId && unavailable.gadgetId === urlGadget);
}

/** Notes the session a handoff's cover still hides (see `handoffCover`), or `null`. */
export function noteGadgetHandoffCover(conversationId: string | null): void {
  if (useGadgetRailRequest.getState().handoffCover === conversationId) return;
  useGadgetRailRequest.setState({ handoffCover: conversationId });
}

export function clearGadgetRailRequest(): void {
  useGadgetRailRequest.setState({ requested: null });
}

/** Forgets every request; for tests. */
export function resetGadgetRailRequests(): void {
  useGadgetRailRequest.setState({
    requested: null,
    exitFullScreen: null,
    newSessionMaximized: false,
    fullScreenGadgetId: null,
    createSessionMenuRequested: false,
    createSessionReview: null,
    createSessionReviewShownFor: null,
    frameConnections: {},
    failedStarts: {},
    fullScreenUnavailable: null,
    handoffCover: null,
  });
}
