// Per-device switches that pick the engine behind a surface, such as the editor
// behind the file viewer. Each switch keeps the user's stored intent (Automatic
// or a pinned engine) apart from the engine that results, so moving a switch's
// `automaticEngine` moves everyone still on Automatic.

import { createStoredChoicePreference, type StoredPreference } from "@/lib/storedChoicePreference";

/** The engines the file viewer and its diff view can run on. */
export type ViewerEngine = "monaco";

/** What the user chose. Nothing stored means Automatic. */
export type EngineIntent<Engine extends string> = "automatic" | Engine;

export const AUTOMATIC_ENGINE_INTENT = "automatic" satisfies EngineIntent<string>;

/** One surface's engine switch. */
export interface EngineSwitch<Engine extends string> {
  /** The engines this surface can run on. */
  readonly engines: readonly Engine[];
  /** The engine Automatic resolves to. */
  readonly automaticEngine: Engine;
  /**
   * Whether a pinned engine applies yet. Until then the switch always runs its
   * Automatic engine, so a stray stored value can't select an unfinished one.
   */
  readonly offered: boolean;
  /** The stored intent, kept on this device only. */
  readonly intent: StoredPreference<EngineIntent<Engine>>;
}

export function createEngineSwitch<Engine extends string>(options: {
  storageKey: string;
  engines: readonly Engine[];
  automaticEngine: NoInfer<Engine>;
  offered: boolean;
}): EngineSwitch<Engine> {
  const intents: EngineIntent<Engine>[] = [AUTOMATIC_ENGINE_INTENT, ...options.engines];
  return {
    engines: options.engines,
    automaticEngine: options.automaticEngine,
    offered: options.offered,
    intent: createStoredChoicePreference(options.storageKey, intents, AUTOMATIC_ENGINE_INTENT),
  };
}

/** The engine a surface runs on for `intent`. */
export function engineFor<Engine extends string>(
  engineSwitch: EngineSwitch<Engine>,
  intent: EngineIntent<Engine>,
): Engine {
  if (!engineSwitch.offered || intent === AUTOMATIC_ENGINE_INTENT) {
    return engineSwitch.automaticEngine;
  }
  return intent;
}

/** The engine this surface runs on right now. */
export function readEngine<Engine extends string>(engineSwitch: EngineSwitch<Engine>): Engine {
  return engineFor(engineSwitch, engineSwitch.intent.read());
}

/** Live engine for this surface, following changes made in other windows. */
export function useEngine<Engine extends string>(engineSwitch: EngineSwitch<Engine>): Engine {
  return engineFor(engineSwitch, engineSwitch.intent.use());
}

/** Opened files: Monaco's viewer and editor. */
export const FILE_VIEWER_ENGINE = createEngineSwitch<ViewerEngine>({
  storageKey: "omnigent:file-viewer-engine",
  engines: ["monaco"],
  automaticEngine: "monaco",
  offered: false,
});

/** A changed file opened in the file viewer's diff view: Monaco's diff editor. */
export const DIFF_VIEWER_ENGINE = createEngineSwitch<ViewerEngine>({
  storageKey: "omnigent:diff-viewer-engine",
  engines: ["monaco"],
  automaticEngine: "monaco",
  offered: false,
});
