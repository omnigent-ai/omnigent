// Whether diffs soft-wrap long lines: the file viewer's saved preference, so
// every review wraps alike and the choice survives a reload.

import { useCallback, useState } from "react";
import { readFileViewPreferences, writeFileViewPreferences } from "@/lib/fileViewPreferences";

export function useSavedWrapLines(): [boolean, (wrap: boolean) => void] {
  const [wrapLines, setWrapLines] = useState(() => readFileViewPreferences().wrapLines);
  const changeWrapLines = useCallback((wrap: boolean) => {
    setWrapLines(wrap);
    writeFileViewPreferences({ ...readFileViewPreferences(), wrapLines: wrap });
  }, []);
  return [wrapLines, changeWrapLines];
}
