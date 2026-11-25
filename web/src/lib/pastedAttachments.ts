/**
 * Pasted content the CodeMirror composer turns into attachment chips: a large
 * plain-text paste becomes a `.txt` file, and a chip's name is unique in the draft.
 */

/** A plain-text paste with at least this many lines or characters becomes a file. */
export const PASTE_ATTACHMENT_THRESHOLD = { lines: 40, characters: 4000 } as const;

/** The `.txt` attachment a large plain-text paste becomes, or `null` when it stays inline. */
export function largePasteAsFile(
  data: Pick<DataTransfer, "getData">,
  trayNames: readonly string[] = [],
): File | null {
  const text = data.getData("text/plain").replace(/\r\n?/g, "\n");
  const lines = text.replace(/\n$/, "").split("\n").length;
  if (
    text.length < PASTE_ATTACHMENT_THRESHOLD.characters &&
    lines < PASTE_ATTACHMENT_THRESHOLD.lines
  ) {
    return null;
  }
  const name = uniqueAttachmentName("pasted-text.txt", (candidate) =>
    trayNames.includes(candidate),
  );
  return new File([text], name, { type: "text/plain" });
}

/**
 * `name` as a chip token's name, with a ` (n)` suffix before its extension when `isTaken` says a
 * spelling is already in use, e.g. `image (2).png`.
 */
export function uniqueAttachmentName(
  name: string,
  isTaken: (candidate: string) => boolean,
): string {
  if (!isTaken(name)) return name;
  const dot = name.lastIndexOf(".");
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const extension = dot > 0 ? name.slice(dot) : "";
  for (let suffix = 2; ; suffix++) {
    const candidate = `${stem} (${suffix})${extension}`;
    if (!isTaken(candidate)) return candidate;
  }
}
