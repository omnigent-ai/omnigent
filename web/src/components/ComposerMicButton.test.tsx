// Tests for ComposerMicButton — Web Speech API voice dictation.
//
// The button toggles a SpeechRecognition session; final transcripts are
// emitted via onTranscript. It renders nothing when the browser has no
// SpeechRecognition constructor. None of this is e2e-testable (CI has no real
// mic / Web Speech engine), so it's pinned here by stubbing the global
// SpeechRecognition constructor with a fake whose addEventListener captures the
// handlers the test then fires. getUserMedia (used only for the visualizer) is
// stubbed to reject so no AudioContext is constructed in jsdom.

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ComposerMicButton } from "./ComposerMicButton";

/** Captured event handlers keyed by event type, fed by the fake recognition. */
let handlers: Record<string, (event: unknown) => void>;
let startSpy: ReturnType<typeof vi.fn>;
let stopSpy: ReturnType<typeof vi.fn>;
/** Original navigator.mediaDevices descriptor, restored after each test. */
let originalMediaDevices: PropertyDescriptor | undefined;

function installSpeechRecognition() {
  handlers = {};
  startSpy = vi.fn();
  stopSpy = vi.fn();
  // A class (not an arrow fn) so `new Ctor()` is constructable — the component
  // does `new Ctor()` in its mount effect.
  class FakeRecognition {
    continuous = false;
    interimResults = false;
    lang = "en-US";
    start = startSpy;
    stop = stopSpy;
    addEventListener(type: string, handler: (event: unknown) => void) {
      handlers[type] = handler;
    }
    removeEventListener() {}
  }
  vi.stubGlobal("SpeechRecognition", FakeRecognition);
}

/** Build a SpeechRecognition `result` event carrying one final transcript. */
function resultEvent(transcript: string) {
  return {
    resultIndex: 0,
    results: { length: 1, 0: { length: 1, isFinal: true, 0: { transcript } } },
  };
}

beforeEach(() => {
  installSpeechRecognition();
  // The visualizer's getUserMedia is best-effort; reject so no AudioContext
  // (unavailable in jsdom) is ever constructed. Capture the original descriptor
  // first so afterEach can restore it — otherwise this navigator stub leaks.
  originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, "mediaDevices");
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia: vi.fn().mockRejectedValue(new Error("no mic")) },
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  // Restore navigator.mediaDevices so the stub never leaks to other test files.
  if (originalMediaDevices) {
    Object.defineProperty(navigator, "mediaDevices", originalMediaDevices);
  } else {
    delete (navigator as { mediaDevices?: unknown }).mediaDevices;
  }
});

describe("ComposerMicButton", () => {
  it("renders nothing when the browser has no SpeechRecognition support", () => {
    vi.stubGlobal("SpeechRecognition", undefined);
    vi.stubGlobal("webkitSpeechRecognition", undefined);
    const { container } = render(<ComposerMicButton onTranscript={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders an idle, un-pressed dictation button when supported", () => {
    render(<ComposerMicButton onTranscript={vi.fn()} />);
    const button = screen.getByRole("button", { name: "Voice dictation" });
    expect(button).toHaveAttribute("aria-pressed", "false");
  });

  it("starts recognition on click and reflects the recording state", () => {
    render(<ComposerMicButton onTranscript={vi.fn()} />);
    const button = screen.getByRole("button", { name: "Voice dictation" });

    fireEvent.click(button);
    expect(startSpy).toHaveBeenCalledTimes(1);

    // The recognizer's "start" event flips the pressed state.
    act(() => handlers.start?.({}));
    expect(button).toHaveAttribute("aria-pressed", "true");
  });

  it("stops recognition on a second click once recording", () => {
    render(<ComposerMicButton onTranscript={vi.fn()} />);
    const button = screen.getByRole("button", { name: "Voice dictation" });

    fireEvent.click(button);
    act(() => handlers.start?.({}));
    fireEvent.click(button);
    expect(stopSpy).toHaveBeenCalledTimes(1);
  });

  it("delivers the trimmed final transcript via onTranscript", () => {
    const onTranscript = vi.fn();
    render(<ComposerMicButton onTranscript={onTranscript} />);
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    act(() => handlers.start?.({}));

    act(() => handlers.result?.(resultEvent("  hello world  ")));
    expect(onTranscript).toHaveBeenCalledWith("hello world");
  });

  it("does not emit a transcript while the composer is disabled", () => {
    const onTranscript = vi.fn();
    render(<ComposerMicButton onTranscript={onTranscript} disabled />);
    // The button is disabled, but a late recognition result must still be
    // dropped by the disabled guard rather than reaching the callback.
    act(() => handlers.result?.(resultEvent("late words")));
    expect(onTranscript).not.toHaveBeenCalled();
  });

  it("surfaces a permission-denied error in the button tooltip", () => {
    render(<ComposerMicButton onTranscript={vi.fn()} />);
    const button = screen.getByRole("button", { name: "Voice dictation" });

    act(() => handlers.error?.({ error: "not-allowed" }));
    expect(button).toHaveAttribute("title", "Microphone permission denied");
  });

  it("ignores routine no-speech/aborted errors (no tooltip change)", () => {
    render(<ComposerMicButton onTranscript={vi.fn()} />);
    const button = screen.getByRole("button", { name: "Voice dictation" });

    act(() => handlers.error?.({ error: "no-speech" }));
    expect(button).toHaveAttribute("title", "Voice dictation");
  });
});
