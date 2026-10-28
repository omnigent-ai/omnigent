// What a collapsed tool-call row says about its result: an edit's line counts.
// Everything else, failures included, waits behind the expand.

import { stripAnsi } from "@/lib/ansi";
import type { ToolState } from "@/lib/renderItems";
import { stripOmnigentPrefix } from "@/lib/toolTitle";
import {
  contentEnvelopeText,
  editErrorMessage,
  firstLine,
  isRecord,
  parseToolOutput,
} from "@/lib/toolValue";
import { getFileEditDiff } from "@/lib/toolEditDiff";

/** A tool call as the transcript renders it. */
export interface ToolCall {
  name: string;
  args: Record<string, unknown>;
  /** Tool output, or `null` if not yet available / never produced. */
  output: string | null;
  state: ToolState;
}

/** Lines an edit added and removed. */
export interface LineCounts {
  additions: number;
  removals: number;
}

// Codex patch output leads with its outcome. Output without that line does not
// say whether the patch applied, so it gets no counts.
const CODEX_PATCH_APPLIED = "File change applied.";
// First lines of each harness's successful edit: Claude and Qwen, Claude
// MultiEdit, Omnigent's built-in Edit, Pi, OpenCode, and a new Qwen file.
const EDIT_APPLIED_RE =
  /^(?:The file:? .+ has been updated|Applied \d+ edits? to |Replaced \d+ occurrence|Successfully replaced |Edit applied successfully|Created new file: )/;

/**
 * A finished edit's added and removed line counts, once its output says it
 * applied. Running, failed and unchanged edits (including pure renames) have
 * none, nor do a replace-all edit, whose arguments describe one match of an
 * unknown number, and counts that could overstate the change.
 */
export function fileEditLineCounts(call: ToolCall): LineCounts | null {
  if (call.state !== "output-available" || call.output === null) return null;
  if (editErrorMessage(call.output) !== null || !editApplied(call.name, call.output)) return null;
  const diff = getFileEditDiff(call.name, call.args);
  if (diff === null || diff.countsArePerMatch || diff.countsAreInexact) return null;
  if (diff.additions + diff.removals === 0) return null;
  return { additions: diff.additions, removals: diff.removals };
}

/**
 * Whether an edit's output says the change applied. Failures can lack a
 * recognized error prefix, so counts need this positive evidence.
 */
export function editApplied(name: string, output: string): boolean {
  const parsed = parseToolOutput(output);
  const tool = stripOmnigentPrefix(name);
  if (tool === "apply_patch") return firstLine(parsed.text) === CODEX_PATCH_APPLIED;
  if (tool === "sys_os_edit") {
    const record = parsed.kind === "json" && isRecord(parsed.value) ? parsed.value : null;
    const replacements = record?.replacements;
    return typeof replacements === "number" && replacements > 0;
  }
  const envelopeText = parsed.kind === "json" ? contentEnvelopeText(parsed.value) : null;
  return EDIT_APPLIED_RE.test(stripAnsi(envelopeText ?? parsed.text).trim());
}
