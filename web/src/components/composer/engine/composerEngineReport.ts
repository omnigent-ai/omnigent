// Which engine a chat composer asked for, which one renders and why, as closed codes, so a
// composer that stays a textarea with `chat_composer_codemirror` on explains itself. Passive:
// it reads what the composer already decided and never starts a load of its own.

import { useEffect, useRef } from "react";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import type { CodeMirrorComposerLoad } from "./codeMirrorComposerLoader";
import { effectiveComposerEngine, useCodeMirrorComposerLoad } from "./composerEnginePolicy";
import type { ComposerInputEngine } from "./composerInput";

export type ComposerEngineSurface = "main" | "side_chat" | "new_chat";

/** Why the composer asks for the engine it does. */
export type ComposerEngineReason = "ready" | "flag_off" | "mobile" | "loading" | "load_failed";

export type ComposerLoadFailure = "chunk_fetch" | "chunk_eval" | "other";

export interface ComposerEngineReport {
  surface: ComposerEngineSurface;
  load: CodeMirrorComposerLoad["status"];
  /** Set only for a failed load. */
  failure?: ComposerLoadFailure;
  requested: ComposerInputEngine;
  rendered: ComposerInputEngine;
  reason: ComposerEngineReason;
}

export type ComposerEngineReportSink = (report: ComposerEngineReport) => void;

// What browsers say when a lazily loaded module couldn't be fetched (Chromium, Safari, Firefox).
const CHUNK_FETCH_FAILURE =
  /failed to fetch dynamically imported module|importing a module script failed|error loading dynamically imported module/i;

/** A failed load as a closed code; anything not clearly a fetch or evaluation failure is "other". */
export function classifyComposerLoadFailure(error: unknown): ComposerLoadFailure {
  if (error instanceof TypeError && CHUNK_FETCH_FAILURE.test(error.message)) return "chunk_fetch";
  if (error instanceof SyntaxError || error instanceof ReferenceError) return "chunk_eval";
  return "other";
}

/** The first thing that keeps the composer off CodeMirror, or "ready" when nothing does. */
export function composerEngineReason(
  flagOn: boolean,
  mobile: boolean,
  load: CodeMirrorComposerLoad["status"],
): ComposerEngineReason {
  if (!flagOn) return "flag_off";
  if (mobile) return "mobile";
  if (load === "failed") return "load_failed";
  return load === "ready" ? "ready" : "loading";
}

/** The report for one composer, from what it already knows. */
export function composerEngineReportFor({
  surface,
  flagOn,
  mobile,
  load,
  rendered,
}: {
  surface: ComposerEngineSurface;
  flagOn: boolean;
  mobile: boolean;
  load: CodeMirrorComposerLoad;
  rendered: ComposerInputEngine;
}): ComposerEngineReport {
  return {
    surface,
    load: load.status,
    ...(load.status === "failed" ? { failure: classifyComposerLoadFailure(load.error) } : {}),
    requested: effectiveComposerEngine(flagOn, mobile, load.status),
    rendered,
    reason: composerEngineReason(flagOn, mobile, load.status),
  };
}

/** How long a switch may wait (on IME, dictation or a selection drag) before that is reported. */
export const HELD_SWITCH_REPORT_DELAY_MS = 5_000;

function reportKey(report: ComposerEngineReport): string {
  return [
    report.surface,
    report.load,
    report.failure ?? "",
    report.requested,
    report.rendered,
    report.reason,
  ].join("|");
}

/**
 * Send `surface`'s report to `send` whenever it changes, while `chat_composer_codemirror` is
 * on. With the flag off, or without a sink, nothing is sent.
 */
export function useComposerEngineReport(
  surface: ComposerEngineSurface,
  rendered: ComposerInputEngine,
  send: ComposerEngineReportSink | undefined,
): void {
  const flagOn = isFeatureEnabled(useServerInfo(), "chat_composer_codemirror");
  const mobile = useIsMobileViewport();
  const load = useCodeMirrorComposerLoad();
  const lastSent = useRef<string | null>(null);
  // The latest sink, so a caller's new callback each render neither restarts a held
  // switch's deadline nor gets a report meant for it delivered to an old one.
  const latestSend = useRef(send);
  latestSend.current = send;
  const hasSink = send !== undefined;
  useEffect(() => {
    if (!flagOn || !hasSink) return;
    const report = composerEngineReportFor({ surface, flagOn, mobile, load, rendered });
    const key = reportKey(report);
    if (key === lastSent.current) return;
    const deliver = () => {
      const sink = latestSend.current;
      if (!sink) return;
      lastSent.current = key;
      try {
        sink(report);
      } catch (error: unknown) {
        console.warn("Could not report the chat composer engine.", error);
      }
    };
    // A switch renders within a commit unless something holds it; only a long hold is news.
    if (report.requested === report.rendered) {
      deliver();
      return;
    }
    const timer = window.setTimeout(deliver, HELD_SWITCH_REPORT_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [flagOn, mobile, load, surface, rendered, hasSink]);
}
