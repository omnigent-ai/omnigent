// Requests to show a thingy in a session's side panel, and whether the thingy beside New session
// fills the screen. Thingy code that runs outside the panel (a thingy link, a pinned row, a quote
// sent to the composer) asks here; the session's panel takes the request once it shows.
import { create } from "zustand";

export interface ThingyRailRequest {
  conversationId: string;
  /** The side panel tab, for example `thingy:<id>`. */
  key: string;
  maximize: boolean;
  keepSidebar: boolean;
}

interface ThingyRailState {
  requested: ThingyRailRequest | null;
  /** A request to take a session's thingy tab out of full screen. */
  exitFullScreen: { conversationId: string } | null;
  /** Whether the thingy beside New session fills the screen. */
  newSessionMaximized: boolean;
}

export const useThingyRailRequest = create<ThingyRailState>(() => ({
  requested: null,
  exitFullScreen: null,
  newSessionMaximized: false,
}));

/**
 * Shows the tab `key` in the side panel of `conversationId`, opening the panel, and in full
 * screen with `maximize`, where the panel shows only that tab; `keepSidebar` leaves the sidebar
 * open then. A request made before navigating to the session waits until its panel shows the tab.
 */
export function requestThingyRailTab(
  conversationId: string,
  key: string,
  { maximize = false, keepSidebar = false }: { maximize?: boolean; keepSidebar?: boolean } = {},
): void {
  useThingyRailRequest.setState({ requested: { conversationId, key, maximize, keepSidebar } });
}

/**
 * Takes the thingy out of full screen so the conversation shows: in the side panel of
 * `conversationId`, or beside New session for null.
 */
export function exitThingyRailFullScreen(conversationId: string | null): void {
  if (conversationId === null) useThingyRailRequest.setState({ newSessionMaximized: false });
  else useThingyRailRequest.setState({ exitFullScreen: { conversationId } });
}

/** Whether the thingy beside New session fills the screen. */
export function setThingyNewSessionTabMaximized(maximized: boolean): void {
  useThingyRailRequest.setState({ newSessionMaximized: maximized });
}

export function clearThingyRailRequest(): void {
  useThingyRailRequest.setState({ requested: null });
}

/** Forgets every request; for tests. */
export function resetThingyRailRequests(): void {
  useThingyRailRequest.setState({
    requested: null,
    exitFullScreen: null,
    newSessionMaximized: false,
  });
}
