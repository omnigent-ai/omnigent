import type { AnyBlock } from "./blocks";

export const COMPUTER_USE_ACTION_KINDS = [
  "inspect",
  "click",
  "scroll",
  "type",
  "select",
  "drag",
  "key",
  "interact",
] as const;

export type ComputerUseActionKind = (typeof COMPUTER_USE_ACTION_KINDS)[number];

export interface ComputerUsePresentation {
  kind: "computer_use";
  provider: "claude" | "codex";
  appName?: string;
  appId?: string;
  actionLabel?: string;
  actionKinds?: ComputerUseActionKind[];
}

/** Flattened conversation-item JSON uses the server's snake_case keys. */
export interface ComputerUsePresentationWire {
  kind: "computer_use";
  provider: "claude" | "codex";
  app_name?: string;
  app_id?: string;
  action_label?: string;
  action_kinds?: ComputerUseActionKind[];
}

export interface ComputerFrameAttachment {
  kind: "computer_frame";
  fileId: string;
  contentType: "image/jpeg" | "image/png" | "image/webp";
  width: number;
  height: number;
}

export interface ComputerFrameAttachmentWire {
  kind: "computer_frame";
  file_id: string;
  content_type: ComputerFrameAttachment["contentType"];
  width: number;
  height: number;
}

export type ComputerUseTerminalStatus = "completed" | "failed" | "interrupted";
export type ComputerUseStatus = "running" | ComputerUseTerminalStatus;

export interface ComputerUseViewModel {
  callId: string;
  presentation: ComputerUsePresentation;
  status: ComputerUseStatus;
  frame: ComputerFrameAttachment | null;
  error: string | null;
}

const DISPLAY_TEXT_LIMIT = 256;
const ERROR_TEXT_LIMIT = 512;
const MAX_ATTACHMENTS = 16;
const MAX_ACTION_KINDS = COMPUTER_USE_ACTION_KINDS.length;
const ACTION_KIND_SET = new Set<string>(COMPUTER_USE_ACTION_KINDS);
const SUPPORTED_FRAME_TYPES = new Set<ComputerFrameAttachment["contentType"]>([
  "image/jpeg",
  "image/png",
  "image/webp",
]);

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function optionalDisplayText(value: unknown): string | undefined {
  if (typeof value !== "string" || value.length > DISPLAY_TEXT_LIMIT) return undefined;
  const trimmed = value.trim();
  return trimmed || undefined;
}

function actionKindsFromWire(value: unknown): ComputerUseActionKind[] {
  if (!Array.isArray(value)) return [];
  const actionKinds: ComputerUseActionKind[] = [];
  for (const candidate of value.slice(0, MAX_ACTION_KINDS)) {
    if (
      typeof candidate === "string" &&
      ACTION_KIND_SET.has(candidate) &&
      !actionKinds.includes(candidate as ComputerUseActionKind)
    ) {
      actionKinds.push(candidate as ComputerUseActionKind);
    }
  }
  return actionKinds;
}

/** Parse the server's optional, display-only computer-use metadata. */
export function computerUsePresentationFromWire(
  value: unknown,
): ComputerUsePresentation | undefined {
  const data = record(value);
  if (data === null || data.kind !== "computer_use") return undefined;
  if (data.provider !== "claude" && data.provider !== "codex") return undefined;

  const appName = optionalDisplayText(data.app_name);
  const appId = optionalDisplayText(data.app_id);
  const actionLabel = optionalDisplayText(data.action_label);
  const actionKinds = actionKindsFromWire(data.action_kinds);
  return {
    kind: "computer_use",
    provider: data.provider,
    ...(appName ? { appName } : {}),
    ...(appId ? { appId } : {}),
    ...(actionLabel ? { actionLabel } : {}),
    ...(actionKinds.length > 0 ? { actionKinds } : {}),
  };
}

function isFrameContentType(value: unknown): value is ComputerFrameAttachment["contentType"] {
  return (
    typeof value === "string" &&
    SUPPORTED_FRAME_TYPES.has(value as ComputerFrameAttachment["contentType"])
  );
}

function isFrameDimension(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value > 0 && value <= 32_768;
}

/** Keep only complete, browser-renderable computer frame attachments. */
export function computerFramesFromWire(value: unknown): ComputerFrameAttachment[] {
  if (!Array.isArray(value)) return [];
  const frames: ComputerFrameAttachment[] = [];
  for (const candidate of value.slice(0, MAX_ATTACHMENTS)) {
    const data = record(candidate);
    if (
      data === null ||
      data.kind !== "computer_frame" ||
      typeof data.file_id !== "string" ||
      data.file_id.length === 0 ||
      data.file_id.length > 128 ||
      !isFrameContentType(data.content_type) ||
      !isFrameDimension(data.width) ||
      !isFrameDimension(data.height)
    ) {
      continue;
    }
    frames.push({
      kind: "computer_frame",
      fileId: data.file_id,
      contentType: data.content_type,
      width: data.width,
      height: data.height,
    });
  }
  return frames;
}

export function computerUseStatusFromWire(value: unknown): ComputerUseTerminalStatus | undefined {
  return value === "completed" || value === "failed" || value === "interrupted" ? value : undefined;
}

interface CallState {
  callId: string;
  lastSequence: number;
  presentation?: ComputerUsePresentation;
  status: ComputerUseStatus;
  frames: { sequence: number; frame: ComputerFrameAttachment }[];
  error: string | null;
}

function boundedError(output: string): string | null {
  const trimmed = output.trim();
  if (!trimmed) return null;
  return trimmed.length <= ERROR_TEXT_LIMIT
    ? trimmed
    : `${trimmed.slice(0, ERROR_TEXT_LIMIT - 1).trimEnd()}…`;
}

/**
 * Derive the latest computer-use activity from the shared history/live block
 * shape. Terminal metadata is authoritative; a final result with no
 * presentation clears a provisional classification. The newest prior frame is
 * retained across later text-only computer actions.
 */
export function deriveComputerUseViewModel(
  blocks: readonly AnyBlock[],
): ComputerUseViewModel | null {
  const calls = new Map<string, CallState>();
  let sequence = 0;

  for (const block of blocks) {
    if (block.type === "tool_group") {
      for (const execution of block.executions) {
        sequence += 1;
        const existing = calls.get(execution.callId);
        const state: CallState = existing ?? {
          callId: execution.callId,
          lastSequence: sequence,
          status: "running",
          frames: [],
          error: null,
        };
        state.lastSequence = sequence;
        if (execution.presentation) state.presentation = execution.presentation;
        calls.set(execution.callId, state);
      }
      continue;
    }
    if (block.type !== "tool_result") continue;

    sequence += 1;
    const existing = calls.get(block.callId);
    const state: CallState = existing ?? {
      callId: block.callId,
      lastSequence: sequence,
      status: "running",
      frames: [],
      error: null,
    };
    state.lastSequence = sequence;
    if (block.presentation) {
      state.presentation = block.presentation;
    } else if (block.presentationFinal) {
      state.presentation = undefined;
    }
    state.status = block.status ?? "completed";
    state.error = state.status === "failed" ? boundedError(block.output) : null;
    for (const frame of block.attachments ?? []) {
      sequence += 1;
      state.frames.push({ sequence, frame });
      state.lastSequence = sequence;
    }
    calls.set(block.callId, state);
  }

  const classified = Array.from(calls.values()).filter(
    (call): call is CallState & { presentation: ComputerUsePresentation } =>
      call.presentation !== undefined,
  );
  if (classified.length === 0) return null;

  const latest = classified.reduce((current, candidate) =>
    candidate.lastSequence > current.lastSequence ? candidate : current,
  );
  const latestFrame = classified
    .flatMap((call) => call.frames)
    .filter((entry) => entry.sequence <= latest.lastSequence)
    .reduce<{ sequence: number; frame: ComputerFrameAttachment } | null>(
      (current, candidate) =>
        current === null || candidate.sequence > current.sequence ? candidate : current,
      null,
    );

  return {
    callId: latest.callId,
    presentation: latest.presentation,
    status: latest.status,
    frame: latestFrame?.frame ?? null,
    error: latest.error,
  };
}

export function computerUseViewModelsEqual(
  left: ComputerUseViewModel | null,
  right: ComputerUseViewModel | null,
): boolean {
  if (left === right) return true;
  if (left === null || right === null) return false;
  const leftActionKinds = left.presentation.actionKinds ?? [];
  const rightActionKinds = right.presentation.actionKinds ?? [];
  return (
    left.callId === right.callId &&
    left.status === right.status &&
    left.error === right.error &&
    left.presentation.provider === right.presentation.provider &&
    left.presentation.appName === right.presentation.appName &&
    left.presentation.appId === right.presentation.appId &&
    left.presentation.actionLabel === right.presentation.actionLabel &&
    leftActionKinds.length === rightActionKinds.length &&
    leftActionKinds.every((actionKind, index) => actionKind === rightActionKinds[index]) &&
    left.frame?.fileId === right.frame?.fileId &&
    left.frame?.contentType === right.frame?.contentType &&
    left.frame?.width === right.frame?.width &&
    left.frame?.height === right.frame?.height
  );
}
