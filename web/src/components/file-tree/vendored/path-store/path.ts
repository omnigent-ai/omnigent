import type { LookupPath, PreparedPath } from "./internal-types";

// Canonical path parsing runs once per input path during prepare steps, so keep
// it as a single pass over the string and avoid constructing an extra
// trailing-slash-trimmed copy before splitting segments.
function splitCanonicalPath(inputPath: string): {
  hasTrailingSlash: boolean;
  segments: readonly string[];
} {
  const hasTrailingSlash =
    inputPath.length > 0 && inputPath.charCodeAt(inputPath.length - 1) === 47;
  const endIndex = hasTrailingSlash ? inputPath.length - 1 : inputPath.length;
  const segments: string[] = [];
  let segmentStart = 0;

  for (let index = 0; index < endIndex; index++) {
    if (inputPath.charCodeAt(index) !== 47) {
      continue;
    }

    segments.push(inputPath.slice(segmentStart, index));
    segmentStart = index + 1;
  }

  segments.push(inputPath.slice(segmentStart, endIndex));

  return {
    hasTrailingSlash,
    segments,
  };
}

export function parseInputPath(inputPath: string): PreparedPath {
  const { hasTrailingSlash, segments } = splitCanonicalPath(inputPath);
  const basename = segments[segments.length - 1] ?? "";

  return {
    basename,
    isDirectory: hasTrailingSlash,
    path: inputPath,
    segments,
  };
}

export function parseLookupPath(inputPath: string): LookupPath {
  if (inputPath.length === 0) {
    return {
      requiresDirectory: false,
      segments: [],
    };
  }

  const { hasTrailingSlash, segments } = splitCanonicalPath(inputPath);
  return {
    requiresDirectory: hasTrailingSlash,
    segments,
  };
}
