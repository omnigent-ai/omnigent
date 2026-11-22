interface FileTreeRowClickPlan {
  selection: { kind: "range"; additive: boolean } | { kind: "toggle" } | { kind: "single" };
  toggleDirectory: boolean;
}

/**
 * What a click on a row does, from its modifier keys: Shift extends a range,
 * Command or Control toggles one row, and a plain click selects the row and
 * opens or closes a directory.
 */
export function computeFileTreeRowClickPlan(
  event: { shiftKey: boolean; ctrlKey: boolean; metaKey: boolean },
  isDirectory: boolean,
): FileTreeRowClickPlan {
  const additive = event.ctrlKey || event.metaKey;
  return {
    selection: event.shiftKey
      ? { kind: "range", additive }
      : additive
        ? { kind: "toggle" }
        : { kind: "single" },
    toggleDirectory: isDirectory && !event.shiftKey && !additive,
  };
}
