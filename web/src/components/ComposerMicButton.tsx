"use client";

import { Button } from "@/components/ui/button";
import { useVoiceDictationHotkey } from "@/hooks/useVoiceDictationHotkey";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { DictationBusyError, DictationSession } from "@/lib/dictation";
import { readDictationPreferences } from "@/lib/dictationPreferences";
import { isElectronShell } from "@/lib/nativeBridge";
import { cn } from "@/lib/utils";
import { MicIcon, SquareIcon } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState } from "react";

// Local-only types; speech-input.tsx already augments Window globally.
interface SpeechRecognitionLike {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start: () => void;
  stop: () => void;
  addEventListener: (type: string, listener: (event: Event) => void) => void;
  removeEventListener: (type: string, listener: (event: Event) => void) => void;
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
const BAR_BINS: readonly (readonly [number, number])[] = [
  [1, 3],
  [3, 6],
  [6, 10],
  [10, 16],
];

const BAR_BASELINE = 0.2;

type MeterSource = { kind: "web-speech" } | { kind: "server"; stream: MediaStream };

type DictationPhase = "idle" | "starting" | "listening" | "stopping";

const STATUS_CLEAR_DELAY_MS = 2_500;
const STARTUP_TIMEOUT_MS = 45_000;

export interface ComposerMicButtonProps {
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
}

/** getUserMedia permission failures, distinct from transport failures. */
const isPermissionError = (error: unknown): boolean =>
  error instanceof DOMException &&
  (error.name === "NotAllowedError" || error.name === "SecurityError");

const isMissingMicrophoneError = (error: unknown): boolean =>
  error instanceof DOMException &&
  (error.name === "NotFoundError" || error.name === "OverconstrainedError");

export const ComposerMicButton = ({
  onTranscript,
  onInterim,
  disabled,
  lang,
  enableHotkey = false,
  onVoiceStart,
  onVoiceDiscard,
}: ComposerMicButtonProps) => {
  const [preferences] = useState(readDictationPreferences);
  const recognitionLanguage = lang ?? preferences.browserLanguage;
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
  const [phase, setPhase] = useState<DictationPhase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [completionStatus, setCompletionStatus] = useState<string | null>(null);
  const statusId = useId();
  const [meterSource, setMeterSource] = useState<MeterSource | null>(null);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const sessionRef = useRef<DictationSession | null>(null);
  const pendingServerStartRef = useRef<AbortController | null>(null);
  const serverTakeIdRef = useRef(0);
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
  const mountedRef = useRef(true);
  const phaseRef = useRef<DictationPhase>("idle");
  phaseRef.current = phase;
  const webTakeActiveRef = useRef(false);
  const voiceSnapshotTakenRef = useRef(false);
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
  const toggleServerRef = useRef<(snapshot?: boolean) => Promise<void>>(async () => {});

  // Written via .style.transform from rAF — avoids 60Hz React re-renders.
  const barRefs = useRef<(HTMLSpanElement | null)[]>(BAR_BINS.map(() => null));

  useEffect(() => {
    if (!Ctor) return;

    const recognition = new Ctor();
    // Keep listening until the user clicks stop — no auto-stop on silence.
    recognition.continuous = true;
    recognition.interimResults = false;
    recognition.lang = recognitionLanguage;

    // A dead recognizer keeps firing start/end/error after the take has
    // fallen back to the server; those stale events must not clobber the
    // server session's isListening/transition state.
    const serverTakeOwnsState = () => sessionRef.current !== null || serverBusyRef.current;

    const handleStart = () => {
      if (serverTakeOwnsState()) return;
      if (!webTakeActiveRef.current || disabledRef.current) {
        try {
          recognition.stop();
        } catch {
          // A cancelled start may already have stopped the recognizer.
        }
        return;
      }
      transitionRef.current = false;
      discardingRef.current = false;
      setError(null);
      setPhase("listening");
      setMeterSource({ kind: "web-speech" });
      // Snapshot point: let the parent record the text so Esc can revert to it.
      onVoiceStartRef.current?.();
      voiceSnapshotTakenRef.current = true;
    };
    const handleEnd = () => {
      if (serverTakeOwnsState()) return;
      transitionRef.current = false;
      const wasActive = webTakeActiveRef.current;
      webTakeActiveRef.current = false;
      setPhase("idle");
      setMeterSource(null);
      if (wasActive)
        setCompletionStatus(discardingRef.current ? "Dictation cancelled" : "Dictation stopped");
    };
    const handleError = (event: Event) => {
      if (serverTakeOwnsState()) return;
      if (!webTakeActiveRef.current) return;
      transitionRef.current = false;
      setMeterSource(null);
      const err = (event as SpeechRecognitionErrorEventLike).error;
      // "network" means the recognizer's cloud backend refused us —
      // always the case in Electron/plain Chromium, occasionally a
      // transient blip in real Chrome. Serve THIS take from the server
      // instead; the next take tries Web Speech again.
      if (
        err === "network" &&
        preferences.path === "auto" &&
        serverAvailableRef.current &&
        !disabledRef.current
      ) {
        webTakeActiveRef.current = false;
        setPhase("idle");
        void toggleServerRef.current(!voiceSnapshotTakenRef.current);
        return;
      }
      // "no-speech" / "aborted" are routine (silence timeout, user stop).
      if (err === "not-allowed" || err === "service-not-allowed") {
        setError("Microphone access denied. Allow access, then try again.");
      } else if (err && err !== "no-speech" && err !== "aborted") {
        setError("Could not start dictation. Try again.");
      } else if (webTakeActiveRef.current) {
        setCompletionStatus(discardingRef.current ? "Dictation cancelled" : "Dictation stopped");
      }
      webTakeActiveRef.current = false;
      setPhase("idle");
    };
    const handleResult = (event: Event) => {
      // Drop late events that arrive after the composer went disabled, or after
      // an Esc discard the parent has already reverted.
      if (!webTakeActiveRef.current || disabledRef.current || discardingRef.current) return;
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
  }, [Ctor, preferences.path, recognitionLanguage]);

  useEffect(() => {
    if (!completionStatus) return;
    const timer = window.setTimeout(() => setCompletionStatus(null), STATUS_CLEAR_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [completionStatus]);

  useEffect(() => {
    if (phase !== "starting") return;
    const timer = window.setTimeout(() => {
      if (pendingServerStartRef.current) {
        serverTakeIdRef.current += 1;
        pendingServerStartRef.current.abort();
      }
      webTakeActiveRef.current = false;
      transitionRef.current = false;
      try {
        recognitionRef.current?.stop();
      } catch {
        // The recognizer may have failed without dispatching an event.
      }
      setPhase("idle");
      setError("Could not start dictation. Try again.");
    }, STARTUP_TIMEOUT_MS);
    return () => window.clearTimeout(timer);
  }, [phase]);

  // Auto-stop if the composer goes disabled mid-dictation. Stops the
  // recognizer; the disabledRef guard in handleResult catches any final
  // events still queued before the end event fires. A server session is
  // cancelled outright (no tail flush) — the take is moot once the
  // composer can't accept text.
  useEffect(() => {
    if (!disabled) return;
    if (pendingServerStartRef.current) {
      serverTakeIdRef.current += 1;
      pendingServerStartRef.current.abort();
    }
    if (phase === "idle") return;
    webTakeActiveRef.current = false;
    transitionRef.current = false;
    setPhase("idle");
    setCompletionStatus("Dictation cancelled");
    setMeterSource(null);
    if (sessionRef.current) {
      serverTakeIdRef.current += 1;
      sessionRef.current.cancel();
      sessionRef.current = null;
      onInterimRef.current?.("");
      return;
    }
    try {
      recognitionRef.current?.stop();
    } catch {
      // .stop() on an already-stopped recognizer can throw in some
      // browsers; safe to ignore — the end event will reconcile state.
    }
  }, [disabled, phase]);

  // Release the mic if the component unmounts mid-take (e.g. the
  // new-chat dialog closes while dictating).
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      webTakeActiveRef.current = false;
      transitionRef.current = false;
      pendingServerStartRef.current?.abort();
      serverTakeIdRef.current += 1;
      sessionRef.current?.cancel();
      sessionRef.current = null;
    };
  }, []);

  // Web Speech hides its audio buffer, so it owns a separate visualizer
  // capture. Server dictation lends the meter its existing capture stream.
  useEffect(() => {
    if (!meterSource) return;
    const sourceSpec = meterSource;
    let cancelled = false;
    let stream: MediaStream | null = null;
    let ownsStream = false;
    let audioCtx: AudioContext | null = null;
    let rafId: number | null = null;
    // Snapshot so cleanup doesn't read a stale .current (exhaustive-deps).
    const bars = barRefs.current;

    const start = async () => {
      try {
        ownsStream = sourceSpec.kind === "web-speech";
        const nextStream =
          sourceSpec.kind === "web-speech"
            ? await navigator.mediaDevices.getUserMedia({ audio: true })
            : sourceSpec.stream;
        if (cancelled) {
          if (ownsStream) {
            for (const track of nextStream.getTracks()) track.stop();
          }
          return;
        }
        stream = nextStream;
        audioCtx = new AudioContext();
        const source = audioCtx.createMediaStreamSource(nextStream);
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
      } catch {
        if (stream && ownsStream) {
          for (const track of stream.getTracks()) track.stop();
          stream = null;
        }
        if (audioCtx && audioCtx.state !== "closed") void audioCtx.close();
        audioCtx = null;
      }
    };

    start();

    return () => {
      cancelled = true;
      if (rafId !== null) cancelAnimationFrame(rafId);
      if (stream && ownsStream) {
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
  }, [meterSource]);

  // Server-dictation toggle. Start resolves only once the mic + socket
  // handshake are up, so isListening flips exactly when audio flows.
  const toggleServer = useCallback(
    async (snapshot = true) => {
      if (serverBusyRef.current) {
        if (pendingServerStartRef.current) {
          serverTakeIdRef.current += 1;
          pendingServerStartRef.current.abort();
          setPhase("idle");
          setCompletionStatus("Dictation cancelled");
        }
        return;
      }
      serverBusyRef.current = true;
      const session = sessionRef.current;
      if (session) {
        setPhase("stopping");
        const stopTakeId = ++serverTakeIdRef.current;
        sessionRef.current = null;
        setMeterSource(null);
        const tail = (await session.stop()).trim();
        if (!mountedRef.current || disabledRef.current || serverTakeIdRef.current !== stopTakeId) {
          serverBusyRef.current = false;
          return;
        }
        if (!disabledRef.current) {
          // A non-empty tail supersedes the pending interim via
          // onTranscript; an empty one just clears the interim region.
          if (tail) onTranscriptRef.current(tail);
          else onInterimRef.current?.("");
        }
        setPhase("idle");
        setCompletionStatus("Dictation stopped");
        serverBusyRef.current = false;
        return;
      }
      pendingServerStartRef.current?.abort();
      const controller = new AbortController();
      pendingServerStartRef.current = controller;
      const takeId = ++serverTakeIdRef.current;
      setError(null);
      setCompletionStatus(null);
      setPhase("starting");
      try {
        // Snapshot point: let the parent record the text so Esc can revert to it.
        discardingRef.current = false;
        if (snapshot) {
          onVoiceStartRef.current?.();
          voiceSnapshotTakenRef.current = true;
        }
        const next = await DictationSession.start(
          {
            onPartial: (text) => {
              // Drop late partials after an Esc discard — they'd repopulate the
              // composer the parent just reverted.
              if (
                serverTakeIdRef.current === takeId &&
                !disabledRef.current &&
                !discardingRef.current
              ) {
                onInterimRef.current?.(text);
              }
            },
            onFinal: (text) => {
              const trimmed = text.trim();
              if (
                serverTakeIdRef.current === takeId &&
                trimmed &&
                !disabledRef.current &&
                !discardingRef.current
              ) {
                onTranscriptRef.current(trimmed);
              }
            },
            onError: () => {
              if (!mountedRef.current || serverTakeIdRef.current !== takeId) return;
              serverTakeIdRef.current += 1;
              sessionRef.current = null;
              setMeterSource(null);
              setError("Dictation connection lost. Try again.");
              setPhase("idle");
              onInterimRef.current?.("");
            },
          },
          { microphoneDeviceId: preferences.microphoneDeviceId, signal: controller.signal },
        );
        if (
          !mountedRef.current ||
          disabledRef.current ||
          controller.signal.aborted ||
          serverTakeIdRef.current !== takeId
        ) {
          next.cancel();
          return;
        }
        sessionRef.current = next;
        setMeterSource({ kind: "server", stream: next.captureStream });
        setError(null);
        setPhase("listening");
      } catch (startError) {
        if (startError instanceof DOMException && startError.name === "AbortError") return;
        if (!mountedRef.current || disabledRef.current || serverTakeIdRef.current !== takeId)
          return;
        setMeterSource(null);
        setError(
          startError instanceof DictationBusyError
            ? "Dictation is busy. Try again shortly."
            : isPermissionError(startError)
              ? "Microphone access denied. Allow access, then try again."
              : isMissingMicrophoneError(startError)
                ? "Selected microphone unavailable. Choose another in Settings."
                : "Could not connect to dictation. Try again.",
        );
        setPhase("idle");
      } finally {
        if (pendingServerStartRef.current === controller) {
          pendingServerStartRef.current = null;
        }
        serverBusyRef.current = false;
      }
    },
    [preferences.microphoneDeviceId],
  );
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
    if (preferences.path === "server") {
      if (serverAvailable) void toggleServer();
      return;
    }
    if (preferences.path === "auto" && (!Ctor || (serverAvailable && isElectronShell()))) {
      if (serverAvailable) void toggleServer();
      return;
    }
    // Guard against rapid clicks landing before start/end event fires.
    if (transitionRef.current) return;
    const recognition = recognitionRef.current;
    if (!recognition) return;
    transitionRef.current = true;
    try {
      if (phaseRef.current === "listening") {
        setPhase("stopping");
        setMeterSource(null);
        recognition.stop();
      } else {
        webTakeActiveRef.current = true;
        voiceSnapshotTakenRef.current = false;
        discardingRef.current = false;
        setError(null);
        setCompletionStatus(null);
        setPhase("starting");
        recognition.start();
      }
    } catch {
      // InvalidStateError from a double-call — drop the guard so the
      // user can try again, and let the next event reconcile state.
      transitionRef.current = false;
      webTakeActiveRef.current = false;
      setPhase("idle");
      setError("Could not start dictation. Try again.");
    }
  }, [Ctor, preferences.path, serverAvailable, toggleServer]);

  // ⌘⌥V toggles dictation anywhere in the focused Omnigent window. It isn't an
  // OS-global shortcut. Keep it inert when the selected path cannot run.
  const pathAvailable =
    preferences.path === "server"
      ? serverAvailable
      : preferences.path === "browser"
        ? Boolean(Ctor)
        : Boolean(Ctor) || serverAvailable;
  useVoiceDictationHotkey(toggle, enableHotkey && pathAvailable && !disabled);

  // While listening, Enter commits (end the take, keep the text) and Esc
  // cancels (end the take, discard back to the pre-dictation snapshot). Bound in
  // the capture phase so it preempts the composer's own Enter-sends / Esc-stops.
  // Path-aware: a live server take is torn down via the DictationSession, a Web
  // Speech take via the recognizer.
  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent): void => {
      if (e.repeat || e.metaKey || e.ctrlKey || e.altKey) return;
      const pendingStart = pendingServerStartRef.current;
      const currentPhase = phaseRef.current;
      if (currentPhase === "idle" && !pendingStart) return;
      if (e.key === "Enter" && !e.shiftKey) {
        if (currentPhase !== "listening") return;
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
        if (currentPhase === "listening" || currentPhase === "stopping" || pendingStart) {
          onVoiceDiscardRef.current?.();
        }
        if (currentPhase === "stopping") serverTakeIdRef.current += 1;
        setCompletionStatus("Dictation cancelled");
        setPhase("idle");
        if (pendingStart) {
          serverTakeIdRef.current += 1;
          pendingStart.abort();
          return;
        }
        const session = sessionRef.current;
        if (session) {
          serverTakeIdRef.current += 1;
          sessionRef.current = null;
          serverBusyRef.current = false;
          setMeterSource(null);
          session.cancel();
        } else {
          webTakeActiveRef.current = false;
          transitionRef.current = false;
          setMeterSource(null);
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
  }, [toggle]);

  const unavailableMessage =
    preferences.path === "server"
      ? "Server dictation is unavailable."
      : preferences.path === "browser"
        ? "Browser dictation is unavailable."
        : "Voice dictation is unavailable.";
  const isCapabilityLoading = serverInfo === "loading" && preferences.path !== "browser";
  const status =
    error ??
    (phase === "starting"
      ? "Starting dictation..."
      : phase === "listening"
        ? "Listening..."
        : phase === "stopping"
          ? "Stopping dictation..."
          : (completionStatus ??
            (!pathAvailable && !isCapabilityLoading ? unavailableMessage : null)));
  const isBusy = phase === "starting" || phase === "stopping";
  const isListening = phase === "listening";

  // Stable accessible name with aria-pressed signals toggle state to
  // screen readers. Error text takes over the tooltip when set.
  const a11yLabel = "Voice dictation";
  const tooltip =
    error ?? (!pathAvailable && !isCapabilityLoading ? unavailableMessage : a11yLabel);

  return (
    <div className="flex min-w-0 items-center gap-1">
      <Button
        type="button"
        size="icon"
        variant="ghost"
        disabled={disabled || !pathAvailable}
        onClick={toggle}
        aria-pressed={isListening}
        aria-busy={isBusy}
        aria-label={a11yLabel}
        aria-describedby={status ? statusId : undefined}
        title={tooltip}
        className={cn(
          "size-9 shrink-0 md:size-8",
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
      <span
        id={statusId}
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className={cn(
          "max-w-36 text-xs leading-tight text-muted-foreground sm:max-w-48",
          (error || (!pathAvailable && !isCapabilityLoading)) && "text-destructive",
        )}
      >
        {status}
      </span>
    </div>
  );
};
