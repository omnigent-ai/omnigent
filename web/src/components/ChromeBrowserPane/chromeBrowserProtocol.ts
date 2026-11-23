/** Wire protocol of the Chrome browser stream; the runner's `chrome_browser.py` is its other end. */

/** The host has no Chrome browser configured; retrying cannot help. */
export const WS_CLOSE_NOT_CONFIGURED = 4404;
/** The server refused the viewer. */
const WS_CLOSE_POLICY_VIOLATION = 1008;

export interface ChromeBrowserFrame {
  seq: number;
  /** Page size in CSS pixels, which pointer coordinates use. */
  width: number;
  height: number;
  jpeg: Uint8Array<ArrayBuffer>;
}

export interface ChromeBrowserTab {
  id: string;
  title: string;
  url: string;
}

export interface ChromeBrowserState {
  url: string;
  title: string;
  canGoBack: boolean;
  canGoForward: boolean;
  loading: boolean;
  tabId: string | null;
  tabs: ChromeBrowserTab[];
}

export type ChromeBrowserServerMessage =
  | { type: "ready"; readOnly: boolean }
  | { type: "state"; state: ChromeBrowserState }
  | { type: "notice"; message: string };

export type ChromeBrowserClientMessage =
  | { type: "frame_ack"; seq: number }
  | { type: "resize"; width: number; height: number }
  | {
      type: "mouse";
      event: "move" | "down" | "up" | "wheel";
      x: number;
      y: number;
      button: MouseButtonName;
      buttons: number;
      modifiers: number;
      click_count?: number;
      delta_x?: number;
      delta_y?: number;
    }
  | {
      type: "key";
      event: "down" | "up";
      key: string;
      code: string;
      key_code: number;
      modifiers: number;
      text?: string;
    }
  | { type: "text"; text: string }
  | { type: "navigate"; url: string }
  | { type: "back" | "forward" | "reload" }
  | { type: "select_tab"; id: string };

export type MouseButtonName = "none" | "left" | "middle" | "right";

export function chromeBrowserAttachPath(
  sessionId: string,
  readOnly: boolean,
  hostId?: string,
): string {
  const params = new URLSearchParams();
  if (readOnly) params.set("read_only", "true");
  // A WebSocket handshake cannot carry headers, so the routing key rides the query.
  if (hostId) params.set("omnigent_slice_key", hostId);
  const query = params.toString();
  const path = `/v1/sessions/${encodeURIComponent(sessionId)}/resources/chrome_browser/attach`;
  return query ? `${path}?${query}` : path;
}

/** Split a binary message into its JSON header and JPEG; null when malformed. */
export function decodeFrame(buffer: ArrayBuffer): ChromeBrowserFrame | null {
  if (buffer.byteLength < 4) return null;
  const headerLength = new DataView(buffer).getUint32(0);
  if (headerLength === 0 || 4 + headerLength > buffer.byteLength) return null;
  try {
    const header: unknown = JSON.parse(
      new TextDecoder().decode(new Uint8Array(buffer, 4, headerLength)),
    );
    if (typeof header !== "object" || header === null) return null;
    const { seq, width, height } = header as Record<string, unknown>;
    if (typeof seq !== "number" || typeof width !== "number" || typeof height !== "number") {
      return null;
    }
    return { seq, width, height, jpeg: new Uint8Array(buffer, 4 + headerLength) };
  } catch {
    return null;
  }
}

function tabsFrom(value: unknown): ChromeBrowserTab[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((tab) =>
    typeof tab === "object" && tab !== null && typeof tab.id === "string"
      ? [{ id: tab.id, title: String(tab.title ?? ""), url: String(tab.url ?? "") }]
      : [],
  );
}

/** Parse a text message from the runner; null for anything this build does not know. */
export function parseServerMessage(text: string): ChromeBrowserServerMessage | null {
  let payload: unknown;
  try {
    payload = JSON.parse(text);
  } catch {
    return null;
  }
  if (typeof payload !== "object" || payload === null) return null;
  const message = payload as Record<string, unknown>;
  if (message.type === "ready") return { type: "ready", readOnly: message.read_only === true };
  if (message.type === "notice" && typeof message.message === "string") {
    return { type: "notice", message: message.message };
  }
  if (message.type === "state") {
    return {
      type: "state",
      state: {
        url: typeof message.url === "string" ? message.url : "",
        title: typeof message.title === "string" ? message.title : "",
        canGoBack: message.can_go_back === true,
        canGoForward: message.can_go_forward === true,
        loading: message.loading === true,
        tabId: typeof message.tab_id === "string" ? message.tab_id : null,
        tabs: tabsFrom(message.tabs),
      },
    };
  }
  return null;
}

/** Whether a dropped connection is worth dialing again. */
export function isRetryableClose(code: number): boolean {
  return code !== WS_CLOSE_NOT_CONFIGURED && code !== WS_CLOSE_POLICY_VIOLATION;
}

interface ModifierKeys {
  altKey: boolean;
  ctrlKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
}

/** The DevTools protocol's modifier bit mask. */
export function modifierMask(event: ModifierKeys): number {
  return (
    (event.altKey ? 1 : 0) |
    (event.ctrlKey ? 2 : 0) |
    (event.metaKey ? 4 : 0) |
    (event.shiftKey ? 8 : 0)
  );
}

export function mouseButtonName(button: number): MouseButtonName {
  return button === 0 ? "left" : button === 1 ? "middle" : button === 2 ? "right" : "none";
}

interface Size {
  width: number;
  height: number;
}

/** The largest size with the page's aspect ratio that fits the stage. */
export function fitPage(page: Size, stage: Size): Size {
  if (page.width <= 0 || page.height <= 0 || stage.width <= 0 || stage.height <= 0) {
    return { width: 0, height: 0 };
  }
  const scale = Math.min(stage.width / page.width, stage.height / page.height);
  return { width: page.width * scale, height: page.height * scale };
}

/** A pointer position on the drawn canvas, in the page's CSS pixels. */
export function pagePoint(
  client: { x: number; y: number },
  drawn: { left: number; top: number; width: number; height: number },
  page: Size,
): { x: number; y: number } {
  if (drawn.width <= 0 || drawn.height <= 0) return { x: 0, y: 0 };
  return {
    x: ((client.x - drawn.left) * page.width) / drawn.width,
    y: ((client.y - drawn.top) * page.height) / drawn.height,
  };
}

interface KeyLike extends ModifierKeys {
  key: string;
  code: string;
  keyCode: number;
}

/** A key message; a press carries text only when it types a character. */
export function keyMessage(
  event: KeyLike,
  kind: "down" | "up",
): Extract<ChromeBrowserClientMessage, { type: "key" }> {
  const message: Extract<ChromeBrowserClientMessage, { type: "key" }> = {
    type: "key",
    event: kind,
    key: event.key,
    code: event.code,
    key_code: event.keyCode,
    modifiers: modifierMask(event),
  };
  if (kind === "down") {
    // A shortcut such as Ctrl+A must not also type its letter.
    const types = event.key.length === 1 && !event.ctrlKey && !event.metaKey;
    if (types) message.text = event.key;
    else if (event.key === "Enter") message.text = "\r";
  }
  return message;
}
