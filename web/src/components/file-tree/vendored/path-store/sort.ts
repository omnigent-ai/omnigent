import type { PreparedPath, SegmentSortKey, SegmentTable } from "./internal-types";
import { PATH_STORE_NODE_KIND_DIRECTORY, PATH_STORE_NODE_KIND_FILE } from "./internal-types";
import type { PathStoreCompareEntry } from "./public-types";

function isDigitCode(characterCode: number): boolean {
  return characterCode >= 48 && characterCode <= 57;
}

function splitIntoNaturalTokens(value: string): readonly (number | string)[] {
  const tokens: (number | string)[] = [];
  let tokenStart = 0;
  let index = 0;

  while (index < value.length) {
    while (index < value.length && !isDigitCode(value.charCodeAt(index))) {
      index += 1;
    }

    if (index >= value.length) {
      break;
    }

    if (index > tokenStart) {
      tokens.push(value.slice(tokenStart, index));
    }

    let numberValue = 0;
    while (index < value.length && isDigitCode(value.charCodeAt(index))) {
      numberValue = numberValue * 10 + (value.charCodeAt(index) - 48);
      index += 1;
    }

    tokens.push(numberValue);
    tokenStart = index;
  }

  if (tokenStart < value.length || tokens.length === 0) {
    tokens.push(value.slice(tokenStart));
  }

  return tokens;
}

export function createSegmentSortKey(value: string): SegmentSortKey {
  const lowerValue = value.toLowerCase();
  return {
    lowerValue,
    tokens: splitIntoNaturalTokens(lowerValue),
  };
}

function compareNaturalTokens(
  leftTokens: readonly (number | string)[],
  rightTokens: readonly (number | string)[],
): number {
  const tokenCount = Math.min(leftTokens.length, rightTokens.length);

  for (let index = 0; index < tokenCount; index++) {
    const leftToken = leftTokens[index];
    const rightToken = rightTokens[index];

    if (leftToken === rightToken) {
      continue;
    }

    if (typeof leftToken === "number" && typeof rightToken === "number") {
      return leftToken < rightToken ? -1 : 1;
    }

    const leftString = String(leftToken);
    const rightString = String(rightToken);
    if (leftString !== rightString) {
      return leftString < rightString ? -1 : 1;
    }
  }

  if (leftTokens.length !== rightTokens.length) {
    return leftTokens.length < rightTokens.length ? -1 : 1;
  }

  return 0;
}

export function compareSegmentSortKeys(leftKey: SegmentSortKey, rightKey: SegmentSortKey): number {
  // Segments without numeric runs produce a single string token, so they can
  // compare directly on the cached lower-case string without walking the token
  // arrays or coercing tokens back through String().
  if (
    leftKey.tokens.length === 1 &&
    rightKey.tokens.length === 1 &&
    typeof leftKey.tokens[0] === "string" &&
    typeof rightKey.tokens[0] === "string"
  ) {
    if (leftKey.lowerValue === rightKey.lowerValue) {
      return 0;
    }

    return leftKey.lowerValue < rightKey.lowerValue ? -1 : 1;
  }

  const tokenComparison = compareNaturalTokens(leftKey.tokens, rightKey.tokens);
  if (tokenComparison !== 0) {
    return tokenComparison;
  }

  if (leftKey.lowerValue !== rightKey.lowerValue) {
    return leftKey.lowerValue < rightKey.lowerValue ? -1 : 1;
  }

  return 0;
}

function compareSegmentValuesWithSortKeyLookup(
  left: string,
  right: string,
  getSortKey: (value: string) => SegmentSortKey,
): number {
  const leftKey = getSortKey(left);
  const rightKey = getSortKey(right);
  const comparison = compareSegmentSortKeys(leftKey, rightKey);
  if (comparison !== 0) {
    return comparison;
  }

  if (left === right) {
    return 0;
  }

  return left < right ? -1 : 1;
}

function compareSegmentValues(left: string, right: string): number {
  return compareSegmentValuesWithSortKeyLookup(left, right, createSegmentSortKey);
}

function getKindAtDepth(entry: PreparedPath | PathStoreCompareEntry, depth: number): number {
  const isTerminalSegment = depth === entry.segments.length - 1;
  if (!isTerminalSegment) {
    return PATH_STORE_NODE_KIND_DIRECTORY;
  }

  return entry.isDirectory ? PATH_STORE_NODE_KIND_DIRECTORY : PATH_STORE_NODE_KIND_FILE;
}

function comparePreparedEntries(
  left: PreparedPath | PathStoreCompareEntry,
  right: PreparedPath | PathStoreCompareEntry,
): number {
  const sharedDepth = Math.min(left.segments.length, right.segments.length);

  for (let depth = 0; depth < sharedDepth; depth++) {
    const leftSegment = left.segments[depth];
    const rightSegment = right.segments[depth];

    if (leftSegment === rightSegment) {
      continue;
    }

    const leftKind = getKindAtDepth(left, depth);
    const rightKind = getKindAtDepth(right, depth);
    if (leftKind !== rightKind) {
      return leftKind === PATH_STORE_NODE_KIND_DIRECTORY ? -1 : 1;
    }

    return compareSegmentValues(leftSegment, rightSegment);
  }

  if (left.segments.length !== right.segments.length) {
    return left.segments.length < right.segments.length ? -1 : 1;
  }

  if (left.isDirectory === right.isDirectory) {
    return 0;
  }

  return left.isDirectory ? -1 : 1;
}

export function comparePreparedPaths(left: PreparedPath, right: PreparedPath): number {
  return comparePreparedEntries(left, right);
}

export function comparePreparedPathsWithCachedSortKeys(
  left: PreparedPath,
  right: PreparedPath,
  cache: Map<string, SegmentSortKey>,
): number {
  const getCachedSortKey = (value: string): SegmentSortKey => {
    const existingKey = cache.get(value);
    if (existingKey != null) {
      return existingKey;
    }

    const nextKey = createSegmentSortKey(value);
    cache.set(value, nextKey);
    return nextKey;
  };
  const sharedDepth = Math.min(left.segments.length, right.segments.length);

  for (let depth = 0; depth < sharedDepth; depth++) {
    const leftSegment = left.segments[depth];
    const rightSegment = right.segments[depth];

    if (leftSegment === rightSegment) {
      continue;
    }

    const leftKind = getKindAtDepth(left, depth);
    const rightKind = getKindAtDepth(right, depth);
    if (leftKind !== rightKind) {
      return leftKind === PATH_STORE_NODE_KIND_DIRECTORY ? -1 : 1;
    }

    return compareSegmentValuesWithSortKeyLookup(leftSegment, rightSegment, getCachedSortKey);
  }

  if (left.segments.length !== right.segments.length) {
    return left.segments.length < right.segments.length ? -1 : 1;
  }

  if (left.isDirectory === right.isDirectory) {
    return 0;
  }

  return left.isDirectory ? -1 : 1;
}

export function getSegmentSortKey(segmentTable: SegmentTable, segmentId: number): SegmentSortKey {
  const existingKey = segmentTable.sortKeyById[segmentId];
  if (existingKey !== undefined) {
    return existingKey;
  }

  const value = segmentTable.valueById[segmentId];
  const nextKey = createSegmentSortKey(value);
  segmentTable.sortKeyById[segmentId] = nextKey;
  return nextKey;
}
