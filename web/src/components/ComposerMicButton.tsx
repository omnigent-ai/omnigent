"use client";

import { Button } from "@/components/ui/button";
import { useVoiceDictationHotkey } from "@/hooks/useVoiceDictationHotkey";
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

export const ComposerMicButton = ({
  onTranscript,
  disabled,
  lang = "en-US",
  enableHotkey = false,
  onVoiceStart,
  onVoiceDiscard,
}: ComposerMicButtonProps) => {
  // null Ctor → no Web Speech support → render nothing (no server fallback).
  const [Ctor] = useState(getRecognitionCtor);
  const [isListening, setIsListening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  // Ref so the result handler isn't re-attached on every parent re-render.
  const onTranscriptRef = useRef(onTranscript);
  onTranscriptRef.current = onTranscript;
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

  // Written via .style.transform from rAF — avoids 60Hz React re-renders.
  const barRefs = useRef<(HTMLSpanElement | null)[]>(BAR_BINS.map(() => null));

  useEffect(() => {
    if (!Ctor) return;

    const recognition = new Ctor();
    // Keep listening until the user clicks stop — no auto-stop on silence.
    recognition.continuous = true;
    recognition.interimResults = false;
    recognition.lang = lang;

    const handleStart = () => {
      transitionRef.current = false;
      discardingRef.current = false;
      setError(null);
      setIsListening(true);
      // Snapshot point: let the parent record the text so Esc can revert to it.
      onVoiceStartRef.current?.();
    };
    const handleEnd = () => {
      transitionRef.current = false;
      setIsListening(false);
    };
    const handleError = (event: Event) => {
      transitionRef.current = false;
      const err = (event as SpeechRecognitionErrorEventLike).error;
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
  // events still queued before the end event fires.
  useEffect(() => {
    if (!(disabled && isListening)) return;
    try {
      recognitionRef.current?.stop();
    } catch {
      // .stop() on an already-stopped recognizer can throw in some
      // browsers; safe to ignore — the end event will reconcile state.
    }
  }, [disabled, isListening]);

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

  const toggle = useCallback(() => {
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
  }, [isListening]);

  // ⌘⌥V toggles dictation from anywhere — same as clicking the button. Gated on
  // Ctor/disabled so the chord is inert when dictation can't run right now.
  useVoiceDictationHotkey(toggle, enableHotkey && Boolean(Ctor) && !disabled);

  // While listening, Enter commits (stop, keep the dictated text) and Esc
  // cancels (stop, discard back to the pre-dictation snapshot). Bound in the
  // capture phase so it preempts the composer's own Enter-sends / Esc-stops.
  useEffect(() => {
    if (!isListening) return;
    const stop = () => {
      try {
        recognitionRef.current?.stop();
      } catch {
        // Already stopping — the end event will reconcile state.
      }
    };
    const handler = (e: globalThis.KeyboardEvent): void => {
      if (e.repeat || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "Enter" && !e.shiftKey) {
        // Commit: keep the text already in the composer, just end dictation.
        e.preventDefault();
        e.stopPropagation();
        stop();
      } else if (e.key === "Escape") {
        // Cancel: flag the discard so trailing results are dropped, revert the
        // composer, then end dictation.
        e.preventDefault();
        e.stopPropagation();
        discardingRef.current = true;
        onVoiceDiscardRef.current?.();
        stop();
      }
    };
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [isListening]);

  if (!Ctor) return null;

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
