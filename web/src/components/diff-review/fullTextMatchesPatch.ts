// A patch's full text, loaded to expand its unchanged lines, can come from another revision
// than the patch, or be missing a side. Filled in anyway, the diff points at lines the text
// doesn't have, so the text is used only when every line the patch shows is where it says.
import type { FileDiffLoadedFiles, FileDiffMetadata } from "@pierre/diffs";

/** Lines as the diff library splits them, each keeping its newline. */
function splitLines(contents: string): string[] {
  return contents === "" ? [] : contents.split(/(?<=\n)/);
}

/** The same line, whether or not it ends the file without a newline. */
function sameLine(patchLine: string | undefined, fileLine: string | undefined): boolean {
  if (patchLine === undefined || fileLine === undefined) return false;
  return patchLine.replace(/\n$/, "") === fileLine.replace(/\n$/, "");
}

/** Whether `files` holds every line `fileDiff`'s patch shows, at the line numbers it shows them. */
export function fullTextMatchesPatch(
  fileDiff: FileDiffMetadata,
  files: FileDiffLoadedFiles,
): boolean {
  if (!fileDiff.isPartial) return true;
  const newLines = splitLines(files.newFile.contents);
  // A pure rename has one text, shown on both sides.
  const oldLines = files.oldFile ? splitLines(files.oldFile.contents) : newLines;
  const matches = (
    patchLines: readonly string[],
    patchStart: number,
    fileLines: readonly string[],
    fileStart: number,
    count: number,
  ) => {
    for (let offset = 0; offset < count; offset++) {
      if (!sameLine(patchLines[patchStart + offset], fileLines[fileStart + offset])) return false;
    }
    return true;
  };
  for (const hunk of fileDiff.hunks) {
    let oldIndex = Math.max(hunk.deletionStart - 1, 0);
    let newIndex = Math.max(hunk.additionStart - 1, 0);
    for (const content of hunk.hunkContent) {
      const deletions = content.type === "context" ? content.lines : content.deletions;
      const additions = content.type === "context" ? content.lines : content.additions;
      if (
        !matches(
          fileDiff.deletionLines,
          content.deletionLineIndex,
          oldLines,
          oldIndex,
          deletions,
        ) ||
        !matches(fileDiff.additionLines, content.additionLineIndex, newLines, newIndex, additions)
      ) {
        return false;
      }
      oldIndex += deletions;
      newIndex += additions;
    }
  }
  return true;
}
