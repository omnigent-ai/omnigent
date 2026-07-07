// Server-side dictation transport: mic → 16 kHz PCM → WS /v1/dictation/stream.
//
// ComposerMicButton uses this as the fallback when the browser Web Speech API
// has no backend (Electron, Firefox/Chromium — see web/electron/README.md).
// One DictationSession is one dictation take: it owns the microphone stream,
// an AudioWorklet that downsamples the capture rate to 16 kHz mono s16le, and
// the WebSocket that streams those frames to the server and receives
// transcript events back. The wire protocol is documented in
// omnigent/server/routes/dictation.py; availability is gated by the
// `dictation_available` capability from GET /v1/info.
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
      return typeof frame.message === "string"
        ? { type: "error", message: frame.message }
        : null;
    default:
      return null;
  }
}

// The server engine may load model weights on the first take, so the ready
// handshake gets a generous timeout; stop just flushes trailing audio.
const READY_TIMEOUT_MS = 10_000;
const STOP_TIMEOUT_MS = 5_000;

const TARGET_RATE = 16_000;

// AudioWorklet processor, inlined as a Blob module so no separate asset has
// to survive the Vite build. Linear-interpolation downsample from the
// context capture rate to 16 kHz, Float32 → Int16, posted in 100 ms chunks.
// (When the context already runs at 16 kHz — we ask for that — the step is
// 1 and the loop degenerates to a plain format conversion.)
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
  private stopResolve: ((tail: string) => void) | null = null;
  private closed = false;

  private constructor(
    events: DictationSessionEvents,
    ws: WebSocket,
    mediaStream: MediaStream,
    audioContext: AudioContext,
  ) {
    this.events = events;
    this.ws = ws;
    this.mediaStream = mediaStream;
    this.audioContext = audioContext;

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
   * denied, the socket fails, or the engine never comes up.
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
      const socket = ws;
      node.port.onmessage = (msg: MessageEvent<Int16Array<ArrayBuffer>>) => {
        if (socket.readyState === WebSocket.OPEN) socket.send(msg.data.buffer);
      };
      // The worklet only renders while it reaches the destination; route
      // it through a muted gain so nothing is audible.
      const mute = audioContext.createGain();
      mute.gain.value = 0;
      source.connect(node);
      node.connect(mute);
      mute.connect(audioContext.destination);
    } catch (error) {
      for (const track of mediaStream.getTracks()) track.stop();
      if (audioContext && audioContext.state !== "closed") void audioContext.close();
      ws?.close();
      throw error;
    }
    return new DictationSession(events, ws, mediaStream, audioContext);
  }

  /**
   * End the take: release the mic immediately, ask the server to flush,
   * and resolve with the flushed tail utterance ("" on timeout/close).
   */
  stop(): Promise<string> {
    this.teardownAudio();
    if (this.closed || this.ws.readyState !== WebSocket.OPEN) {
      this.closed = true;
      return Promise.resolve("");
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

/** Resolve when the server sends its ready frame; reject on close/timeout. */
function waitForReady(ws: WebSocket): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error("dictation server did not become ready"));
    }, READY_TIMEOUT_MS);
    ws.onmessage = (msg) => {
      if (typeof msg.data !== "string") return;
      if (parseDictationEvent(msg.data)?.type === "ready") {
        clearTimeout(timer);
        resolve();
      }
    };
    ws.onerror = () => {
      clearTimeout(timer);
      reject(new Error("dictation connection failed"));
    };
    ws.onclose = () => {
      clearTimeout(timer);
      reject(new Error("dictation connection closed"));
    };
  });
}
