// Derive a line diff for file-editing tool calls so the transcript shows
// what changed instead of a raw full-content Parameters dump.
//
// Two sources, matching where each tool's "before" side exists:
// - exact-replacement edit tools (sys_os_edit, Claude Code's Edit/MultiEdit,
//   Pi/OpenCode's edit) carry old/new text pairs in their arguments;
// - full-file write tools (sys_os_write) can't know the replaced content
//   client-side, so the OS environment reports a unified `diff` in the
//   tool's JSON result when it overwrites an existing text file.

/** Unchanged context lines kept around a replacement hunk. */
const CONTEXT_LINES = 2;

interface EditPair {
  oldText: string;
  newText: string;
}

/** Tools whose arguments carry exact old/new replacement pairs. */
const EDIT_PAIR_TOOLS = new Set(["sys_os_edit", "edit", "Edit", "MultiEdit"]);

/** Tools whose JSON result may carry a server-computed `diff` field. */
const WRITE_RESULT_TOOLS = new Set(["sys_os_write", "write", "Write"]);

function stripOmnigentPrefix(name: string): string {
  return name.startsWith("mcp__omnigent__") ? name.slice("mcp__omnigent__".length) : name;
}

/** Read one old/new pair from an object, accepting each harness's key style. */
function asPair(value: unknown): EditPair | null {
  if (value === null || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const oldText = record.oldText ?? record.old_string ?? record.oldString;
  const newText = record.newText ?? record.new_string ?? record.newString;
  if (typeof oldText !== "string" || typeof newText !== "string") return null;
  if (oldText === newText) return null;
  return { oldText, newText };
}

function editPairs(args: Record<string, unknown>): EditPair[] {
  const edits = args.edits;
  if (Array.isArray(edits)) {
    const pairs = edits.map(asPair).filter((pair): pair is EditPair => pair !== null);
    if (pairs.length > 0) return pairs;
  }
  const single = asPair(args);
  return single === null ? [] : [single];
}

function splitLines(text: string): string[] {
  if (text === "") return [];
  const lines = text.split("\n");
  // A trailing newline is a line terminator, not an extra empty line.
  if (lines[lines.length - 1] === "") lines.pop();
  return lines;
}

/**
 * Render one replacement pair as a diff hunk: trim the lines common to
 * both sides, keep a couple of them back as context, and mark the rest
 * removed/added.
 */
function pairHunk(pair: EditPair): string {
  const oldLines = splitLines(pair.oldText);
  const newLines = splitLines(pair.newText);
  let start = 0;
  while (
    start < oldLines.length &&
    start < newLines.length &&
    oldLines[start] === newLines[start]
  ) {
    start += 1;
  }
  let oldEnd = oldLines.length;
  let newEnd = newLines.length;
  while (oldEnd > start && newEnd > start && oldLines[oldEnd - 1] === newLines[newEnd - 1]) {
    oldEnd -= 1;
    newEnd -= 1;
  }
  const before = oldLines.slice(Math.max(0, start - CONTEXT_LINES), start).map((l) => ` ${l}`);
  const removed = oldLines.slice(start, oldEnd).map((l) => `-${l}`);
  const added = newLines.slice(start, newEnd).map((l) => `+${l}`);
  const after = oldLines
    .slice(oldEnd, Math.min(oldLines.length, oldEnd + CONTEXT_LINES))
    .map((l) => ` ${l}`);
  return [...before, ...removed, ...added, ...after].join("\n");
}

/** The `diff` field of a write tool's JSON result, when present. */
function writeResultDiff(output: string | null): string | null {
  if (output === null) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(output);
  } catch {
    return null;
  }
  if (parsed === null || typeof parsed !== "object") return null;
  const diff = (parsed as Record<string, unknown>).diff;
  return typeof diff === "string" && diff.length > 0 ? diff : null;
}

/**
 * Diff text for a file-editing tool call, or `null` when the call isn't a
 * file edit / has no derivable "before" side (e.g. a write that created a
 * new file).
 */
export function getFileEditDiff(
  name: string,
  args: Record<string, unknown>,
  output: string | null,
): string | null {
  const tool = stripOmnigentPrefix(name);
  if (EDIT_PAIR_TOOLS.has(tool)) {
    const hunks = editPairs(args)
      .map(pairHunk)
      .filter((hunk) => hunk.length > 0);
    return hunks.length > 0 ? hunks.join("\n\n") : null;
  }
  if (WRITE_RESULT_TOOLS.has(tool)) {
    return writeResultDiff(output);
  }
  return null;
}
