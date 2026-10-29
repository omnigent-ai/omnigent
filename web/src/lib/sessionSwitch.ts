// Pure rules and call plan for the "Switch model or account" dialog.

import { CLAUDE_NATIVE_EFFORTS, PI_NATIVE_EFFORTS } from "@/components/HarnessConfigControls";
import { findNativeModelOption } from "@/lib/codexNativeModels";
import { nativeCodingAgentForHarness, nativeCodingAgentForWrapper } from "@/lib/nativeCodingAgents";
import type { NativeModelOption, Session } from "@/lib/types";

/** Native harnesses whose model and effort the dialog can change in place. */
export type SwitchHarness = "claude-native" | "codex-native";

/** The switchable harness a session runs, or null; a session keeps its harness. */
export function switchableHarness(harness: string | null | undefined): SwitchHarness | null {
  const canonical = nativeCodingAgentForHarness(harness)?.harness;
  return canonical === "claude-native" || canonical === "codex-native" ? canonical : null;
}

/** The session's switchable harness from its harness, else its wrapper label. */
export function sessionSwitchHarness(
  session: Pick<Session, "harness" | "labels"> | null | undefined,
): SwitchHarness | null {
  if (!session) return null;
  const fromHarness = switchableHarness(session.harness);
  if (fromHarness !== null) return fromHarness;
  return switchableHarness(
    nativeCodingAgentForWrapper(session.labels?.["omnigent.wrapper"])?.harness,
  );
}

/** User-facing harness name, e.g. ``"Claude Code"``. */
export function harnessLabel(harness: string): string {
  return nativeCodingAgentForHarness(harness)?.displayName ?? harness;
}

/** Effort ids for a catalog model; Claude rows without a list use the composer's ladder. */
export function effortChoicesForModel(
  harness: SwitchHarness,
  option: NativeModelOption | null,
): string[] {
  if (option === null) return [];
  const listed = (option.supportedReasoningEfforts ?? [])
    .map((entry) => entry.reasoningEffort)
    .filter((effort): effort is string => typeof effort === "string" && effort.length > 0);
  if (listed.length > 0) return Array.from(new Set(listed));
  return harness === "claude-native" ? CLAUDE_NATIVE_EFFORTS.map((entry) => entry.value) : [];
}

const EFFORT_LABELS = new Map(
  [...PI_NATIVE_EFFORTS, ...CLAUDE_NATIVE_EFFORTS].map((entry) => [entry.value, entry.label]),
);

/** Display label for an effort id; null means the model's default. */
export function effortLabel(effort: string | null): string {
  if (effort === null) return "Default";
  return EFFORT_LABELS.get(effort) ?? effort;
}

/** Display label for a catalog row. */
export function modelOptionLabel(option: NativeModelOption): string {
  return option.displayName ?? option.id;
}

/** The stored model request, else the reported model; seeds the draft only. */
export function sessionModelId(
  session: Pick<Session, "modelOverride" | "llmModel">,
): string | null {
  return session.modelOverride ?? session.llmModel ?? null;
}

/** Statuses during which the session must not be detached or rebound. */
export function sessionIsBusy(session: Pick<Session, "status">): boolean {
  return session.status === "running" || session.status === "waiting";
}

export interface SwitchDraft {
  accountId: string;
  hostId: string;
  harness: SwitchHarness;
  /** Requested model id as the user picked it (catalog id or raw current id). */
  model: string | null;
  /** Requested effort; null is the model's default. */
  effort: string | null;
  /** Absolute directory for a launch; ignored when no launch is needed. */
  workspace: string | null;
}

export interface SessionUpdate {
  runnerId?: string;
  modelOverride?: string | null;
  reasoningEffort?: string | null;
  costControlModeOverride?: "on" | "off" | null;
  silent?: boolean;
}

export type SwitchStep =
  | { kind: "begin-transition"; draft: SwitchDraft }
  | { kind: "update"; updates: SessionUpdate }
  | { kind: "launch"; hostId: string; workspace: string };

export type SwitchSnapshot = Pick<
  Session,
  | "hostId"
  | "runnerId"
  | "harness"
  | "modelOverride"
  | "llmModel"
  | "reasoningEffort"
  | "status"
  | "costControlModeOverride"
  | "accountTransition"
>;

/** Whether Smart Routing currently picks the model per turn. */
export function routingOn(snapshot: Pick<Session, "costControlModeOverride">): boolean {
  return snapshot.costControlModeOverride === "on";
}

/**
 * Whether the session reports the requested model: ``applied``, ``pending``
 * (nothing reported yet or still launching) or ``unapplied`` (idle, different).
 */
export type ModelApplyState = "applied" | "pending" | "unapplied";

export function modelApplyState(
  snapshot: Pick<Session, "llmModel" | "status">,
  catalog: readonly NativeModelOption[],
  requestedId: string,
): ModelApplyState {
  const reported = snapshot.llmModel ?? null;
  if (reported === null) return "pending";
  const reportedId = findNativeModelOption(catalog, reported)?.id ?? reported;
  if (reportedId === requestedId) return "applied";
  return snapshot.status === "launching" ? "pending" : "unapplied";
}

/** Fields whose last write failed; re-sent even when stored values match. */
export interface SwitchReassert {
  model?: boolean;
  effort?: boolean;
}

/** Fields that decide a plan; any change means the plan is stale. */
export function switchBaselineChanged(a: SwitchSnapshot, b: SwitchSnapshot): boolean {
  return (
    (a.accountTransition?.id ?? null) !== (b.accountTransition?.id ?? null) ||
    (a.hostId ?? null) !== (b.hostId ?? null) ||
    (a.runnerId ?? null) !== (b.runnerId ?? null) ||
    (a.harness ?? null) !== (b.harness ?? null) ||
    (a.modelOverride ?? null) !== (b.modelOverride ?? null) ||
    (a.reasoningEffort ?? null) !== (b.reasoningEffort ?? null) ||
    (a.costControlModeOverride ?? null) !== (b.costControlModeOverride ?? null)
  );
}

/** Whether reaching the draft needs a runner launch on its host. */
export function switchNeedsLaunch(snapshot: SwitchSnapshot, hostId: string | null): boolean {
  if (hostId === null) return false;
  return hostId !== (snapshot.hostId ?? null) || snapshot.runnerId == null;
}

/**
 * Calls that take the session from its actual state to the draft: begin the move,
 * write model and effort, launch; or one plain update on the same host. A retry
 * resumes a recorded move, and [] means nothing to change.
 */
export function planSessionSwitch(
  snapshot: SwitchSnapshot,
  draft: SwitchDraft,
  catalog: readonly NativeModelOption[],
  reassert: SwitchReassert = {},
): SwitchStep[] {
  const option = findNativeModelOption(catalog, draft.model);
  if (option === null) throw new Error("The requested model isn't in this account's catalog.");
  if (
    draft.effort !== null &&
    !effortChoicesForModel(draft.harness, option).includes(draft.effort)
  ) {
    throw new Error("The requested effort isn't available for this model.");
  }

  const marker = snapshot.accountTransition;
  if (
    marker &&
    (marker.account_id !== draft.accountId ||
      marker.host_id !== draft.hostId ||
      marker.harness !== draft.harness ||
      marker.model !== option.id ||
      marker.effort !== draft.effort)
  ) {
    throw new Error("Finish the pending account change before choosing another destination.");
  }

  // An explicit model pin replaces Smart Routing, as in the composer.
  const routingOff = routingOn(snapshot) ? { costControlModeOverride: "off" as const } : {};

  if (marker || switchNeedsLaunch(snapshot, draft.hostId)) {
    const workspace = draft.workspace;
    if (!workspace) throw new Error("Choose a working directory on the new machine.");
    return [
      ...(marker
        ? []
        : [{ kind: "begin-transition", draft: { ...draft, model: option.id } } as const]),
      {
        kind: "update",
        updates: {
          modelOverride: option.id,
          reasoningEffort: draft.effort,
          costControlModeOverride: "off",
          silent: true,
        },
      },
      { kind: "launch", hostId: draft.hostId, workspace },
    ];
  }

  // Compare against the stored request, and re-send a request the session
  // doesn't report.
  const stored = findNativeModelOption(catalog, sessionModelId(snapshot))?.id ?? null;
  const modelChanged =
    routingOn(snapshot) ||
    stored !== option.id ||
    modelApplyState(snapshot, catalog, option.id) === "unapplied" ||
    reassert.model === true;
  const effortChanged =
    (snapshot.reasoningEffort ?? null) !== draft.effort || reassert.effort === true;
  if (!modelChanged && !effortChanged) return [];
  return [
    {
      kind: "update",
      updates: {
        ...(modelChanged ? { modelOverride: option.id, ...routingOff } : {}),
        ...(effortChanged ? { reasoningEffort: draft.effort } : {}),
      },
    },
  ];
}
