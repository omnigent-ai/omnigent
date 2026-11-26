// A name that truncates in the middle, keeping its start and its extension, as
// upstream's `MiddleTruncate` with `split="extension"` does. The browser draws
// each clipped part's ellipsis itself.

const MINIMUM_SPLIT_LENGTH = 5;
const MAXIMUM_EXTENSION_LENGTH = 10;

const isWhitespace = (character: string | undefined): boolean =>
  character !== undefined && /\s/.test(character);

// A seam next to a space would collapse it under `white-space: nowrap`, so
// move the seam to the nearest index with no space on either side.
function avoidWhitespaceBoundary(contents: string, centerIndex: number): number {
  const isOnBoundary = (index: number) =>
    isWhitespace(contents[index - 1]) || isWhitespace(contents[index]);
  if (!isOnBoundary(centerIndex)) return centerIndex;
  for (let offset = 1; offset < contents.length; offset++) {
    const before = centerIndex - offset;
    if (before > 0 && !isOnBoundary(before)) return before;
    const after = centerIndex + offset;
    if (after < contents.length && !isOnBoundary(after)) return after;
  }
  return centerIndex;
}

/** Splits `name` before its extension, or in the middle when it has none or a long one. */
export function splitNameForTruncation(name: string): [string, string] {
  if (name.length < MINIMUM_SPLIT_LENGTH) return [name, ""];
  const extensionIndex = name.lastIndexOf(".") + 1;
  const extensionLength = name.length - extensionIndex;
  const splitIndex =
    extensionIndex >= 1 && extensionLength <= MAXIMUM_EXTENSION_LENGTH
      ? extensionIndex
      : avoidWhitespaceBoundary(name, Math.ceil(name.length / 2));
  return [name.slice(0, splitIndex), name.slice(splitIndex)];
}

export function MiddleTruncate({ name }: { name: string }) {
  const [start, end] = splitNameForTruncation(name);
  if (end === "") {
    return (
      <span data-truncate-container="truncate">
        <span data-truncate-content="visible">{start}</span>
      </span>
    );
  }
  // The start gives way first; the extension clips only once the start is gone.
  return (
    <span data-truncate-group-container="middle">
      <span data-truncate-segment-priority="2">
        <span data-truncate-container="truncate">
          <span data-truncate-content="visible">{start}</span>
        </span>
      </span>
      <span data-truncate-segment-priority="1">
        <span data-truncate-container="fruncate">
          <span data-truncate-content="visible">
            <span>{end}</span>
          </span>
        </span>
      </span>
    </span>
  );
}
