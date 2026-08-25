// Pure-JS session that bridges an xterm.js terminal to an agent's
// tmux WebSocket. Lives outside React so the wire protocol, listener
// wiring, and resource cleanup don't have to ride the render cycle.
// `TerminalView` mounts a session via a callback ref and tears it
// down on the matching detach.
//
// Wire protocol (mirrors `omnigent/server/routes/terminal_attach.py`):
//   - Server → client: binary frames, raw PTY bytes → `term.write`; text
//     frames for JSON control messages (currently tmux clipboard writes).
//   - Client → server: binary frames for keystrokes (`term.onData`);
//     text frames for JSON control messages (currently only resize).

import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { WebglAddon } from "@xterm/addon-webgl";
import { type ITheme, Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { codeFontFamilyForEditor, readCodeFont } from "@/lib/codeFontPreferences";

// Card background colors derived from the app's CSS palette.
// Light: --card: oklch(1.000 0 0) = pure white.
// Dark:  --card: oklch(0.195 0.004 240) ≈ rgb(19, 21, 23) via OKLab → sRGB.
const CARD_LIGHT = "#ffffff";
const CARD_DARK = "#131517";

// WebSocket close codes (RFC 6455 reserves 4xxx).
// 4400 signals wrong-replica routing: the keyed request reached the wrong
// replica (the ``?omnigent_slice_key=`` doesn't match where the tunnel lives).
// Mirrors ``ws_bridge.py`` ``WS_CLOSE_WRONG_REPLICA``.
export const WS_CLOSE_WRONG_REPLICA = 4400;

/**
 * Return an xterm `ITheme` object matched to the app's light or dark palette.
 */
export function terminalTheme(isDark: boolean): ITheme {
  const bg = isDark ? CARD_DARK : CARD_LIGHT;
  return isDark
    ? {
        background: bg,
        foreground: "#e4e4e7",
        cursor: "#22d3ee",
        cursorAccent: bg,
        selectionBackground: "#22d3ee33",
        black: "#09090b",
        brightBlack: "#71717a",
      }
    : {
        background: bg,
        foreground: "#18181b",
        cursor: "#0891b2",
        cursorAccent: bg,
        selectionBackground: "#0891b233",
        black: "#18181b",
        brightBlack: "#e4e4e7",
        // CLIs that assume a dark terminal paint primary text with ANSI
        // white / bright-white. On the white card background those slots
        // must be dark tones, or the text renders white-on-white and
        // vanishes. brightWhite is the most emphasized text, so it maps to
        // the strongest (darkest) tone; white is a slightly muted gray.
        white: "#3f3f46",
        brightWhite: "#18181b",
      };
}

/**
 * Activation handler for clickable links in terminal output.
 *
 * Wired into {@link WebLinksAddon}. Suppresses the addon's default
 * navigation (which would replace the SPA — and the live terminal
 * session — with the link target) and opens the URL in a new tab
 * instead. ``noopener,noreferrer`` denies the opened page a handle
 * back to this window and strips the ``Referer`` header.
 *
 * Exported for direct unit testing; production code passes it to the
 * addon constructor rather than calling it directly.
 *
 * :param event: The DOM mouse event from the link click. Its default
 *     action (addon-driven navigation) is prevented.
 * :param uri: The URL the addon detected in the terminal output,
 *     e.g. ``"https://example.com/foo"``.
 */
export function openTerminalLink(event: MouseEvent, uri: string): void {
  event.preventDefault();
  const sameOriginSessionPath = sameOriginSessionLink(uri);
  if (sameOriginSessionPath) {
    const currentPath = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    if (sameOriginSessionPath !== currentPath) {
      window.history.pushState(null, "", sameOriginSessionPath);
      window.dispatchEvent(new PopStateEvent("popstate", { state: window.history.state }));
    }
    return;
  }
  window.open(uri, "_blank", "noopener,noreferrer");
}

function sameOriginSessionLink(uri: string): string | null {
  let url: URL;
  try {
    url = new URL(uri, window.location.href);
  } catch {
    return null;
  }
  if (url.origin !== window.location.origin) return null;
  if (!/(^|\/)c\/[^/]+\/?$/.test(url.pathname)) return null;
  return `${url.pathname}${url.search}${url.hash}`;
}

/**
 * Lifecycle state of the bridge, surfaced to React for the
 * connecting / closed / error overlays.
 *
 * The ``closed`` variant carries the WebSocket close code alongside
 * the human-readable reason so consumers can distinguish deliberate
 * server closes (the 4xxx app codes) from transport-level drops —
 * see {@link isUnexpectedTerminalClose}.
 */
export type ConnectionState =
  | { kind: "connecting" }
  | { kind: "connected" }
  | { kind: "closed"; reason: string; code: number }
  | { kind: "error" };

/**
 * Decide whether a WebSocket close code represents a transport-level
 * drop worth auto-reconnecting, rather than a deliberate close.
 *
 * Deliberate closes — normal closure (1000), auth/policy rejections
 * (1008), and the app's own 4xxx codes (4404 terminal-not-found,
 * 4405 terminal-detached, 4500 internal error; see
 * ``omnigent/terminals/ws_bridge.py``) — mean the server decided the
 * attach should end, so re-dialing would either loop on the same
 * answer or resurrect a terminal the user intentionally left.
 *
 * Transport-shaped closes happen *to* the connection rather than
 * being decided by either end's terminal logic:
 *
 * - 1005 / 1006: the browser's own "closed without a clean app code"
 *   sentinels — 1005 is "no status code" (a Close frame with an empty
 *   payload, e.g. a fronting proxy collapsing the close of a backend
 *   that went away), 1006 is "no close frame at all" (a dead TCP
 *   connection discovered on tab thaw). Neither is a code any endpoint
 *   sets deliberately, so both are always a transport drop. A server
 *   redeploy behind an ingress surfaces as 1005 here.
 * - 1001: "going away" — a server or proxy restarting.
 * - 1012 / 1013: service restart / try again later.
 * - 1011 / 1014: server internal error / bad gateway — the fronting
 *   proxy (e.g. Databricks Apps) emits these while the backend is
 *   mid-restart. The app's OWN internal error is the explicit 4500, so
 *   a raw 1011/1014 is infrastructure, not a deliberate terminal end.
 *
 * Pure helper — exported for direct unit testing.
 *
 * :param code: The WebSocket close code from the ``close`` event,
 *     e.g. ``1006``.
 * :returns: ``true`` when the close is transport-shaped and a
 *     reconnect attempt is appropriate.
 */
export function isUnexpectedTerminalClose(code: number): boolean {
  return (
    code === 1001 ||
    code === 1005 ||
    code === 1006 ||
    code === 1011 ||
    code === 1012 ||
    code === 1013 ||
    code === 1014
  );
}

/** Listener for `ConnectionState` transitions. */
export type ConnectionStateListener = (state: ConnectionState) => void;

/** Listener for terminal output activity from the server. */
export type TerminalActivityListener = () => void;
/** Listener for user keyboard input sent to the terminal. */
export type TerminalInputListener = () => void;

/** Kitty Keyboard Protocol / CSI-u encoding for Shift+Enter. */
export const SHIFT_ENTER_CSI_U = "\x1b[13;2u";

/**
 * Return the terminal bytes to send for a browser key event.
 *
 * xterm.js does not currently emit Kitty Keyboard Protocol sequences for
 * Shift+Enter, so the browser attach path synthesizes the CSI-u sequence
 * for that one key combination. This mirrors native terminals that support
 * CSI-u while keeping plain Enter and modified Enter variants on xterm's
 * default path.
 *
 * :param event: Browser keyboard event from xterm's custom key handler.
 * :returns: CSI-u bytes for Shift+Enter, or ``null`` to let xterm handle
 *     the event normally.
 */
export function terminalKeyEventPayload(event: KeyboardEvent): string | null {
  if (
    event.key === "Enter" &&
    event.shiftKey &&
    !event.altKey &&
    !event.ctrlKey &&
    !event.metaKey
  ) {
    return SHIFT_ENTER_CSI_U;
  }
  return null;
}

// Reused across keystrokes — allocating a fresh TextEncoder per keypress
// is needless churn on the input hot path.
const INPUT_ENCODER = new TextEncoder();

/**
 * Structural view of xterm's internal mouse service. The public modes API
 * exposes tracking but not the active encoding.
 */
interface TerminalCore {
  _core?: {
    coreMouseService?: { activeEncoding?: string };
  };
}

/**
 * Load the WebGL renderer onto *term*, returning the addon or ``null``
 * when WebGL is unavailable and the DOM renderer stays in use.
 *
 * xterm's default DOM renderer rebuilds spans on every paint, which
 * dominates the main thread on heavy output (large ``cat``, build logs,
 * a redrawing TUI); the WebGL renderer rasterizes glyphs on the GPU and
 * is dramatically faster for those bursts. Loaded *after*
 * {@link Terminal.open} because it needs the mounted ``<canvas>``. Both
 * the no-GPU and context-lost paths fall back to the DOM renderer rather
 * than freezing the canvas — see the inline comments below.
 */
export function loadWebglRenderer(term: Terminal): WebglAddon | null {
  let addon: WebglAddon;
  try {
    addon = new WebglAddon();
  } catch {
    return null;
  }
  // Dispose on context loss so xterm reverts to the DOM renderer; a
  // disposed WebGL addon left attached would freeze on its last frame.
  addon.onContextLoss(() => addon.dispose());
  try {
    term.loadAddon(addon);
  } catch {
    // WebGL unsupported in this environment (no GPU context, jsdom).
    // The DOM renderer stays active; correctness is unaffected.
    addon.dispose();
    return null;
  }
  return addon;
}

/**
 * Populate the clipboard from a terminal text selection on a browser
 * ``copy`` event.
 *
 * The attached tmux session runs with ``mouse on``, so a plain click-drag
 * is captured by tmux for its own copy-mode and never becomes a browser
 * selection; the user makes a selection with Shift-drag (non-Mac) or
 * ⌥-drag (Mac, via ``macOptionClickForcesSelection``). xterm renders that
 * selection in its own layer rather than a DOM range, so the browser's
 * default copy of it is unreliable — we feed ``term.getSelection()`` into
 * the event's ``clipboardData`` ourselves. ``getSelection()`` already
 * rejoins soft-wrapped rows, so a paragraph the terminal wrapped across
 * several rows copies back as one logical line.
 *
 * We never remap Ctrl+C — in a terminal it must stay SIGINT — so on
 * Linux/Windows this fires via right-click → Copy (and Edit → Copy); on
 * macOS ⌘C also dispatches a browser ``copy`` event.
 *
 * Pure helper — exported for direct unit testing; production code wires it
 * to a container ``copy`` listener rather than calling it directly.
 *
 * :param event: The browser ``copy`` event.
 * :param selection: The current terminal selection text ("" if none).
 * :returns: ``true`` if the clipboard was populated, ``false`` when there
 *     was no selection to copy (the event is left untouched so the
 *     browser's default copy behavior still applies elsewhere).
 */
export function applyTerminalCopy(
  event: Pick<ClipboardEvent, "clipboardData" | "preventDefault">,
  selection: string,
): boolean {
  if (!selection) return false;
  event.clipboardData?.setData("text/plain", selection);
  event.preventDefault();
  return true;
}

/** Largest tmux selection accepted for a browser clipboard write. */
export const TERMINAL_CLIPBOARD_MAX_BYTES = 1024 * 1024;
/** A tmux copy notification must closely follow input on this attachment. */
export const TERMINAL_CLIPBOARD_INPUT_WINDOW_MS = 5000;

const STRICT_BASE64_RE = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;

/** Decode one bounded, canonical base64 terminal clipboard payload. */
export function decodeTerminalClipboardBase64(encoded: string): string | null {
  if (
    encoded.length === 0 ||
    encoded.length > Math.ceil(TERMINAL_CLIPBOARD_MAX_BYTES / 3) * 4 ||
    encoded.length % 4 !== 0 ||
    !STRICT_BASE64_RE.test(encoded)
  ) {
    return null;
  }
  let binary: string;
  try {
    binary = atob(encoded);
  } catch {
    return null;
  }
  if (binary.length === 0 || binary.length > TERMINAL_CLIPBOARD_MAX_BYTES) return null;
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

/** Parse a tmux-compatible OSC 52 clipboard-set payload. */
export function parseOsc52Clipboard(data: string): string | null {
  const separator = data.indexOf(";");
  if (separator < 0) return null;
  const selector = data.slice(0, separator);
  const encoded = data.slice(separator + 1);
  // tmux uses the empty selector; `c` is the standard system clipboard.
  if ((selector !== "" && selector !== "c") || encoded === "?") return null;
  return decodeTerminalClipboardBase64(encoded);
}

/** Parse the strict server→browser clipboard control-message schema. */
export function parseTerminalClipboardMessage(message: string): string | null {
  let value: unknown;
  try {
    value = JSON.parse(message);
  } catch {
    return null;
  }
  if (
    typeof value !== "object" ||
    value === null ||
    (value as { type?: unknown }).type !== "clipboard-write" ||
    (value as { encoding?: unknown }).encoding !== "base64" ||
    typeof (value as { data?: unknown }).data !== "string"
  ) {
    return null;
  }
  return decodeTerminalClipboardBase64((value as { data: string }).data);
}

/** Parse the server's explicit PTY OSC 52 trust capability. */
export function parseTerminalOsc52Capability(message: string): boolean | null {
  let value: unknown;
  try {
    value = JSON.parse(message);
  } catch {
    return null;
  }
  if (
    typeof value !== "object" ||
    value === null ||
    (value as { type?: unknown }).type !== "osc52-clipboard-capability" ||
    typeof (value as { enabled?: unknown }).enabled !== "boolean"
  ) {
    return null;
  }
  return (value as { enabled: boolean }).enabled;
}

/** Whether a clipboard event is attributable to recent input on this attach. */
export function hadRecentTerminalInput(lastInputAt: number, now: number): boolean {
  return (
    lastInputAt > 0 && now >= lastInputAt && now - lastInputAt <= TERMINAL_CLIPBOARD_INPUT_WINDOW_MS
  );
}

/** Listener for tmux copy-mode text destined for the user's clipboard. */
export type TerminalClipboardListener = (text: string) => void;

/**
 * Ceiling on synthesized wheel reports for a single DOM wheel event, so a
 * page-mode or pathological delta can't flood the input channel.
 */
export const WHEEL_REPORTS_MAX_PER_EVENT = 50;

/**
 * Mouse state consulted by {@link wheelReportPayload}. ``mouseTrackingMode``
 * comes from the public ``term.modes``; ``sgrEncoding`` is whether the pane
 * program requested SGR mouse encoding (``?1006h``) — the public ``IModes``
 * does not expose the encoding, so the session feature-detects it from
 * xterm's core mouse service (see ``TerminalSession.sgrMouseEncodingActive``).
 */
export interface WheelMouseState {
  mouseTrackingMode: "none" | "x10" | "vt200" | "drag" | "any";
  sgrEncoding: boolean;
}

/** Screen geometry needed to place and scale a wheel report. */
export interface WheelScreenMetrics {
  /** Viewport coordinates of the character grid's top-left corner. */
  left: number;
  top: number;
  /** Size of one character cell in CSS pixels. */
  cellWidth: number;
  cellHeight: number;
  cols: number;
  rows: number;
}

/**
 * Build SGR mouse-wheel reports for *lines* scroll steps at cell
 * (*col*, *row*), 1-based. Negative lines scroll up (button 64),
 * positive down (button 65); 0 yields "".
 */
export function sgrWheelReports(lines: number, col: number, row: number): string {
  if (lines === 0) return "";
  const button = lines < 0 ? 64 : 65;
  return `\x1b[<${button};${col};${row}M`.repeat(Math.abs(lines));
}

/**
 * Decide how one DOM wheel event over the terminal becomes SGR mouse-wheel
 * reports, carrying fractional scroll across events.
 *
 * xterm's built-in wheel→report conversion is unusable with macOS
 * trackpads: it damps sub-50px pixel deltas by ×0.3 and emits at most one
 * report per DOM event regardless of magnitude, so two-finger scrolling
 * over a mouse-tracking TUI (Claude Code, tmux with ``mouse on``) barely
 * moves. This helper replaces that path: deltas convert to lines at face
 * value, the fractional remainder accumulates in *partial* so a run of
 * small trackpad deltas still adds up, and one report is emitted per whole
 * line (capped at {@link WHEEL_REPORTS_MAX_PER_EVENT}; the excess is
 * discarded rather than banked so a giant delta can't keep scrolling long
 * after the gesture).
 *
 * The event is only consumed when the pane program is tracking the mouse
 * with SGR encoding — both tmux ``mouse on`` (PTY transport) and Claude
 * Code's own tracking (control transport) request SGR. Otherwise the
 * caller must let xterm handle the wheel natively so, e.g., a plain shell
 * on the control transport scrolls xterm's own scrollback. Shift-wheel is
 * also left to xterm, mirroring its built-in escape hatch.
 *
 * Pure helper — exported for direct unit testing; production code calls it
 * from the session's custom wheel handler.
 *
 * :param event: The DOM wheel event fields consulted.
 * :param mouse: Current mouse tracking mode + SGR-encoding flag.
 * :param screen: Character-grid geometry, or ``null`` when layout isn't
 *     measurable yet (event is left to xterm).
 * :param partial: Fractional lines carried over from previous events.
 * :returns: ``consume`` — whether the caller owns the event (prevent
 *     default, return ``false`` to xterm); ``data`` — SGR reports to feed
 *     to the terminal ("" when the accumulator hasn't reached a whole
 *     line); ``partial`` — the new carry.
 */
export function wheelReportPayload(
  event: Pick<WheelEvent, "deltaY" | "deltaMode" | "shiftKey" | "clientX" | "clientY">,
  mouse: WheelMouseState,
  screen: WheelScreenMetrics | null,
  partial: number,
): { consume: boolean; data: string; partial: number } {
  if (mouse.mouseTrackingMode === "none" || !mouse.sgrEncoding) {
    // Tracking off (or an encoding we don't synthesize): xterm's native
    // handling is correct. Drop the carry so a stale fraction can't leak
    // into the next tracking-on scroll.
    return { consume: false, data: "", partial: 0 };
  }
  if (event.shiftKey || event.deltaY === 0 || screen === null) {
    return { consume: false, data: "", partial };
  }
  let lines: number;
  switch (event.deltaMode) {
    case WheelEvent.DOM_DELTA_LINE:
      lines = event.deltaY;
      break;
    case WheelEvent.DOM_DELTA_PAGE:
      lines = event.deltaY * screen.rows;
      break;
    default:
      lines = event.deltaY / screen.cellHeight;
  }
  const total = partial + lines;
  const whole = Math.trunc(total);
  const capped = Math.max(
    -WHEEL_REPORTS_MAX_PER_EVENT,
    Math.min(WHEEL_REPORTS_MAX_PER_EVENT, whole),
  );
  const clamp = (v: number, max: number) => Math.min(Math.max(v, 1), max);
  const col = clamp(Math.floor((event.clientX - screen.left) / screen.cellWidth) + 1, screen.cols);
  const row = clamp(Math.floor((event.clientY - screen.top) / screen.cellHeight) + 1, screen.rows);
  return {
    consume: true,
    data: sgrWheelReports(capped, col, row),
    partial: capped === whole ? total - whole : 0,
  };
}

/**
 * One xterm ↔ tmux WebSocket bridge tied to a single DOM container.
 *
 * The constructor performs all the setup synchronously — open the
 * terminal on the container, open the WebSocket, wire up listeners,
 * attach a ResizeObserver. {@link dispose} tears them all down in
 * the same order callers expect: abort listeners first (so the
 * close event doesn't fire stale state into a remounted view),
 * disconnect the observer, dispose the xterm data subscription,
 * close the WS, dispose the terminal.
 */
export class TerminalSession {
  private readonly term: Terminal;
  private readonly fit: FitAddon;
  /** WebGL renderer addon, or ``null`` when WebGL is unavailable. */
  private readonly webgl: WebglAddon | null;
  private readonly ws: WebSocket;
  private readonly listenerCtl: AbortController;
  private readonly resizeObserver: ResizeObserver;
  private readonly dataDispose: { dispose: () => void };
  private readonly osc52Dispose: { dispose: () => void };
  private readonly onClipboardRequest?: TerminalClipboardListener;
  /** Whether this visible, interactive attach may write the local clipboard. */
  private clipboardEnabled: boolean;
  /** Explicit server capability; defaults off so old/unknown servers fail safe. */
  private osc52ClipboardEnabled = false;
  /** ``performance.now()`` of the last keystroke; gates clipboard writes. */
  private lastUserInputAt = 0;
  /** Guards {@link dispose} so calling it twice is a safe no-op. */
  private disposed = false;
  /**
   * Last ``cols×rows`` actually sent to the server, or ``null`` before the
   * first resize. {@link sendResize} skips a send when the fitted dimensions
   * are unchanged so the WS-open + ResizeObserver double-fire on mount (and a
   * transient re-fit) don't emit a redundant resize — which, on the tmux
   * control transport, would otherwise be an avoidable ``refresh-client -C``.
   */
  private lastSentSize: { cols: number; rows: number } | null = null;
  /** Fractional wheel lines carried across events (see {@link wheelReportPayload}). */
  private wheelPartialLines = 0;

  /**
   * Construct, attach to the DOM, and open the WebSocket.
   *
   * :param container: DOM node to mount the xterm Terminal under.
   * :param url: Fully-qualified ``ws(s)://`` URL for the
   *     ``.../resources/terminals/{id}/attach`` endpoint.
   * :param onState: Called with each state transition so React can
   *     render the connecting / closed / error overlay. Invoked
   *     synchronously from WS event handlers.
   * :param onActivity: Called whenever PTY output arrives from the
   *     server. This is a best-effort UI activity signal, not a shell
   *     job-state oracle.
   * :param onInput: Called when user input is sent to the terminal.
   * :param nativeSelection: When ``true`` (control-mode transport), xterm
   *     owns the character buffer and mouse, so plain click-drag selects and
   *     the browser's own copy works — the ``macOptionClickForcesSelection``
   *     workaround and the custom ``copy`` listener are skipped. When
   *     ``false`` (PTY transport, the default), tmux runs with ``mouse on``
   *     and captures drags, so both workarounds stay wired.
   * :param clipboardEnabled: Whether tmux copies may write the local clipboard.
   * :param onClipboardRequest: Receives validated tmux copy-mode text.
   */
  constructor(
    container: HTMLElement,
    url: string,
    onState: ConnectionStateListener,
    isDark = false,
    onActivity?: TerminalActivityListener,
    onInput?: TerminalInputListener,
    nativeSelection = false,
    clipboardEnabled = true,
    onClipboardRequest?: TerminalClipboardListener,
  ) {
    this.clipboardEnabled = clipboardEnabled;
    this.onClipboardRequest = onClipboardRequest;
    // Read the user's code-font preference (Settings → Appearance) at
    // construction; a mid-session change is applied live via setFont(). The
    // xterm.js defaults (15px, no theme) feel out of place inside the app
    // chrome, so an unset family falls back to the shared mono stack.
    const { sizePx, family } = readCodeFont();
    this.term = new Terminal({
      fontFamily: codeFontFamilyForEditor(family),
      fontSize: sizePx,
      scrollback: 20000,
      cursorBlink: true,
      theme: terminalTheme(isDark),
      // 256-color indices (e.g. Claude Code's 38;5;231 white) can't be
      // remapped via ITheme (slots 0-15 only), so they vanish on the
      // light theme's white card. This WCAG AA contrast floor nudges a
      // cell's foreground luminance only when it lacks contrast against
      // its actual background.
      minimumContrastRatio: 4.5,
      // PTY transport only: the attached tmux session runs with `mouse on`
      // (terminal.py) so the wheel pages through scrollback, but tmux then
      // captures every mouse drag for its own copy-mode, so a plain
      // click-drag never produces a browser text selection. xterm's escape
      // hatch `macOptionClickForcesSelection` lets Mac users ⌥-drag to select,
      // then ⌘-C copies. In control mode xterm owns the mouse and plain drag
      // selects natively, so the forced-selection workaround is unnecessary.
      macOptionClickForcesSelection: !nativeSelection,
      // Opt into xterm's proposed APIs, matching openui's terminal setup.
      allowProposedApi: true,
    });
    // PTY tmux clients emit OSC 52 after copy-mode commits. Consume only
    // bounded write requests; clipboard reads/queries are deliberately absent.
    this.osc52Dispose = this.term.parser.registerOscHandler(52, (data) => {
      // Control mode forwards raw pane output, so accepting pane OSC 52 there
      // would bypass tmux's `set-clipboard external` trust boundary. Its copy
      // path is the typed websocket message emitted from tmux notifications.
      if (!nativeSelection && this.osc52ClipboardEnabled) {
        const text = parseOsc52Clipboard(data);
        if (text !== null) this.requestClipboardWrite(text);
      }
      return true;
    });
    this.fit = new FitAddon();
    this.term.loadAddon(this.fit);
    // Turn bare URLs in terminal output into clickable links. Without
    // this addon xterm renders URLs as plain text.
    this.term.loadAddon(new WebLinksAddon(openTerminalLink));
    this.term.open(container);
    // Load the GPU renderer after open() (it needs the mounted canvas).
    // Falls back to the DOM renderer when WebGL is unavailable.
    this.webgl = loadWebglRenderer(this.term);
    try {
      this.fit.fit();
    } catch (err) {
      console.warn("[terminal-attach] initial fit failed, falling back to 80x24", err);
      this.term.resize(80, 24);
    }

    this.ws = new WebSocket(url);
    // Default is Blob, which forces an async read per chunk. ArrayBuffer
    // keeps the path synchronous and matches xterm.js's preferred input.
    this.ws.binaryType = "arraybuffer";

    // AbortController-scoped listeners so the cleanup's ws.close()
    // can't fire stale `close`/`error` events into the next mount —
    // under React StrictMode this otherwise flickers a "Bridge
    // closed" overlay on top of the freshly-connecting terminal.
    this.listenerCtl = new AbortController();
    const { signal } = this.listenerCtl;

    // Make the browser copy gesture (right-click → Copy and Edit → Copy on
    // every platform, ⌘C on macOS) yield the terminal selection as text.
    // Without this, a Shift/⌥-drag selection has no working copy path on
    // Linux/Windows — Ctrl+C is SIGINT, and xterm's selection layer isn't a
    // DOM range the browser copies on its own. Capture phase + the shared
    // abort signal so `dispose()` removes it for free. Ctrl+C is never
    // remapped (see {@link applyTerminalCopy}).
    container.addEventListener(
      "copy",
      // getSelection() returns "" when nothing is selected, which
      // applyTerminalCopy treats as a no-op — no hasSelection() guard needed.
      (e) => applyTerminalCopy(e, this.term.getSelection()),
      { capture: true, signal },
    );

    this.ws.addEventListener(
      "open",
      () => {
        // Send the size first so tmux re-renders at the right
        // dimensions before the user sees the default 80×24 followed
        // by a reflow.
        this.sendResize();
        this.term.focus();
        onState({ kind: "connected" });
      },
      { signal },
    );

    // Throttle activity notifications so rapid output (e.g. `yes`, large
    // `cat`) doesn't re-arm the 1.5 s idle timer on every WS frame.
    let lastActivityTs = 0;
    this.ws.addEventListener(
      "message",
      (ev) => {
        if (ev.data instanceof ArrayBuffer) {
          const bytes = new Uint8Array(ev.data);
          this.term.write(bytes);
          const now = performance.now();
          if (now - lastActivityTs > 300) {
            lastActivityTs = now;
            onActivity?.();
          }
        } else if (typeof ev.data === "string") {
          const capability = parseTerminalOsc52Capability(ev.data);
          if (capability !== null) {
            this.osc52ClipboardEnabled = capability;
          } else {
            const text = parseTerminalClipboardMessage(ev.data);
            if (text !== null) this.requestClipboardWrite(text);
            // Unknown text frames stay ignored for protocol forward compatibility.
          }
        }
      },
      { signal },
    );

    this.ws.addEventListener(
      "close",
      (ev) => {
        onState({ kind: "closed", reason: ev.reason || `code ${ev.code}`, code: ev.code });
      },
      { signal },
    );

    this.ws.addEventListener(
      "error",
      () => {
        onState({ kind: "error" });
      },
      { signal },
    );

    this.dataDispose = this.term.onData((d) => {
      onInput?.();
      // Stamp before the readyState guard so clipboard trust still reflects
      // local input during a momentary WebSocket hiccup.
      this.lastUserInputAt = performance.now();
      if (this.ws.readyState !== WebSocket.OPEN) return;
      this.ws.send(INPUT_ENCODER.encode(d));
    });

    this.term.attachCustomKeyEventHandler((e) => {
      const payload = terminalKeyEventPayload(e);
      if (payload === null) return true;
      // xterm invokes this handler for keydown, keypress, and keyup.
      // Suppress all three so xterm cannot also send a bare Enter; emit
      // the CSI-u sequence once, on keydown.
      if (e.type === "keydown") {
        e.preventDefault();
        onInput?.();
        this.lastUserInputAt = performance.now();
        if (this.ws.readyState === WebSocket.OPEN) {
          this.ws.send(INPUT_ENCODER.encode(payload));
        }
      }
      return false;
    });

    // Replace xterm's lossy wheel→mouse-report conversion (trackpad deltas
    // are damped and capped to one report per event, which reads as
    // "scrolling doesn't work" on macOS trackpads) with the accumulating
    // synthesis in wheelReportPayload. term.input routes the reports
    // through the normal onData path above, so they hit the WS send and
    // the input-activity bookkeeping like any keystroke.
    this.term.attachCustomWheelEventHandler((e) => {
      const result = wheelReportPayload(
        e,
        {
          mouseTrackingMode: this.term.modes.mouseTrackingMode,
          sgrEncoding: this.sgrMouseEncodingActive(),
        },
        this.screenMetrics(),
        this.wheelPartialLines,
      );
      this.wheelPartialLines = result.partial;
      if (!result.consume) return true;
      if (result.data) this.term.input(result.data, true);
      e.preventDefault();
      return false;
    });

    // ResizeObserver fires on any layout-affecting change (window
    // resize, font load, CSS class change). tmux deduplicates same-
    // size events server-side, so no throttle needed here.
    this.resizeObserver = new ResizeObserver(() => this.sendResize());
    this.resizeObserver.observe(container);
  }

  /**
   * Update the terminal's color theme without reconnecting the WebSocket.
   * Safe to call at any point after construction.
   */
  setTheme(isDark: boolean): void {
    this.term.options.theme = terminalTheme(isDark);
  }

  /** Enable clipboard bridging only for the visible, interactive surface. */
  setClipboardEnabled(enabled: boolean): void {
    this.clipboardEnabled = enabled;
  }

  /**
   * Give the terminal keyboard focus. The WS-open handler focuses
   * automatically, but that call is a browser no-op while the surface is
   * hidden (a pre-warmed attach behind the chat view), so the view calls
   * this when the surface is revealed.
   */
  focus(): void {
    this.term.focus();
  }

  /**
   * Update the terminal's code font (size + family) without reconnecting —
   * mirrors {@link setTheme}, mutating options in place. A new glyph size
   * changes the character-cell dimensions, so this re-fits the grid to the
   * container and pushes the resulting cols×rows to tmux via {@link sendResize}
   * (which no-ops the send while the socket is down; the reconnect re-fits on
   * open). An empty family falls back to the shared mono stack. Safe to call at
   * any point after construction.
   */
  setFont(sizePx: number, family: string): void {
    this.term.options.fontFamily = codeFontFamilyForEditor(family);
    this.term.options.fontSize = sizePx;
    this.sendResize();
  }

  /**
   * Tear down the bridge. Order matters: abort listeners FIRST so
   * the cleanup's ``ws.close()`` can't fire a stale ``close``
   * event into the next mount.
   *
   * Idempotent: the view disposes the outgoing session explicitly on
   * every re-dial (React 18 ignores callback-ref cleanups), and a
   * future React upgrade would have the ref cleanup call this again.
   */
  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.listenerCtl.abort();
    this.resizeObserver.disconnect();
    this.dataDispose.dispose();
    this.osc52Dispose.dispose();
    try {
      this.ws.close();
    } catch {
      /* noop */
    }
    // Dispose the WebGL renderer before the terminal so its canvas and
    // GL context are released while the terminal still owns them.
    this.webgl?.dispose();
    this.term.dispose();
  }

  private requestClipboardWrite(text: string): void {
    if (
      !this.clipboardEnabled ||
      !hadRecentTerminalInput(this.lastUserInputAt, performance.now())
    ) {
      return;
    }
    this.onClipboardRequest?.(text);
  }

  /**
   * Whether the pane program requested SGR mouse encoding (``?1006h``).
   *
   * The public ``IModes`` exposes the tracking mode but not the encoding,
   * so this feature-detects xterm's core mouse service. Both tmux with
   * ``mouse on`` (PTY transport) and Claude Code (control transport) request
   * SGR; when the private shape is missing or the encoding is anything else,
   * the wheel handler defers to xterm rather than synthesizing reports the
   * program could not parse.
   */
  private sgrMouseEncodingActive(): boolean {
    // eslint-disable-next-line no-underscore-dangle
    const core = (this.term as unknown as TerminalCore)._core;
    return core?.coreMouseService?.activeEncoding === "SGR";
  }

  /**
   * Measure the character grid for wheel-report placement, or ``null``
   * when layout isn't available (pre-mount, jsdom). Reads the
   * ``.xterm-screen`` element because the outer container includes
   * padding that would skew the per-cell math.
   */
  private screenMetrics(): WheelScreenMetrics | null {
    const { cols, rows } = this.term;
    const rect = this.term.element?.querySelector(".xterm-screen")?.getBoundingClientRect();
    if (!rect || rect.width <= 0 || rect.height <= 0 || cols <= 0 || rows <= 0) return null;
    return {
      left: rect.left,
      top: rect.top,
      cellWidth: rect.width / cols,
      cellHeight: rect.height / rows,
      cols,
      rows,
    };
  }

  private sendResize(): void {
    if (this.ws.readyState !== WebSocket.OPEN) return;
    try {
      this.fit.fit();
    } catch {
      return;
    }
    const { cols, rows } = this.term;
    // Skip a no-op resize: the WS-open handler and the ResizeObserver both
    // call this on mount, and a transient re-fit can land the same size. On
    // the control transport an unchanged size is a wasted round-trip (tmux
    // recomputes layout for the new value regardless), so dedupe here.
    if (this.lastSentSize && this.lastSentSize.cols === cols && this.lastSentSize.rows === rows) {
      return;
    }
    this.lastSentSize = { cols, rows };
    this.ws.send(JSON.stringify({ type: "resize", cols, rows }));
  }
}
