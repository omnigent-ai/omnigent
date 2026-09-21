import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useLocation, useSearchParams } from "@/lib/routing";
import { readFileViewPreferences, writeFileViewPreferences } from "@/lib/fileViewPreferences";

function usePreferencesState() {
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const [previousLocationKey, setPreviousLocationKey] = useState(location.key);
  const [initial] = useState(readFileViewPreferences);
  const [diffActive, setDiffActive] = useState(
    () => searchParams.get("diff") === "1" || initial.diffActive,
  );
  // Honor explicit navigation (including Back/Forward) before viewers sync the URL.
  // Local toggles keep the same location until their own URL write completes.
  if (previousLocationKey !== location.key) {
    setPreviousLocationKey(location.key);
    if (searchParams.get("diff") === "1") setDiffActive(true);
  }
  const [diffLayout, setDiffLayout] = useState(initial.diffLayout);
  const [hideWhitespace, setHideWhitespace] = useState(initial.hideWhitespace);
  const [wrapLines, setWrapLines] = useState(initial.wrapLines);
  const [previewableViewMode, setPreviewableViewMode] = useState(initial.previewableViewMode);

  useEffect(() => {
    writeFileViewPreferences({
      diffActive,
      diffLayout,
      hideWhitespace,
      wrapLines,
      previewableViewMode,
    });
  }, [diffActive, diffLayout, hideWhitespace, wrapLines, previewableViewMode]);

  return useMemo(
    () => ({
      diffActive,
      setDiffActive,
      diffLayout,
      setDiffLayout,
      hideWhitespace,
      setHideWhitespace,
      wrapLines,
      setWrapLines,
      previewableViewMode,
      setPreviewableViewMode,
    }),
    [diffActive, diffLayout, hideWhitespace, wrapLines, previewableViewMode],
  );
}

const FileViewPreferencesContext = createContext<ReturnType<typeof usePreferencesState> | null>(
  null,
);

function PreferencesProvider({ children }: { children: ReactNode }) {
  const value = usePreferencesState();
  return (
    <FileViewPreferencesContext.Provider value={value}>
      {children}
    </FileViewPreferencesContext.Provider>
  );
}

// Responsive viewers share one preference state; standalone viewers own theirs.
export function FileViewPreferencesProvider({ children }: { children: ReactNode }) {
  const inherited = useContext(FileViewPreferencesContext);
  return inherited ? children : <PreferencesProvider>{children}</PreferencesProvider>;
}

export function useFileViewPreferences() {
  const value = useContext(FileViewPreferencesContext);
  if (!value) throw new Error("FileViewPreferencesProvider is required");
  return value;
}
