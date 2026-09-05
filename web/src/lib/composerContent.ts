import type { ContentBlock } from "./types";

/** One object-replacement character per attachment in the editor text projection. */
export const COMPOSER_ATTACHMENT_PLACEHOLDER = "\uFFFC";

export type ComposerDraftPart = { type: "text"; text: string } | { type: "attachment"; file: File };

function appendText(parts: ComposerDraftPart[], text: string): void {
  if (!text) return;
  const tail = parts.at(-1);
  if (tail?.type === "text") tail.text += text;
  else parts.push({ type: "text", text });
}

/** Collapse empty text and merge adjacent text while preserving attachment order. */
export function normalizeComposerParts(parts: readonly ComposerDraftPart[]): ComposerDraftPart[] {
  const normalized: ComposerDraftPart[] = [];
  for (const part of parts) {
    if (part.type === "text") appendText(normalized, part.text);
    else normalized.push(part);
  }
  return normalized;
}

/** Text projection used by slash, mention, history, and dictation offset logic. */
export function composerPartsToProjection(parts: readonly ComposerDraftPart[]): string {
  return parts
    .map((part) => (part.type === "text" ? part.text : COMPOSER_ATTACHMENT_PLACEHOLDER))
    .join("");
}

/** User-authored text only; attachment placeholders never leak into a prompt. */
export function composerPartsToText(parts: readonly ComposerDraftPart[]): string {
  return parts
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join("");
}

export function composerAttachments(parts: readonly ComposerDraftPart[]): File[] {
  return parts
    .filter(
      (part): part is Extract<ComposerDraftPart, { type: "attachment" }> =>
        part.type === "attachment",
    )
    .map((part) => part.file);
}

/** Rebuild ordered parts from a projection and its attachment sequence. */
export function composerPartsFromProjection(
  projection: string,
  attachments: readonly File[],
): ComposerDraftPart[] {
  const parts: ComposerDraftPart[] = [];
  let attachmentIndex = 0;
  let start = 0;
  for (let index = 0; index < projection.length; index += 1) {
    if (projection[index] !== COMPOSER_ATTACHMENT_PLACEHOLDER) continue;
    appendText(parts, projection.slice(start, index));
    const file = attachments[attachmentIndex];
    // A literal object-replacement character is valid user text. Treat it as
    // an attachment slot only while a real attachment remains to fill it.
    if (file) parts.push({ type: "attachment", file });
    else appendText(parts, COMPOSER_ATTACHMENT_PLACEHOLDER);
    attachmentIndex += 1;
    start = index + 1;
  }
  appendText(parts, projection.slice(start));
  return normalizeComposerParts(parts);
}

/** Replace authored text while retaining every attachment at its current seam. */
export function replaceComposerText(
  parts: readonly ComposerDraftPart[],
  nextText: string,
): ComposerDraftPart[] {
  const normalized = normalizeComposerParts(parts);
  const previousText = composerPartsToText(normalized);
  let prefix = 0;
  while (
    prefix < previousText.length &&
    prefix < nextText.length &&
    previousText[prefix] === nextText[prefix]
  ) {
    prefix += 1;
  }
  let suffix = 0;
  while (
    suffix < previousText.length - prefix &&
    suffix < nextText.length - prefix &&
    previousText[previousText.length - 1 - suffix] === nextText[nextText.length - 1 - suffix]
  ) {
    suffix += 1;
  }

  const replacement = nextText.slice(prefix, nextText.length - suffix);
  const replacedEnd = previousText.length - suffix;
  const result: ComposerDraftPart[] = [];
  let textOffset = 0;
  let inserted = false;
  for (const part of normalized) {
    if (part.type === "attachment") {
      result.push(part);
      continue;
    }
    const partStart = textOffset;
    const partEnd = partStart + part.text.length;
    appendText(
      result,
      part.text.slice(0, Math.max(0, Math.min(part.text.length, prefix - partStart))),
    );
    if (!inserted && prefix <= partEnd) {
      appendText(result, replacement);
      inserted = true;
    }
    const suffixStart = Math.max(0, replacedEnd - partStart);
    appendText(result, part.text.slice(Math.min(part.text.length, suffixStart)));
    textOffset = partEnd;
  }
  if (!inserted) appendText(result, replacement);
  return normalizeComposerParts(result);
}

/** Build message content in the exact order represented by the composer. */
export async function composerPartsToContentBlocks(
  parts: readonly ComposerDraftPart[],
  resolveAttachment: (file: File) => Promise<ContentBlock>,
): Promise<ContentBlock[]> {
  const blocks: ContentBlock[] = [];
  for (const part of normalizeComposerParts(parts)) {
    if (part.type === "text") {
      if (part.text) blocks.push({ type: "input_text", text: part.text });
    } else {
      // Sequential resolution preserves both upload order and content order.
      // oxlint-disable-next-line no-await-in-loop
      blocks.push(await resolveAttachment(part.file));
    }
  }
  return blocks;
}

export function legacyComposerParts(text: string, files: readonly File[]): ComposerDraftPart[] {
  return normalizeComposerParts([
    ...files.map((file) => ({ type: "attachment" as const, file })),
    ...(text ? [{ type: "text" as const, text }] : []),
  ]);
}
