import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type SetStateAction,
} from "react";
import { useLocation, useSearchParams } from "@/lib/routing";
import { readFileViewPreferences, writeFileViewPreferences } from "@/lib/fileViewPreferences";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface DraftGuard {
  isDirty: () => boolean;
}

function usePreferencesState() {
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const [previousLocationKey, setPreviousLocationKey] = useState(location.key);
  const [initial] = useState(readFileViewPreferences);
  const [diffActive, setDiffActiveValue] = useState(
    () => searchParams.get("diff") === "1" || initial.diffActive,
  );
  const [diffLayout, setDiffLayout] = useState(initial.diffLayout);
  const [hideWhitespace, setHideWhitespace] = useState(initial.hideWhitespace);
  const [wrapLines, setWrapLines] = useState(initial.wrapLines);
  const [previewableViewMode, setPreviewableViewModeValue] = useState(initial.previewableViewMode);
  const draftGuards = useRef(new Set<DraftGuard>());
  const applyingConfirmedChange = useRef(false);
  const [pendingChange, setPendingChange] = useState<{ apply: () => void } | null>(null);
  const registerDraftGuard = useCallback((guard: DraftGuard) => {
    draftGuards.current.add(guard);
    return () => {
      draftGuards.current.delete(guard);
    };
  }, []);
  const guardViewChange = useCallback((apply: () => void) => {
    if (
      !applyingConfirmedChange.current &&
      [...draftGuards.current].some((guard) => guard.isDirty())
    ) {
      setPendingChange({ apply });
    } else {
      apply();
    }
  }, []);
  const setDiffActive = useCallback(
    (next: SetStateAction<boolean>) => guardViewChange(() => setDiffActiveValue(next)),
    [guardViewChange],
  );
  const setPreviewableViewMode = useCallback(
    (next: SetStateAction<typeof previewableViewMode>) =>
      guardViewChange(() => setPreviewableViewModeValue(next)),
    [guardViewChange],
  );

  // Resolve incoming mode requests before viewers sync the URL. A pending
  // confirmation pauses that sync; cancelling lets the current mode restore it.
  if (previousLocationKey !== location.key) {
    setPreviousLocationKey(location.key);
    setPendingChange(null);
    if (searchParams.get("diff") === "1" && !diffActive) setDiffActive(true);
  }

  useEffect(() => {
    writeFileViewPreferences({
      diffActive,
      diffLayout,
      hideWhitespace,
      wrapLines,
      previewableViewMode,
    });
  }, [diffActive, diffLayout, hideWhitespace, wrapLines, previewableViewMode]);

  const value = useMemo(
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
      registerDraftGuard,
      guardViewChange,
      viewChangePending: pendingChange !== null,
    }),
    [
      diffActive,
      setDiffActive,
      diffLayout,
      hideWhitespace,
      wrapLines,
      previewableViewMode,
      setPreviewableViewMode,
      registerDraftGuard,
      guardViewChange,
      pendingChange,
    ],
  );
  const discardAndApply = () => {
    // Nested setters share this confirmation. Editors that remain mounted
    // retain their dirty guards; exiting editor mode clears the others.
    applyingConfirmedChange.current = true;
    try {
      pendingChange?.apply();
    } finally {
      applyingConfirmedChange.current = false;
      setPendingChange(null);
    }
  };
  return { value, cancel: () => setPendingChange(null), discardAndApply };
}

const FileViewPreferencesContext = createContext<
  ReturnType<typeof usePreferencesState>["value"] | null
>(null);

function PreferencesProvider({ children }: { children: ReactNode }) {
  const { value, cancel, discardAndApply } = usePreferencesState();
  return (
    <FileViewPreferencesContext.Provider value={value}>
      {children}
      <Dialog open={value.viewChangePending} onOpenChange={(open) => !open && cancel()}>
        <DialogContent showCloseButton={false}>
          <DialogHeader>
            <DialogTitle>Unsaved changes</DialogTitle>
            <DialogDescription>
              Switching views will discard unsaved edits in the file viewers.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={cancel}>
              Keep editing
            </Button>
            <Button variant="destructive" onClick={discardAndApply}>
              Discard changes
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </FileViewPreferencesContext.Provider>
  );
}

// Responsive viewers share one preference state; standalone panels own theirs.
export function FileViewPreferencesProvider({ children }: { children: ReactNode }) {
  const inherited = useContext(FileViewPreferencesContext);
  return inherited ? children : <PreferencesProvider>{children}</PreferencesProvider>;
}

export function useFileViewPreferences() {
  const value = useContext(FileViewPreferencesContext);
  if (!value) throw new Error("FileViewPreferencesProvider is required");
  return value;
}
