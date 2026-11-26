import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import * as CodeMirrorModule from "./codeMirrorComposerModule";
import {
  codeMirrorComposerLoad,
  loadCodeMirrorComposer,
  resetCodeMirrorComposerLoadForTests,
} from "./codeMirrorComposerLoader";
import {
  classifyComposerLoadFailure,
  composerEngineReason,
  composerEngineReportFor,
  HELD_SWITCH_REPORT_DELAY_MS,
  useComposerEngineReport,
  type ComposerEngineReport,
} from "./composerEngineReport";
import type { ComposerInputEngine } from "./composerInput";

const mobile = vi.hoisted(() => ({ current: false }));
vi.mock("@/hooks/useIsMobileViewport", () => ({
  useIsMobileViewport: () => mobile.current,
}));

function flagWrapper(enabled: boolean) {
  const info = { ...FALLBACK_SERVER_INFO, features: { chat_composer_codemirror: enabled } };
  return function Wrapper({ children }: { children: ReactNode }) {
    return <CapabilitiesProvider info={info}>{children}</CapabilitiesProvider>;
  };
}

afterEach(() => {
  mobile.current = false;
  resetCodeMirrorComposerLoadForTests();
  vi.useRealTimers();
});

describe("classifyComposerLoadFailure", () => {
  it("names a chunk that couldn't be fetched, one that couldn't run, and anything else", () => {
    for (const message of [
      "Failed to fetch dynamically imported module: https://app.example.com/a.js",
      "Importing a module script failed.",
      "error loading dynamically imported module",
    ]) {
      expect(classifyComposerLoadFailure(new TypeError(message))).toBe("chunk_fetch");
    }
    expect(classifyComposerLoadFailure(new SyntaxError("Unexpected token"))).toBe("chunk_eval");
    expect(classifyComposerLoadFailure(new ReferenceError("x is not defined"))).toBe("chunk_eval");
    expect(classifyComposerLoadFailure(new TypeError("undefined is not a function"))).toBe("other");
    expect(classifyComposerLoadFailure("offline")).toBe("other");
  });
});

describe("composerEngineReason", () => {
  it("names the first thing that keeps the composer off CodeMirror", () => {
    expect(composerEngineReason(false, true, "failed")).toBe("flag_off");
    expect(composerEngineReason(true, true, "ready")).toBe("mobile");
    expect(composerEngineReason(true, false, "failed")).toBe("load_failed");
    expect(composerEngineReason(true, false, "idle")).toBe("loading");
    expect(composerEngineReason(true, false, "loading")).toBe("loading");
    expect(composerEngineReason(true, false, "ready")).toBe("ready");
  });
});

describe("composerEngineReportFor", () => {
  it("carries the failure class only for a failed load", () => {
    const failed = composerEngineReportFor({
      surface: "main",
      flagOn: true,
      mobile: false,
      load: { status: "failed", error: new TypeError("Importing a module script failed.") },
      rendered: "textarea",
    });
    expect(failed).toEqual({
      surface: "main",
      load: "failed",
      failure: "chunk_fetch",
      requested: "textarea",
      rendered: "textarea",
      reason: "load_failed",
    });
    const loading = composerEngineReportFor({
      surface: "side_chat",
      flagOn: true,
      mobile: false,
      load: { status: "loading" },
      rendered: "textarea",
    });
    expect(loading).not.toHaveProperty("failure");
    expect(loading.reason).toBe("loading");
  });
});

describe("useComposerEngineReport", () => {
  // No default for `send`: an explicit undefined must reach the hook as a missing sink.
  function renderReport(
    flagOn: boolean,
    initialRendered: ComposerInputEngine,
    send: ((report: ComposerEngineReport) => void) | undefined,
  ) {
    return renderHook(
      ({ rendered }: { rendered: ComposerInputEngine }) =>
        useComposerEngineReport("main", rendered, send),
      { wrapper: flagWrapper(flagOn), initialProps: { rendered: initialRendered } },
    );
  }

  it("sends nothing with the flag off, or without a sink", async () => {
    const send = vi.fn();
    renderReport(false, "textarea", send);
    expect(send).not.toHaveBeenCalled();

    // A missing sink is never called: nothing throws and nothing warns, settled or held.
    resetCodeMirrorComposerLoadForTests(async () => CodeMirrorModule);
    loadCodeMirrorComposer();
    await waitFor(() => expect(codeMirrorComposerLoad().status).toBe("ready"));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.useFakeTimers();
    const { rerender } = renderReport(true, "textarea", undefined);
    act(() => vi.advanceTimersByTime(HELD_SWITCH_REPORT_DELAY_MS));
    rerender({ rendered: "codemirror" });
    expect(warn).not.toHaveBeenCalled();
    warn.mockRestore();
  });

  it("sends once per change as the editor loads, then once it renders", async () => {
    let finishLoad: (module: typeof CodeMirrorModule) => void = () => {};
    const pendingModule = new Promise<typeof CodeMirrorModule>((resolve) => {
      finishLoad = resolve;
    });
    resetCodeMirrorComposerLoadForTests(() => pendingModule);
    const send = vi.fn();
    const { rerender } = renderReport(true, "textarea", send);
    expect(send).toHaveBeenLastCalledWith(expect.objectContaining({ reason: "loading" }));
    act(() => loadCodeMirrorComposer());
    // Idle to loading changes the load code, but not the reason or the engines.
    expect(send).toHaveBeenCalledTimes(2);
    rerender({ rendered: "textarea" });
    expect(send).toHaveBeenCalledTimes(2);

    act(() => finishLoad(CodeMirrorModule));
    await waitFor(() => expect(codeMirrorComposerLoad().status).toBe("ready"));
    rerender({ rendered: "codemirror" });
    expect(send).toHaveBeenLastCalledWith({
      surface: "main",
      load: "ready",
      requested: "codemirror",
      rendered: "codemirror",
      reason: "ready",
    });
  });

  it("reports a switch only once it has been held for a while", async () => {
    resetCodeMirrorComposerLoadForTests(async () => CodeMirrorModule);
    loadCodeMirrorComposer();
    await waitFor(() => expect(codeMirrorComposerLoad().status).toBe("ready"));
    vi.useFakeTimers();
    const send = vi.fn();
    const { rerender } = renderReport(true, "textarea", send);
    act(() => vi.advanceTimersByTime(HELD_SWITCH_REPORT_DELAY_MS - 1));
    expect(send).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1));
    expect(send).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ requested: "codemirror", rendered: "textarea" }),
    );
    rerender({ rendered: "codemirror" });
    expect(send).toHaveBeenLastCalledWith(expect.objectContaining({ rendered: "codemirror" }));

    // A switch that finishes before the delay sends only where it settles.
    send.mockClear();
    const quick = renderReport(true, "textarea", send);
    quick.rerender({ rendered: "codemirror" });
    act(() => vi.advanceTimersByTime(HELD_SWITCH_REPORT_DELAY_MS));
    expect(send).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ rendered: "codemirror" }),
    );
  });

  it("keeps a held switch's deadline when the sink changes, and delivers to the latest one", async () => {
    resetCodeMirrorComposerLoadForTests(async () => CodeMirrorModule);
    loadCodeMirrorComposer();
    await waitFor(() => expect(codeMirrorComposerLoad().status).toBe("ready"));
    vi.useFakeTimers();
    const sinks = [vi.fn(), vi.fn(), vi.fn(), vi.fn(), vi.fn()];
    // An inline sink: a new callback on every re-render, one a second.
    const { rerender } = renderHook(
      ({ send }: { send: (report: ComposerEngineReport) => void }) =>
        useComposerEngineReport("main", "textarea", (report) => send(report)),
      { wrapper: flagWrapper(true), initialProps: { send: sinks[0]! } },
    );
    for (const sink of sinks.slice(1)) {
      act(() => vi.advanceTimersByTime(1_000));
      rerender({ send: sink });
    }
    act(() => vi.advanceTimersByTime(HELD_SWITCH_REPORT_DELAY_MS - 4_000));
    expect(sinks.slice(0, -1).every((sink) => sink.mock.calls.length === 0)).toBe(true);
    expect(sinks.at(-1)).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ requested: "codemirror", rendered: "textarea" }),
    );
  });

  it("reports a mobile viewport, and a sink that throws changes nothing", () => {
    mobile.current = true;
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const send = vi.fn(() => {
      throw new Error("shell went away");
    });
    expect(() => renderReport(true, "textarea", send)).not.toThrow();
    expect(send).toHaveBeenCalledWith(expect.objectContaining({ reason: "mobile" }));
    expect(warn).toHaveBeenCalledOnce();
    warn.mockRestore();
  });
});
