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
import type { DictationSessionEvents } from "@/lib/dictation";
import { ComposerMicButton } from "./ComposerMicButton";

// Controllable DictationSession stand-in for the server-mode tests. The
// factory reads the mutable spies at call time, so each test installs its
// own behavior in beforeEach.
type SessionStub = { stop: () => Promise<string>; cancel: () => void };
let sessionStartMock: Mock<(events: DictationSessionEvents) => Promise<SessionStub>>;
let sessionStopMock: Mock<() => Promise<string>>;
let sessionCancelMock: Mock<() => void>;
let sessionEvents: DictationSessionEvents | null;

vi.mock("@/lib/dictation", () => {
  class DictationBusyError extends Error {}
  return {
    DictationBusyError,
    DictationSession: {
      start: (events: DictationSessionEvents) => sessionStartMock(events),
    },
  };
});

function installDictationSession() {
  sessionEvents = null;
  sessionStopMock = vi.fn(async () => "");
  sessionCancelMock = vi.fn();
  sessionStartMock = vi.fn(async (events: DictationSessionEvents) => {
    sessionEvents = events;
    return { stop: sessionStopMock, cancel: sessionCancelMock };
  });
}

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
  installDictationSession();
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
  harness_install_enabled: false,
  installable_harnesses: [],
  dictation_available: true,
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
  it("renders the button when the server advertises dictation", () => {
    renderServerMode();
    expect(screen.getByRole("button", { name: "Voice dictation" })).toBeInTheDocument();
  });

  it("renders nothing when neither Web Speech nor the server can help", () => {
    const { container } = renderServerMode({}, { ...DICTATION_INFO, dictation_available: false });
    expect(container).toBeEmptyDOMElement();
  });

  it("starts a session on click and reflects the recording state", async () => {
    renderServerMode();
    await clickMic();
    expect(sessionStartMock).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Voice dictation" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
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

  it("surfaces mic permission denial in the tooltip", async () => {
    sessionStartMock = vi.fn(async () => {
      throw new DOMException("denied", "NotAllowedError");
    });
    renderServerMode();
    await clickMic();
    expect(screen.getByRole("button", { name: "Voice dictation" })).toHaveAttribute(
      "title",
      "Microphone permission denied",
    );
  });

  it("a mid-take transport error resets state and reports unavailable", async () => {
    const onInterim = vi.fn();
    renderServerMode({ onInterim });
    await clickMic();

    act(() => sessionEvents?.onError("dictation failed"));
    const button = screen.getByRole("button", { name: "Voice dictation" });
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(button).toHaveAttribute("title", "Dictation unavailable");
    expect(onInterim).toHaveBeenCalledWith("");
  });

  it("falls back to server dictation when Web Speech dies with a network error", async () => {
    // Electron / plain Chromium: the SpeechRecognition constructor exists
    // (so Web Speech is picked first) but its cloud backend rejects the
    // build at runtime with "network". The take must fall back to server
    // dictation so the user's click still lands.
    const onInterim = vi.fn();
    render(
      <CapabilitiesContext.Provider value={DICTATION_INFO}>
        <ComposerMicButton onTranscript={vi.fn()} onInterim={onInterim} />
      </CapabilitiesContext.Provider>,
    );
    const button = screen.getByRole("button", { name: "Voice dictation" });

    fireEvent.click(button);
    expect(startSpy).toHaveBeenCalledTimes(1);
    await act(async () => handlers.error?.({ error: "network" }));

    // The take restarted on the server path, with no error tooltip for
    // the silent switch, and partials flow.
    expect(sessionStartMock).toHaveBeenCalledTimes(1);
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

  it("reports a busy server distinctly from a broken one", async () => {
    const { DictationBusyError } = await import("@/lib/dictation");
    sessionStartMock = vi.fn(async () => {
      throw new DictationBusyError("at capacity");
    });
    renderServerMode();
    await clickMic();
    expect(screen.getByRole("button", { name: "Voice dictation" })).toHaveAttribute(
      "title",
      "Dictation is busy — try again shortly",
    );
  });

  it("keeps the plain error path when the server offers no dictation", async () => {
    render(
      <CapabilitiesContext.Provider value={{ ...DICTATION_INFO, dictation_available: false }}>
        <ComposerMicButton onTranscript={vi.fn()} />
      </CapabilitiesContext.Provider>,
    );
    const button = screen.getByRole("button", { name: "Voice dictation" });
    fireEvent.click(button);
    await act(async () => handlers.error?.({ error: "network" }));
    expect(sessionStartMock).not.toHaveBeenCalled();
    expect(button).toHaveAttribute("title", "Dictation unavailable");
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
