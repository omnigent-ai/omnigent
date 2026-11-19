import type { ComponentProps } from "react";
import type { SessionHarnessPicker } from "@/components/composer/SessionHarnessPicker";
import {
  composerHarnessLabel,
  modelResetSupported,
  useSessionField,
  type SessionPickerInputs,
} from "@/components/composer/sessionPickerInputs";
import { useBrainHarnessLabels } from "@/lib/agentLabels";

/** The picker props a session's inputs and gates determine; the caller adds its busy state and hotkey. */
export type SessionHarnessPickerConfig = Omit<
  ComponentProps<typeof SessionHarnessPicker>,
  "appearance" | "busy" | "busyRef" | "setBusy" | "openNonce" | "modelPickerShortcutEnabled"
>;

/** What blocks a session's picker, supplied by the caller that knows the viewer and session. */
export interface SessionHarnessPickerGates {
  /** The viewer's permission level; 1 is read-only. */
  permissionLevel: number | null;
  /** A structural read-only reason, such as a read-only sub-agent. */
  readOnlyReason: string | null;
  sendDisabledReason: string | null;
  /** The session can't be reached, so a config change can't reach its runner. */
  unreachable: boolean;
}

/**
 * Whether the composer refuses input: a read-only grant or a structurally
 * non-interactive session. A temporary send block keeps the level-1 fallback
 * of a still-starting client-only conversation editable.
 */
export function isComposerReadOnly(
  permissionLevel: number | null,
  readOnlyReason: string | null,
  sendDisabledReason: string | null,
): boolean {
  return readOnlyReason !== null || (permissionLevel === 1 && sendDisabledReason === null);
}

/**
 * The session harness picker's props for one session, shared by the full-screen
 * composer and grid cells. `sessionId` undefined reads and writes the active
 * conversation; a fixed id reads and writes that session.
 *
 * `inputs` is the result of `useSessionPickerInputs` for the same session.
 * `agentName` is the bound agent, which a sub-agent head's name overrides.
 */
export function useSessionHarnessPickerProps({
  sessionId,
  hostId,
  inputs,
  agentName,
  wrapperLabel,
  isNativeWrapper,
  runnerOnline,
  gates,
}: {
  sessionId: string | undefined;
  hostId: string | null | undefined;
  inputs: SessionPickerInputs;
  agentName: string | null;
  wrapperLabel: string | null;
  isNativeWrapper: boolean;
  runnerOnline: boolean | undefined;
  gates: SessionHarnessPickerGates;
}): SessionHarnessPickerConfig {
  const brainHarnessLabels = useBrainHarnessLabels();
  const sessionHarness = useSessionField(sessionId, (state) => state.sessionHarness);
  const subAgentName = useSessionField(sessionId, (state) => state.subAgentName);
  const nativeTerminalSession = useSessionField(
    sessionId,
    (state) => state.isNativeTerminalSession,
  );
  const headName = subAgentName ?? agentName;
  const isReadOnly = isComposerReadOnly(
    gates.permissionLevel,
    gates.readOnlyReason,
    gates.sendDisabledReason,
  );
  return {
    sessionId,
    agentName: headName,
    // A sub-agent head shows its own family; a native sub-agent's name is vendor-side, so the wrapper wins.
    harnessLabel: composerHarnessLabel(
      inputs.modelPickerKind,
      headName,
      sessionHarness,
      brainHarnessLabels,
      wrapperLabel,
    ),
    showModels: inputs.showModels,
    showEffort: inputs.showEffort,
    effortLevels: inputs.effortLevels,
    modelPickerKind: inputs.modelPickerKind,
    supportsModelReset: modelResetSupported({
      inferenceConfigured: inputs.inferenceConfigured,
      modelPickerKind: inputs.modelPickerKind,
      isNativeWrapper,
    }),
    codexModelOptions: inputs.codexModelOptions,
    inferenceConfigured: inputs.inferenceConfigured,
    inferenceError: inputs.inferenceError,
    modelsFromLaunchPreview: inputs.modelsFromLaunchPreview,
    modelLabelOptions: inputs.modelLabelOptions,
    modelLabelHostId: hostId,
    costRoutingEligible: inputs.costRoutingEligible,
    // A native session's first model pick wakes its runner, which can take a while.
    modelPickWakesSession: nativeTerminalSession && runnerOnline === false,
    // Config changes persist server-side and apply on the next wake or turn, so the
    // gear stays live wherever a message could be sent. Read-only viewers and
    // unreachable sessions get an inert gear.
    disabled: isReadOnly || gates.unreachable,
  };
}
