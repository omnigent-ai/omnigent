"use client";

import { Button } from "@/components/ui/button";
import { useVoiceDictationHotkey } from "@/hooks/useVoiceDictationHotkey";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { DictationBusyError, DictationSession } from "@/lib/dictation";
import { isElectronShell } from "@/lib/nativeBridge";
import { cn } from "@/lib/utils";
import { MicIcon, SquareIcon } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

// Local-only types; speech-input.tsx already augments Window globally.
interface SpeechRecognitionLike {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start(): void;
  stop(): void;
  addEventListener(type: string, listener: (event: Event) => void): void;
  removeEventListener(type: string, listener: (event: Event) => void): void;
}

interface SpeechRecognitionEventLike extends Event {
  results: {
    readonly length: number;
    [index: number]: {
      readonly length: number;
      [index: number]: { transcript: string };
      isFinal: boolean;
    };
  };
  resultIndex: number;
}

interface SpeechRecognitionErrorEventLike extends Event {
  error: string;
}

type SpeechRecognitionCtor = new () => SpeechRecognitionLike;

const getRecognitionCtor = (): SpeechRecognitionCtor | null => {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    SpeechRecognition?: SpeechRecognitionCtor;
    webkitSpeechRecognition?: SpeechRecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
};

// FFT bin ranges per bar, weighted toward voice frequencies (~100Hz–3kHz).
const BAR_BINS: ReadonlyArray<readonly [number, number]> = [
  [1, 3],
  [3, 6],
  [6, 10],
  [10, 16],
];

const BAR_BASELINE = 0.2;

export type ComposerMicButtonProps = {
  onTranscript: (text: string) => void;
  /**
   * Streaming partial transcripts (server dictation only): called with the
   * revisable in-progress utterance as it forms, and with "" when the take
   * ends without finalizing it. Utterances that do finalize arrive via
   * onTranscript, which supersedes the pending interim. When absent, the
   * server path still works but only finals are inserted — the same
   * behavior the Web Speech path has always had.
   */
  onInterim?: (text: string) => void;
  disabled?: boolean;
  lang?: string;
  /** Bind the global ⌘⌥V dictation hotkey to this mic. Enable on exactly one
   *  mounted mic (the primary composer) so two don't fight for the device. */
  enableHotkey?: boolean;
  /** Fired when dictation begins. The parent should snapshot the composer text
   *  here so {@link onVoiceDiscard} can revert to it. */
  onVoiceStart?: () => void;
  /** Fired when Esc ends dictation. The parent should restore the text it
   *  snapshotted in {@link onVoiceStart}, discarding what was dictated. */
  onVoiceDiscard?: () => void;
};

/** getUserMedia permission failures, distinct from transport failures. */
const isPermissionError = (error: unknown): boolean =>
  error instanceof DOMException &&
  (error.name === "NotAllowedError" || error.name === "SecurityError");

export const ComposerMicButton = ({
  onTranscript,
  onInterim,
  disabled,
  lang = "en-US",
  enableHotkey = false,
  onVoiceStart,
  onVoiceDiscard,
}: ComposerMicButtonProps) => {
  // Web Speech is primary whenever the browser has the constructor
  // (Chrome/Safari, unchanged behavior); with no constructor at all
  // (Firefox) takes use server dictation when GET /v1/info advertises it.
  // A constructor is no guarantee of a backend — Electron and plain
  // Chromium error at runtime with "network" — so a failed Web Speech
  // take falls back to the server per take (see handleError). Per-take,
  // not sticky: a transient blip in real Chrome must not permanently
  // downgrade the page to the local model.
  const [Ctor] = useState(getRecognitionCtor);
  const serverInfo = useServerInfo();
  const serverAvailable = serverInfo !== "loading" && serverInfo.dictation_available;
  // Mirrored into a ref so the mount-time recognition handlers (closed
  // over [Ctor, lang]) see the current probe result.
  const serverAvailableRef = useRef(serverAvailable);
  serverAvailableRef.current = serverAvailable;
  const [isListening, setIsListening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const sessionRef = useRef<DictationSession | null>(null);
  // Refs so handlers aren't re-attached on every parent re-render.
  const onTranscriptRef = useRef(onTranscript);
  onTranscriptRef.current = onTranscript;
  const onInterimRef = useRef(onInterim);
  onInterimRef.current = onInterim;
  // Synced refs so the mount-time listeners call the latest callbacks.
  const onVoiceStartRef = useRef(onVoiceStart);
  onVoiceStartRef.current = onVoiceStart;
  const onVoiceDiscardRef = useRef(onVoiceDiscard);
  onVoiceDiscardRef.current = onVoiceDiscard;
  // Set by the Esc handler so late results after a discard don't repopulate the
  // composer the parent just reverted. Cleared on the next start.
  const discardingRef = useRef(false);
  // Synced prop ref so the recognition result handler (closure over the
  // mount-time effect) can drop late events when the composer goes
  // disabled mid-utterance.
  const disabledRef = useRef(disabled);
  disabledRef.current = disabled;
  // Click guard: true between toggle() and the matching start/end event.
  // Prevents rapid double-clicks from calling recognition.start() twice,
  // which throws InvalidStateError in Chrome.
  const transitionRef = useRef(false);
  // Server-take guard, deliberately separate from transitionRef: a failed
  // Web Speech attempt fires a late "end" event that resets transitionRef,
  // which must not unlock a second server take mid-handshake.
  const serverBusyRef = useRef(false);
  // Lets the mount-time Web Speech error handler start the fallback take
  // without closing over toggleServer's identity.
  const toggleServerRef = useRef<() => Promise<void>>(async () => {});

  // Written via .style.transform from rAF — avoids 60Hz React re-renders.
  const barRefs = useRef<(HTMLSpanElement | null)[]>(BAR_BINS.map(() => null));

  useEffect(() => {
    if (!Ctor) return;

    const recognition = new Ctor();
    // Keep listening until the user clicks stop — no auto-stop on silence.
    recognition.continuous = true;
    recognition.interimResults = false;
    recognition.lang = lang;

    // A dead recognizer keeps firing start/end/error after the take has
    // fallen back to the server; those stale events must not clobber the
    // server session's isListening/transition state.
    const serverTakeOwnsState = () => sessionRef.current !== null || serverBusyRef.current;

    const handleStart = () => {
      if (serverTakeOwnsState()) return;
      transitionRef.current = false;
      discardingRef.current = false;
      setError(null);
      setIsListening(true);
      // Snapshot point: let the parent record the text so Esc can revert to it.
      onVoiceStartRef.current?.();
    };
    const handleEnd = () => {
      if (serverTakeOwnsState()) return;
      transitionRef.current = false;
      setIsListening(false);
    };
    const handleError = (event: Event) => {
      if (serverTakeOwnsState()) return;
      transitionRef.current = false;
      const err = (event as SpeechRecognitionErrorEventLike).error;
      // "network" means the recognizer's cloud backend refused us —
      // always the case in Electron/plain Chromium, occasionally a
      // transient blip in real Chrome. Serve THIS take from the server
      // instead; the next take tries Web Speech again.
      if (err === "network" && serverAvailableRef.current && !disabledRef.current) {
        setIsListening(false);
        void toggleServerRef.current();
        return;
      }
      // "no-speech" / "aborted" are routine (silence timeout, user stop).
      if (err === "not-allowed" || err === "service-not-allowed") {
        setError("Microphone permission denied");
      } else if (err && err !== "no-speech" && err !== "aborted") {
        setError("Dictation unavailable");
      }
      setIsListening(false);
    };
    const handleResult = (event: Event) => {
      // Drop late events that arrive after the composer went disabled, or after
      // an Esc discard the parent has already reverted.
      if (disabledRef.current || discardingRef.current) return;
      const speechEvent = event as SpeechRecognitionEventLike;
      let finalTranscript = "";
      for (let i = speechEvent.resultIndex; i < speechEvent.results.length; i += 1) {
        const result = speechEvent.results[i];
        if (result.isFinal) {
          finalTranscript += result[0]?.transcript ?? "";
        }
      }
      const trimmed = finalTranscript.trim();
      if (trimmed) onTranscriptRef.current(trimmed);
    };

    recognition.addEventListener("start", handleStart);
    recognition.addEventListener("end", handleEnd);
    recognition.addEventListener("error", handleError);
    recognition.addEventListener("result", handleResult);
    recognitionRef.current = recognition;

    return () => {
      recognition.removeEventListener("start", handleStart);
      recognition.removeEventListener("end", handleEnd);
      recognition.removeEventListener("error", handleError);
      recognition.removeEventListener("result", handleResult);
      recognition.stop();
      recognitionRef.current = null;
    };
  }, [Ctor, lang]);

  // Auto-stop if the composer goes disabled mid-dictation. Stops the
  // recognizer; the disabledRef guard in handleResult catches any final
  // events still queued before the end event fires. A server session is
  // cancelled outright (no tail flush) — the take is moot once the
  // composer can't accept text.
  useEffect(() => {
    if (!(disabled && isListening)) return;
    if (sessionRef.current) {
      sessionRef.current.cancel();
      sessionRef.current = null;
      setIsListening(false);
      onInterimRef.current?.("");
      return;
    }
    try {
      recognitionRef.current?.stop();
    } catch {
      // .stop() on an already-stopped recognizer can throw in some
      // browsers; safe to ignore — the end event will reconcile state.
    }
  }, [disabled, isListening]);

  // Release the mic if the component unmounts mid-take (e.g. the
  // new-chat dialog closes while dictating).
  useEffect(
    () => () => {
      sessionRef.current?.cancel();
      sessionRef.current = null;
    },
    [],
  );

  // Second getUserMedia stream just for visualization — Web Speech API
  // hides its audio buffer. Chrome batches the permission to one prompt.
  useEffect(() => {
    if (!isListening) return;
    let cancelled = false;
    let stream: MediaStream | null = null;
    let audioCtx: AudioContext | null = null;
    let rafId: number | null = null;
    // Snapshot so cleanup doesn't read a stale .current (exhaustive-deps).
    const bars = barRefs.current;

    const start = async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch {
        // Mic permission denied just for the visualization stream — leave
        // the bars at baseline. The speech recognition error handler will
        // surface the user-facing message if it also fails.
        return;
      }
      if (cancelled) {
        for (const track of stream.getTracks()) track.stop();
        return;
      }
      audioCtx = new AudioContext();
      const source = audioCtx.createMediaStreamSource(stream);
      const analyser = audioCtx.createAnalyser();
      analyser.fftSize = 64;
      // Built-in temporal smoothing so bars don't jitter frame-to-frame.
      analyser.smoothingTimeConstant = 0.75;
      source.connect(analyser);
      const data = new Uint8Array(analyser.frequencyBinCount);

      const tick = () => {
        analyser.getByteFrequencyData(data);
        for (let i = 0; i < BAR_BINS.length; i += 1) {
          const [lo, hi] = BAR_BINS[i];
          let sum = 0;
          for (let j = lo; j < hi; j += 1) sum += data[j];
          const avg = sum / (hi - lo) / 255;
          // 1.6× headroom for quiet speech; clamp at 1 to fit the button.
          const scale = Math.max(BAR_BASELINE, Math.min(1, avg * 1.6));
          const el = bars[i];
          if (el) el.style.transform = `scaleY(${scale})`;
        }
        rafId = requestAnimationFrame(tick);
      };
      rafId = requestAnimationFrame(tick);
    };

    start();

    return () => {
      cancelled = true;
      if (rafId !== null) cancelAnimationFrame(rafId);
      if (stream) {
        for (const track of stream.getTracks()) track.stop();
      }
      if (audioCtx && audioCtx.state !== "closed") {
        audioCtx.close();
      }
      // Reset for the next session.
      for (const el of bars) {
        if (el) el.style.transform = `scaleY(${BAR_BASELINE})`;
      }
    };
  }, [isListening]);

  // Server-dictation toggle. Start resolves only once the mic + socket
  // handshake are up, so isListening flips exactly when audio flows.
  const toggleServer = useCallback(async () => {
    if (serverBusyRef.current) return;
    serverBusyRef.current = true;
    const session = sessionRef.current;
    if (session) {
      sessionRef.current = null;
      const tail = (await session.stop()).trim();
      if (!disabledRef.current) {
        // A non-empty tail supersedes the pending interim via
        // onTranscript; an empty one just clears the interim region.
        if (tail) onTranscriptRef.current(tail);
        else onInterimRef.current?.("");
      }
      setIsListening(false);
      serverBusyRef.current = false;
      return;
    }
    try {
      // Snapshot point: let the parent record the text so Esc can revert to it.
      discardingRef.current = false;
      onVoiceStartRef.current?.();
      const next = await DictationSession.start({
        onPartial: (text) => {
          // Drop late partials after an Esc discard — they'd repopulate the
          // composer the parent just reverted.
          if (!disabledRef.current && !discardingRef.current) onInterimRef.current?.(text);
        },
        onFinal: (text) => {
          const trimmed = text.trim();
          if (trimmed && !disabledRef.current && !discardingRef.current) {
            onTranscriptRef.current(trimmed);
          }
        },
        onError: () => {
          sessionRef.current = null;
          setError("Dictation unavailable");
          setIsListening(false);
          onInterimRef.current?.("");
        },
      });
      sessionRef.current = next;
      setError(null);
      setIsListening(true);
    } catch (startError) {
      setError(
        startError instanceof DictationBusyError
          ? "Dictation is busy — try again shortly"
          : isPermissionError(startError)
            ? "Microphone permission denied"
            : "Dictation unavailable",
      );
      setIsListening(false);
    }
    serverBusyRef.current = false;
  }, []);
  toggleServerRef.current = toggleServer;

  const toggle = useCallback(() => {
    // An active (or starting) server take is owned by the server path,
    // whichever mode started it.
    if (sessionRef.current || serverBusyRef.current) {
      void toggleServer();
      return;
    }
    // In Electron the SpeechRecognition constructor exists but has no backend,
    // so a Web Speech take always fails with "network" and only THEN falls back
    // to the server — a visible ~1s "fail then recover" on every first take.
    // When the server can serve, go straight to it and skip the doomed attempt.
    // (Real browsers keep Web Speech primary; it genuinely works there.)
    if (!Ctor || (serverAvailable && isElectronShell())) {
      if (serverAvailable) void toggleServer();
      return;
    }
    // Guard against rapid clicks landing before start/end event fires.
    if (transitionRef.current) return;
    const recognition = recognitionRef.current;
    if (!recognition) return;
    transitionRef.current = true;
    try {
      if (isListening) recognition.stop();
      else recognition.start();
    } catch {
      // InvalidStateError from a double-call — drop the guard so the
      // user can try again, and let the next event reconcile state.
      transitionRef.current = false;
    }
  }, [isListening, Ctor, serverAvailable, toggleServer]);

  // ⌘⌥V toggles dictation from anywhere — same as clicking the button. Enabled
  // whenever dictation could run (Web Speech OR the server path) and the
  // composer isn't disabled, so the chord is inert when it can't do anything.
  useVoiceDictationHotkey(toggle, enableHotkey && (Boolean(Ctor) || serverAvailable) && !disabled);

  // While listening, Enter commits (end the take, keep the text) and Esc
  // cancels (end the take, discard back to the pre-dictation snapshot). Bound in
  // the capture phase so it preempts the composer's own Enter-sends / Esc-stops.
  // Path-aware: a live server take is torn down via the DictationSession, a Web
  // Speech take via the recognizer.
  useEffect(() => {
    if (!isListening) return;
    const handler = (e: globalThis.KeyboardEvent): void => {
      if (e.repeat || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "Enter" && !e.shiftKey) {
        // Commit: end the take and keep the text. toggle() routes to the right
        // path — Web Speech stop, or a server stop that flushes the tail.
        e.preventDefault();
        e.stopPropagation();
        toggle();
      } else if (e.key === "Escape") {
        // Cancel: flag the discard so trailing results are dropped, revert the
        // composer, then tear the take down immediately (no tail flush).
        e.preventDefault();
        e.stopPropagation();
        discardingRef.current = true;
        onVoiceDiscardRef.current?.();
        const session = sessionRef.current;
        if (session) {
          sessionRef.current = null;
          serverBusyRef.current = false;
          session.cancel();
          setIsListening(false);
        } else {
          try {
            recognitionRef.current?.stop();
          } catch {
            // Already stopping — the end event will reconcile state.
          }
        }
      }
    };
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [isListening, toggle]);

  if (!Ctor && !serverAvailable) return null;

  // Stable accessible name with aria-pressed signals toggle state to
  // screen readers. Error text takes over the tooltip when set.
  const a11yLabel = "Voice dictation";
  const tooltip = error ?? a11yLabel;

  return (
    <Button
      type="button"
      size="icon"
      variant="ghost"
      disabled={disabled}
      onClick={toggle}
      aria-pressed={isListening}
      aria-label={a11yLabel}
      title={tooltip}
      className={cn(
        "size-9 md:size-8",
        isListening &&
          "bg-muted/60 text-foreground hover:bg-destructive/10 hover:text-destructive focus-visible:bg-destructive/10 focus-visible:text-destructive",
        error && "text-destructive",
      )}
    >
      {isListening ? (
        // Bars fade out and stop icon fades in on hover OR keyboard focus,
        // so keyboard users get the stop affordance without needing hover.
        <span className="relative flex size-4 items-center justify-center" aria-hidden>
          <span className="flex h-full items-center gap-[2px] transition-opacity group-hover/button:opacity-0 group-focus-visible/button:opacity-0">
            {BAR_BINS.map(([lo, hi], i) => (
              <span
                key={`${lo}-${hi}`}
                ref={(el) => {
                  barRefs.current[i] = el;
                }}
                className="block h-3 w-[2px] origin-center rounded-full bg-current"
                style={{ transform: `scaleY(${BAR_BASELINE})` }}
              />
            ))}
          </span>
          <SquareIcon className="absolute size-3 fill-current opacity-0 transition-opacity group-hover/button:opacity-100 group-focus-visible/button:opacity-100" />
        </span>
      ) : (
        <MicIcon className="size-4" />
      )}
    </Button>
  );
};
