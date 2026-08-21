// Tests for ComposerMicButton — Web Speech API voice dictation plus the
// server-dictation fallback.
//
// Web Speech mode: the button toggles a SpeechRecognition session; final
// transcripts are emitted via onTranscript. It renders nothing when the
// browser has no SpeechRecognition constructor AND the server offers no
// dictation. None of this is e2e-testable (CI has no real mic / Web Speech
// engine), so it's pinned here by stubbing the global SpeechRecognition
// constructor with a fake whose addEventListener captures the handlers the
// test then fires. getUserMedia (used only for the visualizer) is stubbed to
// reject so no AudioContext is constructed in jsdom.
//
// Server mode: when there is no SpeechRecognition constructor but the
// /v1/info capability probe reports dictation_available, the button drives a
// DictationSession instead (mocked here — the real transport needs a mic,
// an AudioWorklet, and a WebSocket; the full loop runs in the Playwright
// e2e test against the server's fake engine).

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { CapabilitiesContext } from "@/lib/CapabilitiesContext";
import type { ServerInfo } from "@/lib/capabilities";
import type { DictationSessionEvents, DictationSessionOptions } from "@/lib/dictation";
import { ComposerMicButton } from "./ComposerMicButton";

// Controllable DictationSession stand-in for the server-mode tests. The
// factory reads the mutable spies at call time, so each test installs its
// own behavior in beforeEach.
interface SessionStub {
  captureStream: MediaStream;
  stop: () => Promise<string>;
  cancel: () => void;
}
let sessionStartMock: Mock<
  (events: DictationSessionEvents, options?: DictationSessionOptions) => Promise<SessionStub>
>;
let sessionStopMock: Mock<() => Promise<string>>;
let sessionCancelMock: Mock<() => void>;
let sessionEvents: DictationSessionEvents | null;
let sessionStream: MediaStream;

vi.mock("@/lib/dictation", () => {
  class DictationBusyError extends Error {}
  return {
    DictationBusyError,
    DictationSession: {
      start: (events: DictationSessionEvents, options?: DictationSessionOptions) =>
        sessionStartMock(events, options),
    },
  };
});

function installDictationSession() {
  sessionEvents = null;
  sessionStopMock = vi.fn(async () => "");
  sessionCancelMock = vi.fn();
  sessionStream = { getTracks: () => [] } as unknown as MediaStream;
  sessionStartMock = vi.fn(async (events: DictationSessionEvents) => {
    sessionEvents = events;
    return { captureStream: sessionStream, stop: sessionStopMock, cancel: sessionCancelMock };
  });
}

/** Captured event handlers keyed by event type, fed by the fake recognition. */
let handlers: Record<string, (event: unknown) => void>;
let startSpy: ReturnType<typeof vi.fn>;
let stopSpy: ReturnType<typeof vi.fn>;
let recognitionLanguage: string;
let getUserMediaMock: Mock;
/** Original navigator.mediaDevices descriptor, restored after each test. */
let originalMediaDevices: PropertyDescriptor | undefined;

interface MeterMocks {
  close: Mock;
  createMediaStreamSource: Mock;
  cancelAnimationFrame: Mock;
}

function installMeterAudio(): MeterMocks {
  const close = vi.fn(function (this: { state: AudioContextState }) {
    this.state = "closed";
    return Promise.resolve();
  });
  const createMediaStreamSource = vi.fn(() => ({ connect: vi.fn() }));
  const createAnalyser = vi.fn(() => ({
    fftSize: 0,
    smoothingTimeConstant: 0,
    frequencyBinCount: 32,
    getByteFrequencyData: vi.fn(),
  }));
  class FakeAudioContext {
    state: AudioContextState = "running";
    close = close;
    createMediaStreamSource = createMediaStreamSource;
    createAnalyser = createAnalyser;
  }
  const cancelAnimationFrame = vi.fn();
  vi.stubGlobal("AudioContext", FakeAudioContext);
  vi.stubGlobal(
    "requestAnimationFrame",
    vi.fn(() => 1),
  );
  vi.stubGlobal("cancelAnimationFrame", cancelAnimationFrame);
  return { close, createMediaStreamSource, cancelAnimationFrame };
}

function mediaStream(stop = vi.fn()): { stream: MediaStream; stop: Mock } {
  return {
    stream: { getTracks: () => [{ stop }] } as unknown as MediaStream,
    stop,
  };
}

function installSpeechRecognition() {
  handlers = {};
  startSpy = vi.fn();
  stopSpy = vi.fn();
  // A class (not an arrow fn) so `new Ctor()` is constructable — the component
  // does `new Ctor()` in its mount effect.
  class FakeRecognition {
    continuous = false;
    interimResults = false;
    private recognitionLang = "en-US";
    get lang() {
      return this.recognitionLang;
    }
    set lang(value: string) {
      this.recognitionLang = value;
      recognitionLanguage = value;
    }
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
  installDictationSession();
  // The visualizer's getUserMedia is best-effort; reject so no AudioContext
  // (unavailable in jsdom) is ever constructed. Capture the original descriptor
  // first so afterEach can restore it — otherwise this navigator stub leaks.
  originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, "mediaDevices");
  getUserMediaMock = vi.fn().mockRejectedValue(new Error("no mic"));
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia: getUserMediaMock },
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  localStorage.clear();
  // Restore navigator.mediaDevices so the stub never leaks to other test files.
  if (originalMediaDevices) {
    Object.defineProperty(navigator, "mediaDevices", originalMediaDevices);
  } else {
    delete (navigator as { mediaDevices?: unknown }).mediaDevices;
  }
});

describe("ComposerMicButton", () => {
  it("keeps the button disabled while fallback availability is loading", () => {
    vi.stubGlobal("SpeechRecognition", undefined);
    vi.stubGlobal("webkitSpeechRecognition", undefined);
    render(<ComposerMicButton onTranscript={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Voice dictation" })).toBeDisabled();
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
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
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("status")).toHaveTextContent("Starting dictation...");

    // The recognizer's "start" event flips the pressed state.
    act(() => handlers.start?.({}));
    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(button).toHaveAttribute("aria-busy", "false");
    expect(screen.getByRole("status")).toHaveTextContent("Listening...");
  });

  it("times out a Web Speech start that never reports listening", () => {
    vi.useFakeTimers();
    try {
      render(<ComposerMicButton onTranscript={vi.fn()} />);
      const button = screen.getByRole("button", { name: "Voice dictation" });
      fireEvent.click(button);

      act(() => vi.advanceTimersByTime(45_000));

      expect(stopSpy).toHaveBeenCalledOnce();
      expect(button).toHaveAttribute("aria-busy", "false");
      expect(screen.getByRole("status")).toHaveTextContent("Could not start dictation. Try again.");
    } finally {
      vi.useRealTimers();
    }
  });

  it("cancels Web Speech when clicked again during startup", () => {
    render(<ComposerMicButton onTranscript={vi.fn()} />);
    const button = screen.getByRole("button", { name: "Voice dictation" });
    fireEvent.click(button);

    fireEvent.click(button);

    expect(stopSpy).toHaveBeenCalledOnce();
    expect(button).toHaveAttribute("aria-busy", "false");
    expect(screen.getByRole("status")).toHaveTextContent("Dictation cancelled");

    act(() => handlers.start?.({}));
    expect(button).toHaveAttribute("aria-pressed", "false");
  });

  it("captures a separate owned stream for the Web Speech meter", async () => {
    const meter = installMeterAudio();
    const owned = mediaStream();
    getUserMediaMock.mockResolvedValue(owned.stream);
    render(<ComposerMicButton onTranscript={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    await act(async () => handlers.start?.({}));

    expect(getUserMediaMock).toHaveBeenCalledOnce();
    expect(meter.createMediaStreamSource).toHaveBeenCalledWith(owned.stream);
    expect(owned.stop).not.toHaveBeenCalled();
  });

  it.each([
    ["stop", () => handlers.end?.({})],
    ["error", () => handlers.error?.({ error: "network" })],
    [
      "Escape",
      () => window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })),
    ],
  ])("cleans up the owned Web Speech meter on %s", async (_name, finish) => {
    const meter = installMeterAudio();
    const owned = mediaStream();
    getUserMediaMock.mockResolvedValue(owned.stream);
    render(<ComposerMicButton onTranscript={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    await act(async () => handlers.start?.({}));

    act(finish);

    expect(owned.stop).toHaveBeenCalledOnce();
    expect(meter.close).toHaveBeenCalledOnce();
    expect(meter.cancelAnimationFrame).toHaveBeenCalledWith(1);
  });

  it("cleans up the owned Web Speech meter when disabled", async () => {
    const meter = installMeterAudio();
    const owned = mediaStream();
    getUserMediaMock.mockResolvedValue(owned.stream);
    const { rerender } = render(<ComposerMicButton onTranscript={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    await act(async () => handlers.start?.({}));

    rerender(<ComposerMicButton onTranscript={vi.fn()} disabled />);

    expect(owned.stop).toHaveBeenCalledOnce();
    expect(meter.close).toHaveBeenCalledOnce();
  });

  it("stops a late Web Speech meter capture after unmount", async () => {
    installMeterAudio();
    const owned = mediaStream();
    let resolveCapture: ((stream: MediaStream) => void) | undefined;
    getUserMediaMock.mockReturnValue(
      new Promise<MediaStream>((resolve) => {
        resolveCapture = resolve;
      }),
    );
    const { unmount } = render(<ComposerMicButton onTranscript={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    act(() => handlers.start?.({}));

    unmount();
    await act(async () => resolveCapture?.(owned.stream));

    expect(owned.stop).toHaveBeenCalledOnce();
  });

  it("stops its owned stream when meter setup fails", async () => {
    const owned = mediaStream();
    getUserMediaMock.mockResolvedValue(owned.stream);
    function BrokenAudioContext() {
      throw new Error("audio context failed");
    }
    vi.stubGlobal("AudioContext", BrokenAudioContext);
    render(<ComposerMicButton onTranscript={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));

    await act(async () => handlers.start?.({}));

    expect(owned.stop).toHaveBeenCalledOnce();
  });

  it("uses the saved browser recognition language", () => {
    localStorage.setItem(
      "omnigent:dictation-preferences",
      JSON.stringify({ path: "browser", browserLanguage: "de-DE", microphoneDeviceId: null }),
    );
    render(<ComposerMicButton onTranscript={vi.fn()} />);
    expect(recognitionLanguage).toBe("de-DE");
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

  it("shows and announces actionable permission feedback", () => {
    render(<ComposerMicButton onTranscript={vi.fn()} />);
    const button = screen.getByRole("button", { name: "Voice dictation" });

    fireEvent.click(button);
    act(() => handlers.error?.({ error: "not-allowed" }));
    expect(screen.getByRole("status")).toHaveTextContent(
      "Microphone access denied. Allow access, then try again.",
    );
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(button).toHaveAccessibleDescription(
      "Microphone access denied. Allow access, then try again.",
    );
  });

  it("announces a routine recognizer stop without showing an error", () => {
    render(<ComposerMicButton onTranscript={vi.fn()} />);
    const button = screen.getByRole("button", { name: "Voice dictation" });

    fireEvent.click(button);
    act(() => handlers.error?.({ error: "no-speech" }));
    expect(button).toHaveAttribute("title", "Voice dictation");
    expect(screen.getByRole("status")).toHaveTextContent("Dictation stopped");
  });

  it("announces normal stop and discard outcomes", () => {
    render(<ComposerMicButton onTranscript={vi.fn()} onVoiceDiscard={vi.fn()} />);
    const button = screen.getByRole("button", { name: "Voice dictation" });
    fireEvent.click(button);
    act(() => handlers.start?.({}));
    fireEvent.click(button);
    expect(screen.getByRole("status")).toHaveTextContent("Stopping dictation...");
    act(() => handlers.end?.({}));
    expect(screen.getByRole("status")).toHaveTextContent("Dictation stopped");

    fireEvent.click(button);
    act(() => handlers.start?.({}));
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })));
    expect(screen.getByRole("status")).toHaveTextContent("Dictation cancelled");
  });

  it("snapshots via onVoiceStart when dictation begins", () => {
    const onVoiceStart = vi.fn();
    render(<ComposerMicButton onTranscript={vi.fn()} onVoiceStart={onVoiceStart} />);
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));

    act(() => handlers.start?.({}));
    expect(onVoiceStart).toHaveBeenCalledTimes(1);
  });

  it("Enter while listening stops dictation and keeps the text (no discard)", () => {
    const onVoiceDiscard = vi.fn();
    render(<ComposerMicButton onTranscript={vi.fn()} onVoiceDiscard={onVoiceDiscard} />);
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    act(() => handlers.start?.({}));

    const e = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    act(() => {
      window.dispatchEvent(e);
    });

    expect(stopSpy).toHaveBeenCalledTimes(1);
    expect(onVoiceDiscard).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(true);
  });

  it("Esc while listening stops dictation and discards the text", () => {
    const onVoiceDiscard = vi.fn();
    render(<ComposerMicButton onTranscript={vi.fn()} onVoiceDiscard={onVoiceDiscard} />);
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    act(() => handlers.start?.({}));

    const e = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    act(() => {
      window.dispatchEvent(e);
    });

    expect(stopSpy).toHaveBeenCalledTimes(1);
    expect(onVoiceDiscard).toHaveBeenCalledTimes(1);
    expect(e.defaultPrevented).toBe(true);
  });

  it("drops a late transcript that arrives after an Esc discard", () => {
    const onTranscript = vi.fn();
    render(<ComposerMicButton onTranscript={onTranscript} onVoiceDiscard={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    act(() => handlers.start?.({}));

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    // A trailing final result races in before the recognizer's end event.
    act(() => handlers.result?.(resultEvent("trailing words")));

    expect(onTranscript).not.toHaveBeenCalled();
  });

  it("does not intercept Enter/Esc when not listening", () => {
    const onVoiceDiscard = vi.fn();
    render(<ComposerMicButton onTranscript={vi.fn()} onVoiceDiscard={onVoiceDiscard} />);
    // Never started listening.
    const enter = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    const esc = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    act(() => {
      window.dispatchEvent(enter);
      window.dispatchEvent(esc);
    });

    expect(stopSpy).not.toHaveBeenCalled();
    expect(onVoiceDiscard).not.toHaveBeenCalled();
    expect(enter.defaultPrevented).toBe(false);
    expect(esc.defaultPrevented).toBe(false);
  });
});

/** ServerInfo with dictation on; the other capabilities are irrelevant here. */
const DICTATION_INFO: ServerInfo = {
  accounts_enabled: false,
  single_user: false,
  login_url: null,
  needs_setup: false,
  databricks_features: false,
  managed_sandboxes_enabled: false,
  sandbox_provider: null,
  sharing_mode: "on",
  public_sharing_enabled: true,
  server_version: "test",
  smart_routing_enabled: false,
  smart_routing_sources: { external: false, oss: false },
  features: {},
  harness_install_enabled: false,
  installable_harnesses: [],
  dictation_available: true,
};

const NO_DICTATION_INFO: ServerInfo = {
  ...DICTATION_INFO,
  dictation_available: false,
};

function renderServerMode(
  props: Partial<React.ComponentProps<typeof ComposerMicButton>> = {},
  info: ServerInfo = DICTATION_INFO,
) {
  // No SpeechRecognition constructor → the component must pick server mode.
  vi.stubGlobal("SpeechRecognition", undefined);
  vi.stubGlobal("webkitSpeechRecognition", undefined);
  return render(
    <CapabilitiesContext.Provider value={info}>
      <ComposerMicButton onTranscript={vi.fn()} {...props} />
    </CapabilitiesContext.Provider>,
  );
}

async function clickMic() {
  // toggle() kicks off the async DictationSession.start; flush it inside act.
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
  });
}

describe("ComposerMicButton (server dictation)", () => {
  it("borrows the direct server capture for metering without another capture", async () => {
    const meter = installMeterAudio();
    const borrowed = mediaStream();
    getUserMediaMock.mockResolvedValue(borrowed.stream);
    sessionStartMock = vi.fn(async (events: DictationSessionEvents) => {
      sessionEvents = events;
      const captureStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      return { captureStream, stop: sessionStopMock, cancel: sessionCancelMock };
    });
    renderServerMode();

    await clickMic();

    expect(sessionStartMock).toHaveBeenCalledOnce();
    expect(getUserMediaMock).toHaveBeenCalledOnce();
    expect(meter.createMediaStreamSource).toHaveBeenCalledWith(borrowed.stream);
    expect(borrowed.stop).not.toHaveBeenCalled();
  });

  it.each(["stop", "error", "disable", "Escape", "unmount"])(
    "cleans up but never stops the borrowed server stream on %s",
    async (finish) => {
      const meter = installMeterAudio();
      const borrowed = mediaStream();
      sessionStream = borrowed.stream;
      const view = renderServerMode();
      await clickMic();

      if (finish === "stop") await clickMic();
      else if (finish === "error") act(() => sessionEvents?.onError("failed"));
      else if (finish === "disable") {
        view.rerender(
          <CapabilitiesContext.Provider value={DICTATION_INFO}>
            <ComposerMicButton onTranscript={vi.fn()} disabled />
          </CapabilitiesContext.Provider>,
        );
      } else if (finish === "Escape") {
        act(() =>
          window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })),
        );
      } else view.unmount();

      expect(borrowed.stop).not.toHaveBeenCalled();
      expect(meter.close).toHaveBeenCalledOnce();
      expect(meter.cancelAnimationFrame).toHaveBeenCalledWith(1);
    },
  );

  it("cancels a session that resolves after unmount", async () => {
    let resolveStart: ((session: SessionStub) => void) | undefined;
    sessionStartMock = vi.fn(
      () =>
        new Promise<SessionStub>((resolve) => {
          resolveStart = resolve;
        }),
    );
    const { unmount } = renderServerMode();
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    unmount();

    await act(async () =>
      resolveStart?.({
        captureStream: sessionStream,
        stop: sessionStopMock,
        cancel: sessionCancelMock,
      }),
    );
    expect(sessionCancelMock).toHaveBeenCalledTimes(1);
  });

  it.each(["unmount", "disable", "toggle", "Escape"])(
    "aborts a pending server start on %s without showing an error",
    async (cancel) => {
      let rejectStart: ((error: unknown) => void) | undefined;
      sessionStartMock = vi.fn(
        (_events, options) =>
          new Promise<SessionStub>((_resolve, reject) => {
            rejectStart = reject;
            options?.signal?.addEventListener("abort", () =>
              reject(new DOMException("aborted", "AbortError")),
            );
          }),
      );
      const view = renderServerMode();
      const button = screen.getByRole("button", { name: "Voice dictation" });
      fireEvent.click(button);
      const signal = sessionStartMock.mock.calls[0]?.[1]?.signal;

      if (cancel === "unmount") view.unmount();
      else if (cancel === "disable") {
        view.rerender(
          <CapabilitiesContext.Provider value={DICTATION_INFO}>
            <ComposerMicButton onTranscript={vi.fn()} disabled />
          </CapabilitiesContext.Provider>,
        );
      } else if (cancel === "toggle") fireEvent.click(button);
      else {
        window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      }
      await act(async () => {
        if (!signal?.aborted) rejectStart?.(new Error("start was not aborted"));
      });

      expect(signal?.aborted).toBe(true);
      if (cancel !== "unmount") {
        expect(screen.getByRole("button", { name: "Voice dictation" })).toHaveAttribute(
          "title",
          "Voice dictation",
        );
      }
    },
  );

  it("cancels a session that resolves after the composer becomes disabled", async () => {
    let resolveStart: ((session: SessionStub) => void) | undefined;
    sessionStartMock = vi.fn(
      () =>
        new Promise<SessionStub>((resolve) => {
          resolveStart = resolve;
        }),
    );
    const { rerender } = renderServerMode();
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    rerender(
      <CapabilitiesContext.Provider value={DICTATION_INFO}>
        <ComposerMicButton onTranscript={vi.fn()} disabled />
      </CapabilitiesContext.Provider>,
    );

    await act(async () =>
      resolveStart?.({
        captureStream: sessionStream,
        stop: sessionStopMock,
        cancel: sessionCancelMock,
      }),
    );
    expect(sessionCancelMock).toHaveBeenCalledTimes(1);
  });

  it("ignores transport events from a cancelled take after a new take starts", async () => {
    const firstEvents: { current: DictationSessionEvents | null } = { current: null };
    const onTranscript = vi.fn();
    sessionStartMock = vi
      .fn()
      .mockImplementationOnce(async (events: DictationSessionEvents) => {
        firstEvents.current = events;
        return { captureStream: sessionStream, stop: sessionStopMock, cancel: sessionCancelMock };
      })
      .mockImplementationOnce(async (events: DictationSessionEvents) => {
        sessionEvents = events;
        return { captureStream: sessionStream, stop: sessionStopMock, cancel: sessionCancelMock };
      });
    renderServerMode({ onTranscript });
    await clickMic();
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })));
    await clickMic();

    act(() => {
      firstEvents.current?.onFinal("late final");
      firstEvents.current?.onError("late failure");
      sessionEvents?.onFinal("current final");
    });

    expect(onTranscript).toHaveBeenCalledOnce();
    expect(onTranscript).toHaveBeenCalledWith("current final");
    expect(screen.getByRole("status")).toHaveTextContent("Listening...");
  });

  it("shows cancelled and ignores late startup resolution when disabled", async () => {
    let resolveStart: ((session: SessionStub) => void) | undefined;
    sessionStartMock = vi.fn(
      () =>
        new Promise<SessionStub>((resolve) => {
          resolveStart = resolve;
        }),
    );
    const view = renderServerMode();
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    view.rerender(
      <CapabilitiesContext.Provider value={DICTATION_INFO}>
        <ComposerMicButton onTranscript={vi.fn()} disabled />
      </CapabilitiesContext.Provider>,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Dictation cancelled");

    await act(async () =>
      resolveStart?.({
        captureStream: sessionStream,
        stop: sessionStopMock,
        cancel: sessionCancelMock,
      }),
    );
    expect(sessionCancelMock).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Voice dictation" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("times out a cold server start and cancels a late session", async () => {
    vi.useFakeTimers();
    try {
      let resolveStart: ((session: SessionStub) => void) | undefined;
      sessionStartMock = vi.fn(
        () =>
          new Promise<SessionStub>((resolve) => {
            resolveStart = resolve;
          }),
      );
      renderServerMode();
      fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));

      act(() => vi.advanceTimersByTime(45_000));

      expect(sessionStartMock.mock.calls[0]?.[1]?.signal?.aborted).toBe(true);
      expect(screen.getByRole("status")).toHaveTextContent("Could not start dictation. Try again.");
      await act(async () =>
        resolveStart?.({
          captureStream: sessionStream,
          stop: sessionStopMock,
          cancel: sessionCancelMock,
        }),
      );
      expect(sessionCancelMock).toHaveBeenCalledOnce();
      expect(screen.getByRole("button", { name: "Voice dictation" })).toHaveAttribute(
        "aria-pressed",
        "false",
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("server mode bypasses Web Speech and passes the selected microphone", async () => {
    localStorage.setItem(
      "omnigent:dictation-preferences",
      JSON.stringify({ path: "server", browserLanguage: "en-US", microphoneDeviceId: "mic-2" }),
    );
    render(
      <CapabilitiesContext.Provider value={DICTATION_INFO}>
        <ComposerMicButton onTranscript={vi.fn()} />
      </CapabilitiesContext.Provider>,
    );
    await clickMic();

    expect(startSpy).not.toHaveBeenCalled();
    expect(sessionStartMock).toHaveBeenCalledWith(expect.any(Object), {
      microphoneDeviceId: "mic-2",
      signal: expect.any(AbortSignal),
    });
  });

  it("browser mode never falls back to the server after a network error", async () => {
    localStorage.setItem(
      "omnigent:dictation-preferences",
      JSON.stringify({ path: "browser", browserLanguage: "en-US", microphoneDeviceId: null }),
    );
    render(
      <CapabilitiesContext.Provider value={DICTATION_INFO}>
        <ComposerMicButton onTranscript={vi.fn()} />
      </CapabilitiesContext.Provider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    await act(async () => handlers.error?.({ error: "network" }));

    expect(startSpy).toHaveBeenCalledTimes(1);
    expect(sessionStartMock).not.toHaveBeenCalled();
  });

  it("shows the selected strict path as unavailable", () => {
    localStorage.setItem(
      "omnigent:dictation-preferences",
      JSON.stringify({ path: "server", browserLanguage: "en-US", microphoneDeviceId: null }),
    );
    render(
      <CapabilitiesContext.Provider value={NO_DICTATION_INFO}>
        <ComposerMicButton onTranscript={vi.fn()} />
      </CapabilitiesContext.Provider>,
    );
    expect(screen.getByRole("button", { name: "Voice dictation" })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("Server dictation is unavailable.");
  });

  it("renders the button when the server advertises dictation", () => {
    renderServerMode();
    expect(screen.getByRole("button", { name: "Voice dictation" })).toBeInTheDocument();
  });

  it("shows unavailable when neither Web Speech nor the server can help", () => {
    renderServerMode({}, NO_DICTATION_INFO);
    expect(screen.getByRole("button", { name: "Voice dictation" })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("Voice dictation is unavailable.");
  });

  it("starts a session on click and reflects the recording state", async () => {
    let resolveStart: ((session: SessionStub) => void) | undefined;
    sessionStartMock = vi.fn(
      () =>
        new Promise<SessionStub>((resolve) => {
          resolveStart = resolve;
        }),
    );
    renderServerMode();
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    const button = screen.getByRole("button", { name: "Voice dictation" });
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("status")).toHaveTextContent("Starting dictation...");

    await act(async () =>
      resolveStart?.({
        captureStream: sessionStream,
        stop: sessionStopMock,
        cancel: sessionCancelMock,
      }),
    );
    expect(sessionStartMock).toHaveBeenCalledTimes(1);
    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(button).toHaveAttribute("aria-busy", "false");
    expect(screen.getByRole("status")).toHaveTextContent("Listening...");
  });

  it("routes partials to onInterim and finals to onTranscript", async () => {
    const onTranscript = vi.fn();
    const onInterim = vi.fn();
    renderServerMode({ onTranscript, onInterim });
    await clickMic();

    act(() => sessionEvents?.onPartial("hello wor"));
    expect(onInterim).toHaveBeenCalledWith("hello wor");
    expect(onTranscript).not.toHaveBeenCalled();

    act(() => sessionEvents?.onFinal("Hello, world."));
    expect(onTranscript).toHaveBeenCalledWith("Hello, world.");
  });

  it("stop click flushes the tail into onTranscript", async () => {
    const onTranscript = vi.fn();
    sessionStopMock = vi.fn(async () => "tail words");
    renderServerMode({ onTranscript });
    await clickMic();
    await clickMic();

    expect(sessionStopMock).toHaveBeenCalledTimes(1);
    expect(onTranscript).toHaveBeenCalledWith("tail words");
    expect(screen.getByRole("button", { name: "Voice dictation" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("stop with an empty tail clears the interim region instead", async () => {
    const onTranscript = vi.fn();
    const onInterim = vi.fn();
    renderServerMode({ onTranscript, onInterim });
    await clickMic();
    await clickMic();

    expect(onTranscript).not.toHaveBeenCalled();
    expect(onInterim).toHaveBeenCalledWith("");
  });

  it("does not publish a late stop tail after disable or unmount", async () => {
    let resolveStop: ((tail: string) => void) | undefined;
    sessionStopMock = vi.fn(
      () =>
        new Promise<string>((resolve) => {
          resolveStop = resolve;
        }),
    );
    const onTranscript = vi.fn();
    const view = renderServerMode({ onTranscript });
    await clickMic();
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));

    view.unmount();
    await act(async () => resolveStop?.("late tail"));

    expect(onTranscript).not.toHaveBeenCalled();
  });

  it("keeps Escape cancellation authoritative during an asynchronous stop", async () => {
    let resolveStop: ((tail: string) => void) | undefined;
    sessionStopMock = vi.fn(
      () =>
        new Promise<string>((resolve) => {
          resolveStop = resolve;
        }),
    );
    const onTranscript = vi.fn();
    const onVoiceDiscard = vi.fn();
    renderServerMode({ onTranscript, onVoiceDiscard });
    await clickMic();
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })));
    expect(screen.getByRole("status")).toHaveTextContent("Dictation cancelled");
    expect(onVoiceDiscard).toHaveBeenCalledOnce();

    await act(async () => resolveStop?.("late tail"));

    expect(onTranscript).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent("Dictation cancelled");
  });

  it("shows mic permission denial with recovery guidance", async () => {
    sessionStartMock = vi.fn(async () => {
      throw new DOMException("denied", "NotAllowedError");
    });
    renderServerMode();
    await clickMic();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Microphone access denied. Allow access, then try again.",
    );
  });

  it("a mid-take transport error resets state and reports unavailable", async () => {
    const onInterim = vi.fn();
    renderServerMode({ onInterim });
    await clickMic();

    act(() => sessionEvents?.onError("dictation failed"));
    const button = screen.getByRole("button", { name: "Voice dictation" });
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("status")).toHaveTextContent("Dictation connection lost. Try again.");
    expect(onInterim).toHaveBeenCalledWith("");
  });

  it("falls back to server dictation when Web Speech dies with a network error", async () => {
    // Electron / plain Chromium: the SpeechRecognition constructor exists
    // (so Web Speech is picked first) but its cloud backend rejects the
    // build at runtime with "network". The take must fall back to server
    // dictation so the user's click still lands.
    const onInterim = vi.fn();
    const onVoiceStart = vi.fn();
    render(
      <CapabilitiesContext.Provider value={DICTATION_INFO}>
        <ComposerMicButton
          onTranscript={vi.fn()}
          onInterim={onInterim}
          onVoiceStart={onVoiceStart}
        />
      </CapabilitiesContext.Provider>,
    );
    const button = screen.getByRole("button", { name: "Voice dictation" });

    fireEvent.click(button);
    expect(startSpy).toHaveBeenCalledTimes(1);
    act(() => handlers.start?.({}));
    expect(onVoiceStart).toHaveBeenCalledOnce();
    await act(async () => handlers.error?.({ error: "network" }));

    // The take restarted on the server path, with no error tooltip for
    // the silent switch, and partials flow.
    expect(sessionStartMock).toHaveBeenCalledTimes(1);
    expect(onVoiceStart).toHaveBeenCalledOnce();
    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(button).toHaveAttribute("title", "Voice dictation");
    act(() => sessionEvents?.onPartial("via server"));
    expect(onInterim).toHaveBeenCalledWith("via server");

    // Stale events from the dead recognizer must not clobber the live
    // server take's state (Chrome fires "end" after a failed start).
    act(() => handlers.end?.({}));
    expect(button).toHaveAttribute("aria-pressed", "true");

    // The fallback is per take, not sticky: after stopping, the next
    // take tries Web Speech again (a transient Chrome blip must not
    // permanently downgrade the page to the server model).
    await clickMic(); // stop the server take
    await clickMic(); // next take
    expect(startSpy).toHaveBeenCalledTimes(2);
    expect(sessionStartMock).toHaveBeenCalledTimes(1);
  });

  it("snapshots once when Web Speech falls back before its start event", async () => {
    const onVoiceStart = vi.fn();
    render(
      <CapabilitiesContext.Provider value={DICTATION_INFO}>
        <ComposerMicButton onTranscript={vi.fn()} onVoiceStart={onVoiceStart} />
      </CapabilitiesContext.Provider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));

    await act(async () => handlers.error?.({ error: "network" }));

    expect(sessionStartMock).toHaveBeenCalledOnce();
    expect(onVoiceStart).toHaveBeenCalledOnce();
  });

  it("switches from an owned Web Speech meter to the borrowed fallback stream", async () => {
    const meter = installMeterAudio();
    const owned = mediaStream();
    const borrowed = mediaStream();
    getUserMediaMock.mockResolvedValue(owned.stream);
    sessionStream = borrowed.stream;
    render(
      <CapabilitiesContext.Provider value={DICTATION_INFO}>
        <ComposerMicButton onTranscript={vi.fn()} />
      </CapabilitiesContext.Provider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Voice dictation" }));
    await act(async () => handlers.start?.({}));

    await act(async () => handlers.error?.({ error: "network" }));

    expect(getUserMediaMock).toHaveBeenCalledOnce();
    expect(owned.stop).toHaveBeenCalledOnce();
    expect(borrowed.stop).not.toHaveBeenCalled();
    expect(meter.createMediaStreamSource).toHaveBeenNthCalledWith(1, owned.stream);
    expect(meter.createMediaStreamSource).toHaveBeenNthCalledWith(2, borrowed.stream);
  });

  it("reports a busy server distinctly from a broken one", async () => {
    const { DictationBusyError } = await import("@/lib/dictation");
    sessionStartMock = vi.fn(async () => {
      throw new DictationBusyError("at capacity");
    });
    renderServerMode();
    await clickMic();
    expect(screen.getByRole("status")).toHaveTextContent("Dictation is busy. Try again shortly.");
  });

  it("keeps the plain error path when the server offers no dictation", async () => {
    render(
      <CapabilitiesContext.Provider value={NO_DICTATION_INFO}>
        <ComposerMicButton onTranscript={vi.fn()} />
      </CapabilitiesContext.Provider>,
    );
    const button = screen.getByRole("button", { name: "Voice dictation" });
    fireEvent.click(button);
    await act(async () => handlers.error?.({ error: "network" }));
    expect(sessionStartMock).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent("Could not start dictation. Try again.");
  });

  it("cancels the session when the composer goes disabled mid-take", async () => {
    const { rerender } = renderServerMode();
    await clickMic();

    rerender(
      <CapabilitiesContext.Provider value={DICTATION_INFO}>
        <ComposerMicButton onTranscript={vi.fn()} disabled />
      </CapabilitiesContext.Provider>,
    );
    expect(sessionCancelMock).toHaveBeenCalledTimes(1);
  });

  it("fires onVoiceStart when a server take begins", async () => {
    const onVoiceStart = vi.fn();
    renderServerMode({ onVoiceStart });
    await clickMic();
    expect(onVoiceStart).toHaveBeenCalledTimes(1);
  });

  it("in Electron goes straight to the server, skipping the doomed Web Speech take", async () => {
    // Electron HAS a SpeechRecognition constructor but no backend: a Web Speech
    // take always fails with "network" and only then falls back, a visible ~1s
    // stall. With the server available the button must skip it entirely.
    (window as unknown as Record<string, unknown>).omnigentDesktop = { kind: "electron" };
    try {
      render(
        <CapabilitiesContext.Provider value={DICTATION_INFO}>
          <ComposerMicButton onTranscript={vi.fn()} />
        </CapabilitiesContext.Provider>,
      );
      await clickMic();

      // Server path taken directly; the Web Speech recognizer never started.
      expect(sessionStartMock).toHaveBeenCalledTimes(1);
      expect(startSpy).not.toHaveBeenCalled();
      expect(screen.getByRole("button", { name: "Voice dictation" })).toHaveAttribute(
        "aria-pressed",
        "true",
      );
    } finally {
      delete (window as unknown as Record<string, unknown>).omnigentDesktop;
    }
  });

  it("Enter while listening ends the server take via stop (keeps the tail)", async () => {
    const onVoiceDiscard = vi.fn();
    sessionStopMock = vi.fn(async () => "tail words");
    renderServerMode({ onVoiceDiscard });
    await clickMic();

    const e = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    await act(async () => {
      window.dispatchEvent(e);
    });

    expect(sessionStopMock).toHaveBeenCalledTimes(1);
    expect(sessionCancelMock).not.toHaveBeenCalled();
    expect(onVoiceDiscard).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(true);
  });

  it("Esc while listening cancels the server take and discards, dropping late results", async () => {
    const onTranscript = vi.fn();
    const onInterim = vi.fn();
    const onVoiceDiscard = vi.fn();
    renderServerMode({ onTranscript, onInterim, onVoiceDiscard });
    await clickMic();

    const e = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    act(() => {
      window.dispatchEvent(e);
    });

    expect(sessionCancelMock).toHaveBeenCalledTimes(1);
    expect(sessionStopMock).not.toHaveBeenCalled();
    expect(onVoiceDiscard).toHaveBeenCalledTimes(1);
    expect(e.defaultPrevented).toBe(true);

    // A partial/final racing in after the cancel must not repopulate the
    // composer the parent just reverted.
    onInterim.mockClear();
    onTranscript.mockClear();
    act(() => {
      sessionEvents?.onPartial("late partial");
      sessionEvents?.onFinal("late final");
    });
    expect(onInterim).not.toHaveBeenCalled();
    expect(onTranscript).not.toHaveBeenCalled();
  });
});
