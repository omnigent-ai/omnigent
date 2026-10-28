import type { NativeModelOption } from "./types";
import { nativeCodingAgentForHarness } from "./nativeCodingAgents";
import { codexEffortLevelsForModel, findNativeModelOption } from "./codexNativeModels";
import { effortLevelsForFamily, supportsEffortControl } from "./sessionCapabilities";

const EFFORT_LEVELS = ["low", "medium", "high"] as const;

/** Anthropic-side efforts for claude-native sessions (matches ANTHROPIC_EFFORTS in reasoning_effort.py). */
const CLAUDE_NATIVE_EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"] as const;

/** Pi thinking ladder (matches PI_EFFORTS in reasoning_effort.py; ``ultra`` aliases to ``max`` on Pi so omitted). */
const PI_NATIVE_EFFORT_LEVELS = [
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
] as const;

export type NativeModelPickerKind =
  "claude" | "codex" | "cursor" | "kiro" | "opencode" | "pi" | "devin" | "acp" | "configured";

/** Preserve inherited effort; clear an explicit choice that the new model cannot use. */
export function effortAfterModelChange(
  effort: string | null,
  supportedEfforts: readonly string[],
): string | null {
  return effort !== null && supportedEfforts.includes(effort) ? effort : null;
}

/** Claude/Codex reports win over requests that the runner may not have applied. */
export function selectedModelForPicker(
  kind: NativeModelPickerKind | null,
  options: readonly NativeModelOption[],
  requestedModel: string | null,
  reportedModel: string | null,
): string | null {
  if (kind === "claude" || kind === "codex") {
    const model = reportedModel || requestedModel;
    return model ? (findNativeModelOption(options, model)?.id ?? model) : null;
  }
  return requestedModel ?? (kind === "configured" ? reportedModel : null);
}

/** Resolve unlabeled native sessions from their harness; children keep their own capabilities. */
function effectiveWrapperLabel(
  conv:
    | {
        labels?: Record<string, string | null> | null;
        harness?: string | null;
        parentSessionId?: string | null;
      }
    | null
    | undefined,
): string | undefined {
  const label = conv?.labels?.["omnigent.wrapper"];
  if (label != null) return label;
  if (conv?.parentSessionId != null) return undefined;
  return nativeCodingAgentForHarness(conv?.harness)?.wrapperLabel;
}

/** Declared effort families apply to SDK harnesses; native bridges define their own support. */
export function sdkEffortFamilyForHarness(
  harness: string | null | undefined,
  families: Readonly<Record<string, string>>,
): string | null {
  return harness && !nativeCodingAgentForHarness(harness) ? (families[harness] ?? null) : null;
}

export function effortLevelsForConv(
  conv:
    | {
        labels?: Record<string, string | null> | null;
        harness?: string | null;
        parentSessionId?: string | null;
      }
    | null
    | undefined,
  codexModelOptions: readonly NativeModelOption[] = [],
  currentModel: string | null = null,
  effortFamily: string | null = null,
): readonly string[] {
  switch (effectiveWrapperLabel(conv)) {
    case "claude-code-native-ui":
      return CLAUDE_NATIVE_EFFORT_LEVELS;
    case "devin-native-ui":
    case "codex-native-ui":
      // Both catalogs declare effort per model; Devin encodes it as a model variant.
      return codexEffortLevelsForModel(codexModelOptions, currentModel);
    case "pi-native-ui":
      return PI_NATIVE_EFFORT_LEVELS;
    default:
      // A non-native harness offers its declared family's ladder (claude-sdk →
      // anthropic, codex → openai); with no family the shared default holds.
      return effortFamily ? effortLevelsForFamily(effortFamily) : EFFORT_LEVELS;
  }
}

/**
 * Which native model picker should be visible for *conv*?
 *
 * Gated on the wrapper label, not `omnigent.ui === "terminal"`:
 * other terminal-first wrappers may not be Claude/Codex-native (see
 * `TerminalFirstContext.tsx`).
 */
export function modelPickerKindForConv(
  conv:
    | {
        labels?: Record<string, string | null> | null;
        harness?: string | null;
        parentSessionId?: string | null;
        inferenceConfigured?: boolean;
      }
    | null
    | undefined,
  modelOptions: readonly NativeModelOption[] = [],
): NativeModelPickerKind | null {
  switch (effectiveWrapperLabel(conv)) {
    case "claude-code-native-ui":
      return "claude";
    case "codex-native-ui":
      return "codex";
    case "cursor-native-ui":
      return "cursor";
    case "kiro-native-ui":
      // Launch-only model selection: kiro applies ``--model`` at launch. Unlike
      // cursor/opencode there is no terminal->web model mirror, so the picker
      // reflects the pre-launch ``model_override`` selection.
      return "kiro";
    case "opencode-native-ui":
      // Like cursor: a vendor-owns-model wrapper that mirrors its live TUI
      // model into the session ``model_override`` (the forwarder's terminal→web
      // mirror), so the picker surfaces that as the live model.
      return "opencode";
    case "devin-native-ui":
      // Like opencode/pi: Devin mirrors its live model into ``model_override``
      // (the executor types ``/model`` when a routed model changes).
      return "devin";
    case "pi-native-ui":
      // Pi applies switches through setModel and mirrors /model picks to model_override.
      return "pi";
    default:
      if (conv?.inferenceConfigured) return "configured";
      // Generic ACP sessions carry no wrapper label; the server canonicalizes
      // ``acp:<slug>`` ids to "acp" in the snapshot's harness field.
      if (conv?.harness === "acp" && modelOptions.length > 1) return "acp";
      return null;
  }
}

export function shouldShowModelPicker(
  conv:
    | {
        labels?: Record<string, string | null> | null;
        harness?: string | null;
        inferenceConfigured?: boolean;
      }
    | null
    | undefined,
  modelOptions: readonly NativeModelOption[] = [],
): boolean {
  return modelPickerKindForConv(conv, modelOptions) !== null;
}

/**
 * True when effort controls should be visible.
 *
 * :param conv: Session or sidebar row carrying labels. ``null`` or missing
 *     labels fail closed.
 * :returns: True only when the session supports Web UI effort controls.
 */
export function shouldShowEffortPicker(
  conv:
    | {
        labels?: Record<string, string | null> | null;
        harness?: string | null;
        parentSessionId?: string | null;
      }
    | null
    | undefined,
  effortFamily: string | null = null,
): boolean {
  return supportsEffortControl(conv, effortFamily);
}
