// Unit tests for the terminal session's pure helpers.
//
// The full TerminalSession constructor needs a real xterm + WebSocket
// + DOM container, so it's exercised via manual REPL verification (see
// TerminalView.test.ts). `openTerminalLink` is the one piece of our own
// logic the WebLinksAddon delegates to — the click handler that makes
// terminal URLs clickable — so we pin it here.

import { Terminal } from "@xterm/xterm";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ConnectionState } from "./TerminalSession";
import {
  SHIFT_ENTER_CSI_U,
  SYNC_ECHO_MAX_BYTES,
  SYNC_ECHO_WINDOW_MS,
  TerminalSession,
  WHEEL_REPORTS_MAX_PER_EVENT,
  applyTerminalCopy,
  isUnexpectedTerminalClose,
  loadWebglRenderer,
  openTerminalLink,
  sgrWheelReports,
  shouldEchoSynchronously,
  terminalTheme,
  terminalKeyEventPayload,
  wheelReportPayload,
  type WheelMouseState,
  type WheelScreenMetrics,
} from "./TerminalSession";

describe("openTerminalLink", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("opens the uri in a new tab with noopener,noreferrer", () => {
    // Stub window.open so we observe the call without spawning a tab.
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);
    const event = new MouseEvent("click");

    openTerminalLink(event, "https://example.com/foo");

    // Proves the handler routes the detected URL to a new tab with the
    // hardening flags. If it regressed to navigating in-place, _blank
    // would be missing and the live terminal session would be torn down.
    expect(openSpy).toHaveBeenCalledWith(
      "https://example.com/foo",
      "_blank",
      "noopener,noreferrer",
    );
  });

  it("routes same-origin session links in-place without opening a new tab", () => {
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);
    const pushSpy = vi.spyOn(window.history, "pushState");
    const event = new MouseEvent("click");
    const preventSpy = vi.spyOn(event, "preventDefault");

    openTerminalLink(event, `${window.location.origin}/c/conv_next`);

    expect(preventSpy).toHaveBeenCalledOnce();
    expect(openSpy).not.toHaveBeenCalled();
    expect(pushSpy).toHaveBeenCalledWith(null, "", "/c/conv_next");
  });

  it("does not reopen the current same-origin session link", () => {
    window.history.replaceState(null, "", "/c/conv_current");
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);
    const pushSpy = vi.spyOn(window.history, "pushState");
    const event = new MouseEvent("click");

    openTerminalLink(event, `${window.location.origin}/c/conv_current`);

    expect(openSpy).not.toHaveBeenCalled();
    expect(pushSpy).not.toHaveBeenCalled();
  });

  it("prevents the addon's default in-place navigation", () => {
    vi.spyOn(window, "open").mockReturnValue(null);
    const event = new MouseEvent("click");
    const preventSpy = vi.spyOn(event, "preventDefault");

    openTerminalLink(event, "https://example.com/foo");

    // The WebLinksAddon navigates the current document on click by
    // default; without preventDefault the click would unload the SPA
    // (and kill the WebSocket-attached terminal) before window.open's
    // tab is usable. A failure here means that suppression was dropped.
    expect(preventSpy).toHaveBeenCalledOnce();
  });
});

describe("applyTerminalCopy", () => {
  function copyEvent() {
    const setData = vi.fn();
    const preventDefault = vi.fn();
    const event: Pick<ClipboardEvent, "clipboardData" | "preventDefault"> = {
      clipboardData: { setData } as unknown as DataTransfer,
      preventDefault,
    };
    return { event, setData, preventDefault };
  }

  it("writes the selection to the clipboard and prevents default", () => {
    const { event, setData, preventDefault } = copyEvent();

    // A real selection must be placed on the clipboard as text/plain and
    // the browser's default (per-visual-row) copy suppressed, so a
    // soft-wrapped paragraph pastes as the single logical line that
    // getSelection() already reflowed.
    expect(applyTerminalCopy(event, "selected text")).toBe(true);
    expect(setData).toHaveBeenCalledWith("text/plain", "selected text");
    expect(preventDefault).toHaveBeenCalledOnce();
  });

  it("does nothing when there is no selection", () => {
    const { event, setData, preventDefault } = copyEvent();

    // With no selection the event must be left untouched so the browser's
    // default copy behavior still applies (and we never clobber the
    // clipboard with an empty string).
    expect(applyTerminalCopy(event, "")).toBe(false);
    expect(setData).not.toHaveBeenCalled();
    expect(preventDefault).not.toHaveBeenCalled();
  });
});

describe("shouldEchoSynchronously", () => {
  it("takes the sync path for a small chunk right after a keystroke", () => {
    // Echo/prompt-sized chunk arriving well within the window: paint it
    // synchronously so the keystroke echo lands without a queued-write
    // frame of latency.
    expect(shouldEchoSynchronously(64, 10)).toBe(true);
  });

  it("stays async when the user hasn't typed recently", () => {
    // Past the window, this is unsolicited output (an agent printing),
    // not an echo — the async write queue is correct.
    expect(shouldEchoSynchronously(64, SYNC_ECHO_WINDOW_MS)).toBe(false);
    expect(shouldEchoSynchronously(64, SYNC_ECHO_WINDOW_MS + 1)).toBe(false);
  });

  it("stays async for large chunks even right after a keystroke", () => {
    // A big chunk is a flood/redraw, not an echo; keeping it on the async
    // path stops one giant synchronous write from blocking the main
    // thread mid-type.
    expect(shouldEchoSynchronously(SYNC_ECHO_MAX_BYTES + 1, 10)).toBe(false);
    expect(shouldEchoSynchronously(SYNC_ECHO_MAX_BYTES, 10)).toBe(true);
  });
});

describe("loadWebglRenderer", () => {
  it("returns null without throwing when WebGL is unavailable", () => {
    // jsdom has no WebGL context (getContext() is unimplemented), so this
    // exercises the exact degraded environment the fallback exists for:
    // headless CI, a blocklisted GPU, or a browser with WebGL disabled.
    // The function must swallow the failure and return null so the caller
    // keeps the working DOM renderer — a throw here would crash terminal
    // construction and leave the user with no terminal at all.
    const term = new Terminal();
    const container = document.createElement("div");
    term.open(container);

    expect(loadWebglRenderer(term)).toBeNull();

    term.dispose();
  });
});

describe("terminalTheme", () => {
  it("uses a light ANSI bright-black in light mode", () => {
    const theme = terminalTheme(false);

    // Codex paints its prompt/input band with ANSI gray. In the web light
    // theme that gray must be a pale surface so dark prompt text remains
    // readable.
    expect(theme.background).toBe("#ffffff");
    expect(theme.foreground).toBe("#18181b");
    expect(theme.brightBlack).toBe("#e4e4e7");

    // CLIs that assume a dark terminal paint primary text with ANSI
    // white / bright-white. On the white card background those slots must
    // be dark, or the text renders white-on-white and disappears.
    expect(theme.white).toBe("#3f3f46");
    expect(theme.brightWhite).toBe("#18181b");
  });

  it("keeps dark mode terminal surfaces dark", () => {
    const theme = terminalTheme(true);

    // Dark mode should retain the terminal-like contrast the rest of the
    // app expects rather than inheriting the light prompt-band treatment.
    expect(theme.background).toBe("#131517");
    expect(theme.foreground).toBe("#e4e4e7");
    expect(theme.brightBlack).toBe("#71717a");
  });
});

describe("terminalKeyEventPayload", () => {
  function keyEvent(init: KeyboardEventInit): KeyboardEvent {
    return new KeyboardEvent("keydown", init);
  }

  it("encodes Shift+Enter as Kitty CSI-u", () => {
    const payload = terminalKeyEventPayload(keyEvent({ key: "Enter", shiftKey: true }));

    // This is the byte sequence prompt-toolkit maps to F20, which the
    // REPL binds to "insert newline". Returning "\x1b\r" here would be
    // the old Alt+Enter fallback, not Kitty/CSI-u support.
    expect(payload).toBe(SHIFT_ENTER_CSI_U);
    expect(payload).toBe("\x1b[13;2u");
  });

  it("leaves plain Enter on xterm's default path", () => {
    expect(terminalKeyEventPayload(keyEvent({ key: "Enter" }))).toBeNull();
  });

  it("does not override other modified Enter combinations", () => {
    expect(terminalKeyEventPayload(keyEvent({ key: "Enter", altKey: true }))).toBeNull();
    expect(terminalKeyEventPayload(keyEvent({ key: "Enter", ctrlKey: true }))).toBeNull();
    expect(terminalKeyEventPayload(keyEvent({ key: "Enter", metaKey: true }))).toBeNull();
    expect(
      terminalKeyEventPayload(keyEvent({ key: "Enter", shiftKey: true, altKey: true })),
    ).toBeNull();
  });
});

describe("isUnexpectedTerminalClose", () => {
  it("treats transport-shaped close codes as reconnectable", () => {
    // WHY: 1001/1006/1012/1013 happen TO the connection (proxy restart, dead
    // TCP on tab thaw, service restart) rather than being a deliberate end, so
    // a reconnect is appropriate.
    expect(isUnexpectedTerminalClose(1001)).toBe(true);
    expect(isUnexpectedTerminalClose(1006)).toBe(true);
    expect(isUnexpectedTerminalClose(1012)).toBe(true);
    expect(isUnexpectedTerminalClose(1013)).toBe(true);
  });

  it("treats deliberate closes (normal, policy, app 4xxx) as terminal", () => {
    // WHY: 1000 normal, 1008 policy, and the app's 4xxx codes mean the server
    // decided the attach should end — reconnecting would loop or resurrect a
    // terminal the user intentionally left.
    expect(isUnexpectedTerminalClose(1000)).toBe(false);
    expect(isUnexpectedTerminalClose(1008)).toBe(false);
    expect(isUnexpectedTerminalClose(4404)).toBe(false);
    expect(isUnexpectedTerminalClose(4405)).toBe(false);
    expect(isUnexpectedTerminalClose(4500)).toBe(false);
  });
});

describe("sgrWheelReports", () => {
  it("encodes wheel-up as button 64 and wheel-down as 65, one report per line", () => {
    expect(sgrWheelReports(-2, 5, 7)).toBe("\x1b[<64;5;7M\x1b[<64;5;7M");
    expect(sgrWheelReports(1, 1, 1)).toBe("\x1b[<65;1;1M");
  });

  it("emits nothing for zero lines", () => {
    expect(sgrWheelReports(0, 5, 7)).toBe("");
  });
});

describe("wheelReportPayload", () => {
  const sgrModes: WheelMouseState = { mouseTrackingMode: "vt200", sgrEncoding: true };
  const screen: WheelScreenMetrics = {
    left: 0,
    top: 0,
    cellWidth: 8,
    cellHeight: 16,
    cols: 80,
    rows: 24,
  };

  /** Count occurrences of an SGR report prefix without a control-char regex. */
  function countReports(data: string, prefix: string): number {
    return data.split(prefix).length - 1;
  }

  function wheelEvent(
    deltaY: number,
    over: Partial<Pick<WheelEvent, "deltaMode" | "shiftKey" | "clientX" | "clientY">> = {},
  ) {
    return {
      deltaY,
      deltaMode: WheelEvent.DOM_DELTA_PIXEL,
      shiftKey: false,
      clientX: 100,
      clientY: 100,
      ...over,
    };
  }

  it("defers to xterm and resets the carry when mouse tracking is off", () => {
    // WHY: with no app tracking (a plain shell on the control transport) the
    // wheel must scroll xterm's native scrollback, and a stale fraction from a
    // previous tracking-on scroll must not leak into the next one.
    const result = wheelReportPayload(
      wheelEvent(-40),
      { mouseTrackingMode: "none", sgrEncoding: true },
      screen,
      0.9,
    );
    expect(result).toEqual({ consume: false, data: "", partial: 0 });
  });

  it("defers to xterm when the program did not request SGR encoding", () => {
    // WHY: synthesized reports are SGR-formatted; a program tracking the
    // mouse with the legacy default encoding could not parse them.
    const result = wheelReportPayload(
      wheelEvent(-40),
      { mouseTrackingMode: "vt200", sgrEncoding: false },
      screen,
      0,
    );
    expect(result.consume).toBe(false);
  });

  it("defers on shift-wheel, zero delta, and unmeasurable layout, keeping the carry", () => {
    // WHY: shift-wheel mirrors xterm's built-in escape hatch; deltaY 0 is a
    // horizontal-only tick; null screen means layout isn't measurable yet. None
    // of these should destroy accumulated fractional scroll.
    for (const [ev, scr] of [
      [wheelEvent(-40, { shiftKey: true }), screen],
      [wheelEvent(0), screen],
      [wheelEvent(-40), null],
    ] as const) {
      expect(wheelReportPayload(ev, sgrModes, scr, 0.4)).toEqual({
        consume: false,
        data: "",
        partial: 0.4,
      });
    }
  });

  it("accumulates small trackpad deltas across events into whole-line reports", () => {
    // WHY: this is the macOS-trackpad regression this helper exists for —
    // xterm's own conversion damps sub-50px deltas to nearly nothing. Ten 4px
    // ticks over a 16px cell are 2.5 lines and must yield exactly 2 reports,
    // with the remaining half line carried, and every event consumed so
    // xterm's damped path never double-fires.
    let partial = 0;
    let reports = "";
    for (let i = 0; i < 10; i++) {
      const result = wheelReportPayload(wheelEvent(4), sgrModes, screen, partial);
      expect(result.consume).toBe(true);
      partial = result.partial;
      reports += result.data;
    }
    expect(countReports(reports, "\x1b[<65")).toBe(2);
    expect(partial).toBeCloseTo(0.5);
  });

  it("converts a discrete wheel notch to one report per whole line, carrying the rest", () => {
    const result = wheelReportPayload(wheelEvent(-120), sgrModes, screen, 0);
    expect(countReports(result.data, "\x1b[<64")).toBe(7); // 120/16 = 7.5
    expect(result.partial).toBeCloseTo(-0.5);
  });

  it("honors line and page delta modes", () => {
    const line = wheelReportPayload(
      wheelEvent(3, { deltaMode: WheelEvent.DOM_DELTA_LINE }),
      sgrModes,
      screen,
      0,
    );
    expect(countReports(line.data, "\x1b[<65")).toBe(3);

    const page = wheelReportPayload(
      wheelEvent(1, { deltaMode: WheelEvent.DOM_DELTA_PAGE }),
      sgrModes,
      screen,
      0,
    );
    expect(countReports(page.data, "\x1b[<65")).toBe(screen.rows);
  });

  it("caps the reports for one event and discards the excess", () => {
    // WHY: the cap bounds the input burst; discarding (not banking) the excess
    // keeps a pathological delta from continuing to scroll long after the
    // gesture ended.
    const result = wheelReportPayload(wheelEvent(16 * 1000), sgrModes, screen, 0);
    expect(countReports(result.data, "\x1b[<65")).toBe(WHEEL_REPORTS_MAX_PER_EVENT);
    expect(result.partial).toBe(0);
  });

  it("places the report at the pointer's cell, clamped to the grid", () => {
    // clientX 100 / 8px = col 13 (1-based); clientY 100 / 16px = row 7.
    const at = wheelReportPayload(wheelEvent(16), sgrModes, screen, 0);
    expect(at.data).toBe("\x1b[<65;13;7M");

    // Pointer outside the grid clamps to the edges instead of emitting
    // coordinates tmux/the app would reject.
    const clamped = wheelReportPayload(
      wheelEvent(16, { clientX: -50, clientY: 99999 }),
      sgrModes,
      screen,
      0,
    );
    expect(clamped.data).toBe("\x1b[<65;1;24M");
  });
});

// ---------------------------------------------------------------------------
// TerminalSession class — wired up against a fake WebSocket + ResizeObserver.
// The real xterm Terminal runs (it already does in jsdom for loadWebglRenderer
// above), but the WebSocket and ResizeObserver globals are stubbed so the
// constructor can complete and we can drive its event handlers directly.
// ---------------------------------------------------------------------------

class FakeWebSocket {
  static OPEN = 1;
  static CLOSED = 3;
  static instances: FakeWebSocket[] = [];
  readyState = 0;
  binaryType = "blob";
  sent: Array<string | Uint8Array> = [];
  closed = false;
  private listeners: Record<string, Array<(ev: unknown) => void>> = {};
  url: string;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  addEventListener(type: string, fn: (ev: unknown) => void) {
    (this.listeners[type] ??= []).push(fn);
  }

  send(data: string | Uint8Array) {
    this.sent.push(data);
  }

  close() {
    this.closed = true;
    this.readyState = FakeWebSocket.CLOSED;
  }

  // Test helpers to drive the handlers the session registers.
  emit(type: string, ev: unknown) {
    for (const fn of this.listeners[type] ?? []) fn(ev);
  }
  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.emit("open", {});
  }
}

class FakeResizeObserver {
  static instances: FakeResizeObserver[] = [];
  disconnected = false;
  observed: Element[] = [];
  cb: () => void;
  constructor(cb: () => void) {
    this.cb = cb;
    FakeResizeObserver.instances.push(this);
  }
  observe(el: Element) {
    this.observed.push(el);
  }
  disconnect() {
    this.disconnected = true;
  }
}

describe("TerminalSession", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    FakeResizeObserver.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket);
    vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function makeSession(onActivity?: () => void, onInput?: () => void) {
    const states: ConnectionState[] = [];
    const container = document.createElement("div");
    document.body.appendChild(container);
    const session = new TerminalSession(
      container,
      "ws://localhost/attach",
      (s) => states.push(s),
      false,
      onActivity,
      onInput,
    );
    return { session, states, container, socket: FakeWebSocket.instances.at(-1)! };
  }

  it("reports 'connected' and sends an initial resize on socket open", () => {
    // WHY: the open handler must push a resize frame before the user sees the
    // default 80x24, then surface kind:"connected" to React. readyState is
    // OPEN by the time sendResize runs, so a JSON resize control frame is sent.
    const { states, socket, session } = makeSession();

    socket.open();

    expect(states.at(-1)).toEqual({ kind: "connected" });
    const resizeFrame = socket.sent.find(
      (m) => typeof m === "string" && m.includes('"type":"resize"'),
    );
    expect(resizeFrame).toBeDefined();
    session.dispose();
  });

  it("does not re-send a resize when the fitted size is unchanged", () => {
    // WHY: the WS-open handler and the ResizeObserver both drive sendResize on
    // mount, and jsdom's fit() yields a stable size, so without deduping the
    // control transport would receive a redundant refresh-client -C. Drive the
    // observer callback (the real re-fit path) after open and assert exactly
    // one resize frame total.
    const { socket, session } = makeSession();
    const observer = FakeResizeObserver.instances[0];

    socket.open(); // first (and only distinct) resize
    observer.cb(); // same size → must be deduped
    observer.cb();

    const resizeFrames = socket.sent.filter(
      (m) => typeof m === "string" && m.includes('"type":"resize"'),
    );
    expect(resizeFrames).toHaveLength(1);
    session.dispose();
  });

  it("surfaces close code + reason and error transitions", () => {
    // WHY: the closed variant carries the WS code so consumers can tell a
    // deliberate close from a transport drop; the error handler maps to
    // kind:"error".
    const { states, socket, session } = makeSession();

    socket.emit("close", { reason: "", code: 1006 });
    expect(states.at(-1)).toEqual({ kind: "closed", reason: "code 1006", code: 1006 });

    socket.emit("error", {});
    expect(states.at(-1)).toEqual({ kind: "error" });
    session.dispose();
  });

  it("writes inbound binary frames to the terminal and fires onActivity", () => {
    // WHY: ArrayBuffer message frames are raw PTY bytes — they must reach the
    // terminal and trigger the best-effort activity signal. The throttle keys
    // off performance.now(), so pin it past the 300ms window to make the first
    // notification deterministic. Non-ArrayBuffer (text) frames are ignored so
    // they aren't painted as output.
    vi.spyOn(performance, "now").mockReturnValue(10_000);
    const onActivity = vi.fn();
    const { socket, session } = makeSession(onActivity);

    // Build the buffer from the global ArrayBuffer the source's
    // `instanceof ArrayBuffer` check sees — a TextEncoder's buffer comes from
    // Node's realm and fails that check under jsdom.
    const data = new ArrayBuffer(5);
    new Uint8Array(data).set([104, 101, 108, 108, 111]); // "hello"
    socket.emit("message", { data });
    expect(onActivity).toHaveBeenCalledTimes(1);

    socket.emit("message", { data: "text frame" });
    expect(onActivity).toHaveBeenCalledTimes(1); // unchanged — text ignored
    session.dispose();
  });

  it("setTheme swaps the terminal theme without reconnecting", () => {
    // WHY: theme changes must not tear down the live WebSocket; the socket
    // stays the same instance after setTheme(true).
    const { socket, session } = makeSession();
    const before = socket;
    session.setTheme(true);
    expect(socket).toBe(before);
    expect(socket.closed).toBe(false);
    session.dispose();
  });

  it("setFont re-fonts + refits in place, tolerating a down socket, no reconnect", () => {
    // WHY: a code-font change (Settings → Appearance) must re-font the LIVE
    // terminal — mutating options in place like setTheme, never tearing down the
    // WebSocket (xterm is a fixed-pixel widget that can't follow a CSS variable).
    const { socket, session } = makeSession();
    const { term } = session as unknown as { term: Terminal };

    // Socket-down (pre-open): setFont still applies the size and must not throw
    // or send — sendResize no-ops until the WS opens, and the reconnect re-fits.
    session.setFont(16, "");
    expect(term.options.fontSize).toBe(16);
    expect(socket.sent).toHaveLength(0);

    // Once open, setFont refits the grid (sendResize) so the new glyph cell size
    // reflows cols×rows, and applies a custom family with the mono fallback
    // appended (an uninstalled name degrades to mono, not a serif).
    socket.open();
    const before = socket;
    const sendResize = vi.spyOn(session as unknown as { sendResize: () => void }, "sendResize");
    session.setFont(18, "Fira Code");
    expect(sendResize).toHaveBeenCalledTimes(1);
    expect(term.options.fontSize).toBe(18);
    expect(term.options.fontFamily).toContain("Fira Code");
    // Same socket instance, still open — a re-font never reconnects.
    expect(socket).toBe(before);
    expect(socket.closed).toBe(false);
    session.dispose();
  });

  it("dispose is idempotent and tears down observer + socket once", () => {
    // WHY: the view disposes explicitly on every re-dial and a future React
    // upgrade would call the ref cleanup again — a second dispose must be a
    // safe no-op, not a double close.
    const { socket, session } = makeSession();
    const observer = FakeResizeObserver.instances[0];

    session.dispose();
    expect(socket.closed).toBe(true);
    expect(observer.disconnected).toBe(true);

    // Second call: no throw, socket already closed.
    socket.closed = false; // prove the second close() isn't invoked
    session.dispose();
    expect(socket.closed).toBe(false);
  });

  it("observes the container for resize", () => {
    // WHY: layout changes (window resize, font load) must propagate a resize
    // frame, so the session must register a ResizeObserver on its container.
    const { container, session } = makeSession();
    const observer = FakeResizeObserver.instances[0];
    expect(observer.observed).toContain(container);
    session.dispose();
  });
});
