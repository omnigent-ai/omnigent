// Server-side dictation transport: mic → 16 kHz PCM → WS /v1/dictation/stream.
//
// ComposerMicButton uses this as the fallback when the browser Web Speech API
// has no working backend (Electron, Firefox/Chromium — see
// web/electron/README.md). One DictationSession is one dictation take: it
// owns the microphone stream, an AudioWorklet that downsamples the capture
// rate to 16 kHz mono s16le, and the WebSocket that streams those frames to
// the server and receives transcript events back. The wire protocol is
// documented in omnigent/server/routes/dictation.py; availability is gated
// by the `dictation_available` capability from GET /v1/info.
//
// The WebSocket URL rides the host seam (`resolveWebSocketUrl`) exactly like
// the terminal-attach and session-updates sockets, so embed hosts and the
// Vite dev proxy keep working. Identity rides the ingress/dev proxy on the
// handshake, as with those sockets.

import { resolveWebSocketUrl } from "@/lib/host";

/** A transcript event pushed by the server over the dictation stream. */
export type DictationEvent =
  | { type: "ready" }
  | { type: "partial"; text: string }
  | { type: "final"; text: string }
  | { type: "stopped"; text: string }
  | { type: "error"; message: string };

export type DictationSessionEvents = {
  /** Revisable in-progress utterance (server-throttled to ~6 Hz). */
  onPartial: (text: string) => void;
  /** An utterance completed by a pause; append it and clear the partial. */
  onFinal: (text: string) => void;
  /** Fatal error after start. The session has already cleaned itself up. */
  onError: (message: string) => void;
};

/**
 * The server was reachable but at its concurrent-take cap (WS close 1013).
 * Transient by definition — callers should message "busy, try again",
 * not "unavailable".
 */
export class DictationBusyError extends Error {}

/**
 * Parse one text frame from the dictation socket into a typed event.
 * Returns null for frames that don't match the protocol (ignored for
 * forward compatibility, mirroring the server's posture on control
 * messages it doesn't know).
 */
export function parseDictationEvent(raw: string): DictationEvent | null {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof data !== "object" || data === null) return null;
  const frame = data as { type?: unknown; text?: unknown; message?: unknown };
  switch (frame.type) {
    case "ready":
      return { type: "ready" };
    case "partial":
    case "final":
    case "stopped":
      return typeof frame.text === "string" ? { type: frame.type, text: frame.text } : null;
    case "error":
      return typeof frame.message === "string" ? { type: "error", message: frame.message } : null;
    default:
      return null;
  }
}

// Client budgets must exceed the server's own worst cases or takes fail
// spuriously right when they'd have succeeded:
// - ready: engine construction loads model weights on the first take, and
//   the remote-relay path allows the worker 30 s for its own cold load
//   (_REMOTE_READY_TIMEOUT_S in omnigent/server/dictation.py).
// - stop: the relay waits up to 10 s (_REMOTE_STOP_TIMEOUT_S) for the
//   worker to flush the tail; resolving earlier would drop the user's
//   last words even though they were transcribed moments later.
const READY_TIMEOUT_MS = 40_000;
const STOP_TIMEOUT_MS = 15_000;

// How long stop() waits for the worklet to post its final partial chunk
// before tearing the audio graph down. Message-port turnaround is
// milliseconds; this is only a stuck-worklet backstop.
const FLUSH_TIMEOUT_MS = 250;

/** WS close code the server sends when at its concurrent-take cap. */
const WS_CLOSE_TRY_AGAIN_LATER = 1013;

const TARGET_RATE = 16_000;

// AudioWorklet processor, inlined as a Blob module so no separate asset has
// to survive the Vite build. Linear-interpolation downsample from the
// context capture rate to 16 kHz, Float32 → Int16, posted in 100 ms chunks.
// (When the context already runs at 16 kHz — we ask for that — the step is
// 1 and the loop degenerates to a plain format conversion.)
//
// Any message on the port means "flush": the partially-filled chunk is
// posted, then a null marker — so stop() can capture trailing speech that
// hasn't crossed the 100 ms boundary before tearing the graph down.
const WORKLET_SOURCE = `
const TARGET_RATE = ${TARGET_RATE};
const CHUNK_SAMPLES = TARGET_RATE / 10; // 100 ms per posted chunk
class Pcm16Downsampler extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / TARGET_RATE;
    this.pos = 0; // fractional read position, carried across blocks
    this.pending = new Int16Array(CHUNK_SAMPLES);
    this.filled = 0;
    this.port.onmessage = () => {
      if (this.filled > 0) {
        const out = this.pending.slice(0, this.filled);
        this.filled = 0;
        this.port.postMessage(out, [out.buffer]);
      }
      this.port.postMessage(null);
    };
  }
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel || channel.length === 0) return true;
    let pos = this.pos;
    while (pos < channel.length) {
      const i = Math.floor(pos);
      const s0 = channel[i];
      const s1 = i + 1 < channel.length ? channel[i + 1] : s0;
      const sample = s0 + (s1 - s0) * (pos - i);
      const clamped = Math.max(-1, Math.min(1, sample));
      this.pending[this.filled++] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
      if (this.filled === CHUNK_SAMPLES) {
        const out = this.pending;
        this.pending = new Int16Array(CHUNK_SAMPLES);
        this.filled = 0;
        this.port.postMessage(out, [out.buffer]);
      }
      pos += this.step;
    }
    this.pos = pos - channel.length;
    return true;
  }
}
registerProcessor("omnigent-pcm16-downsampler", Pcm16Downsampler);
`;

let _workletUrl: string | null = null;

function workletUrl(): string {
  if (_workletUrl === null) {
    _workletUrl = URL.createObjectURL(
      new Blob([WORKLET_SOURCE], { type: "application/javascript" }),
    );
  }
  return _workletUrl;
}

/**
 * One live dictation take against the server recognizer.
 *
 * Construct via {@link DictationSession.start}, which resolves once the
 * mic, audio graph, and socket handshake are all up — so a resolved
 * session is guaranteed to be streaming. End it with {@link stop} (flushes
 * the tail utterance) or {@link cancel} (immediate teardown, e.g. unmount).
 */
export class DictationSession {
  private readonly events: DictationSessionEvents;
  private readonly ws: WebSocket;
  private readonly mediaStream: MediaStream;
  private readonly audioContext: AudioContext;
  private readonly workletNode: AudioWorkletNode;
  private stopResolve: ((tail: string) => void) | null = null;
  private flushResolve: (() => void) | null = null;
  private closed = false;

  private constructor(
    events: DictationSessionEvents,
    ws: WebSocket,
    mediaStream: MediaStream,
    audioContext: AudioContext,
    workletNode: AudioWorkletNode,
  ) {
    this.events = events;
    this.ws = ws;
    this.mediaStream = mediaStream;
    this.audioContext = audioContext;
    this.workletNode = workletNode;

    workletNode.port.onmessage = (msg: MessageEvent<Int16Array<ArrayBuffer> | null>) => {
      if (msg.data === null) {
        // Flush marker: the worklet has posted everything it had.
        this.flushResolve?.();
        this.flushResolve = null;
        return;
      }
      if (ws.readyState === WebSocket.OPEN) ws.send(msg.data.buffer);
    };
    ws.onmessage = (msg) => {
      if (typeof msg.data !== "string") return;
      const event = parseDictationEvent(msg.data);
      if (event === null) return;
      if (event.type === "partial") this.events.onPartial(event.text);
      else if (event.type === "final") this.events.onFinal(event.text);
      else if (event.type === "stopped") this.resolveStop(event.text);
      else if (event.type === "error") this.fail(event.message);
    };
    ws.onclose = () => {
      // A close during stop() is the normal end of a take; any other
      // close means the server went away mid-dictation.
      if (this.stopResolve !== null) this.resolveStop("");
      else if (!this.closed) this.fail("Dictation connection closed");
    };
  }

  /**
   * Acquire the mic, open the socket, and wait for the server's ready
   * handshake. Rejects (with everything torn down) when the mic is
   * denied, the socket fails, the server is at capacity
   * ({@link DictationBusyError}), or the engine never comes up.
   */
  static async start(events: DictationSessionEvents): Promise<DictationSession> {
    const mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });

    let ws: WebSocket | null = null;
    let audioContext: AudioContext | null = null;
    try {
      ws = new WebSocket(resolveWebSocketUrl("/v1/dictation/stream"));
      ws.binaryType = "arraybuffer";
      await waitForReady(ws);
      // Detect a close during the async audio-graph setup below: the
      // handler-swap in the constructor would otherwise never see it and
      // start() would resolve a dead session that silently drops audio.
      let closedDuringSetup = false;
      const markClosed = () => {
        closedDuringSetup = true;
      };
      ws.addEventListener("close", markClosed);

      // Ask for the target rate directly — Chrome/Firefox resample the
      // capture for us and the worklet's downsampler becomes a no-op.
      // Some platforms reject the hint; the worklet handles any rate.
      try {
        audioContext = new AudioContext({ sampleRate: TARGET_RATE });
      } catch {
        audioContext = new AudioContext();
      }
      await audioContext.audioWorklet.addModule(workletUrl());
      const source = audioContext.createMediaStreamSource(mediaStream);
      const node = new AudioWorkletNode(audioContext, "omnigent-pcm16-downsampler");
      // The worklet only renders while it reaches the destination; route
      // it through a muted gain so nothing is audible.
      const mute = audioContext.createGain();
      mute.gain.value = 0;
      source.connect(node);
      node.connect(mute);
      mute.connect(audioContext.destination);

      ws.removeEventListener("close", markClosed);
      if (closedDuringSetup || ws.readyState !== WebSocket.OPEN) {
        throw new Error("dictation connection closed during setup");
      }
      return new DictationSession(events, ws, mediaStream, audioContext, node);
    } catch (error) {
      for (const track of mediaStream.getTracks()) track.stop();
      if (audioContext && audioContext.state !== "closed") void audioContext.close();
      ws?.close();
      throw error;
    }
  }

  /**
   * End the take: flush the worklet's trailing samples (so the last words
   * make it to the recognizer), release the mic, ask the server to flush,
   * and resolve with the flushed tail utterance ("" on timeout/close).
   */
  async stop(): Promise<string> {
    if (this.closed) return "";
    await this.flushWorklet();
    this.teardownAudio();
    if (this.ws.readyState !== WebSocket.OPEN) {
      this.closed = true;
      return "";
    }
    return new Promise<string>((resolve) => {
      this.stopResolve = resolve;
      this.ws.send(JSON.stringify({ type: "stop" }));
      setTimeout(() => this.resolveStop(""), STOP_TIMEOUT_MS);
    });
  }

  /** Immediate teardown without waiting for the tail (unmount, disable). */
  cancel(): void {
    this.teardownAudio();
    this.closed = true;
    this.ws.close();
  }

  /** Ask the worklet to post its partial chunk; wait for its marker. */
  private flushWorklet(): Promise<void> {
    return new Promise<void>((resolve) => {
      this.flushResolve = resolve;
      try {
        this.workletNode.port.postMessage("flush");
      } catch {
        resolve();
        return;
      }
      setTimeout(resolve, FLUSH_TIMEOUT_MS);
    });
  }

  private resolveStop(tail: string): void {
    this.closed = true;
    const resolve = this.stopResolve;
    this.stopResolve = null;
    this.ws.close();
    resolve?.(tail);
  }

  private fail(message: string): void {
    this.closed = true;
    this.teardownAudio();
    this.ws.close();
    this.events.onError(message);
  }

  private teardownAudio(): void {
    for (const track of this.mediaStream.getTracks()) track.stop();
    if (this.audioContext.state !== "closed") void this.audioContext.close();
  }
}

/**
 * Resolve when the server sends its ready frame; reject on error frame,
 * close (typed {@link DictationBusyError} for the 1013 at-capacity close),
 * or timeout.
 */
function waitForReady(ws: WebSocket): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error("dictation server did not become ready"));
    }, READY_TIMEOUT_MS);
    ws.onmessage = (msg) => {
      if (typeof msg.data !== "string") return;
      const event = parseDictationEvent(msg.data);
      if (event?.type === "ready") {
        clearTimeout(timer);
        resolve();
      } else if (event?.type === "error") {
        // The engine failed to initialize; surface its message rather
        // than the generic close that follows.
        clearTimeout(timer);
        reject(new Error(event.message));
      }
    };
    ws.onerror = () => {
      clearTimeout(timer);
      reject(new Error("dictation connection failed"));
    };
    ws.onclose = (event) => {
      clearTimeout(timer);
      reject(
        event.code === WS_CLOSE_TRY_AGAIN_LATER
          ? new DictationBusyError("dictation is at capacity")
          : new Error("dictation connection closed"),
      );
    };
  });
}
