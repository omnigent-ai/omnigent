import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { confirmGoogleDocsNavigation } from "@/components/GoogleDocsPane/editorLifecycle";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { classifyGoogleDocsUrl } from "@/lib/googleDocs";
import { subscribeIdentity } from "@/lib/identity";
import { isEditorLevel } from "@/lib/permissionsApi";
import {
  addSessionDocument,
  loadSessionDocuments,
  removeSessionDocument,
  sessionDocumentsScope,
  type SessionDocumentEntry,
  type SessionDocumentsRequest,
} from "@/lib/sessionDocuments";

interface DisplayedDocument {
  originalUrl: string;
  canonicalUrl: string;
  documentId: string;
}
interface Mutation {
  request: SessionDocumentsRequest;
  open?: DisplayedDocument;
  remove?: string;
}

export interface GoogleDocsSession {
  sessionId: string;
  scope: string;
  entries: readonly SessionDocumentEntry[];
  displayed: DisplayedDocument | null;
  loading: boolean;
  busy: boolean;
  error: string | null;
  loadError: string | null;
  canEdit: boolean;
  membership: (url: string) => "linked" | "unlinked" | "unknown";
  refresh: () => void;
  addAndOpen: (url: string) => Promise<boolean>;
  openLinked: (url: string) => boolean;
  remove: (entry: SessionDocumentEntry) => Promise<boolean>;
  retry: () => void;
  registerEditor: () => () => void;
}
const Context = createContext<GoogleDocsSession | null>(null);
export const useGoogleDocsSession = () => useContext(Context);

function scopeOf(sessionId: string): string {
  try {
    return sessionDocumentsScope(sessionId);
  } catch {
    return "";
  }
}
function reference(url: string): DisplayedDocument {
  const link = classifyGoogleDocsUrl(url);
  if (link.kind !== "embed") throw new Error("Use a supported Google Docs editor URL.");
  return {
    originalUrl: url.trim(),
    canonicalUrl: link.reference.canonicalUrl,
    documentId: link.reference.documentId,
  };
}

function SessionState({
  sessionId,
  permissionLevel,
  scope,
  children,
}: {
  sessionId: string;
  permissionLevel: number | null;
  scope: string;
  children: ReactNode;
}) {
  const [boundScope, setBoundScope] = useState(scope);
  const activation = useRef({ scope, token: Symbol() });
  if (activation.current.scope !== scope) activation.current = { scope, token: Symbol() };
  const activationToken = activation.current.token;
  const [entries, setEntries] = useState<SessionDocumentEntry[]>([]);
  const [displayed, setDisplayed] = useState<DisplayedDocument | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const failure = useRef<Mutation | null>(null);
  const busyRef = useRef(false);
  const refreshPending = useRef(false);
  const mounted = useRef(false);
  const restored = useRef(false);
  const loaded = useRef(false);
  const epoch = useRef(0);
  const read = useRef<SessionDocumentsRequest | null>(null);
  const selected = useRef(displayed);
  selected.current = displayed;
  const editors = useRef(new Set<symbol>());
  if (boundScope !== scope) {
    setBoundScope(scope);
    setEntries([]);
    setDisplayed(null);
    setLoading(true);
    setBusy(false);
    setError(null);
    setLoadError(null);
    epoch.current++;
    restored.current = false;
    loaded.current = false;
    failure.current = null;
    busyRef.current = false;
    refreshPending.current = false;
    read.current = null;
  }
  const current = useCallback(
    () =>
      mounted.current &&
      activation.current.token === activationToken &&
      scopeOf(sessionId) === scope,
    [sessionId, scope, activationToken],
  );
  const canEdit = isEditorLevel(permissionLevel);
  const refresh = useCallback(async () => {
    // A new helper generation must not supersede an accepted mutation's navigation.
    if (!current()) return;
    if (busyRef.current) {
      refreshPending.current = true;
      return;
    }
    const started = epoch.current;
    const request = loadSessionDocuments(sessionId);
    read.current = request;
    const applies = () =>
      current() &&
      request === read.current &&
      started === epoch.current &&
      request.isCurrent(sessionId);
    setLoading(true);
    try {
      const result = await request.result;
      if (!applies()) return;
      loaded.current = true;
      setEntries(result.entries);
      setLoadError(null);
      // A fresh authoritative list lets the user make a new explicit decision.
      failure.current = null;
      setError(null);
      if (!restored.current) {
        restored.current = true;
        setDisplayed(result.entries[0] ? reference(result.entries[0].documentUrl) : null);
      }
    } catch (reason) {
      if (applies())
        setLoadError(reason instanceof Error ? reason.message : "Could not load documents.");
    } finally {
      if (applies()) setLoading(false);
    }
  }, [sessionId, current]);
  useEffect(() => {
    mounted.current = true;
    void refresh();
    const focus = () => {
      void refresh();
    };
    window.addEventListener("focus", focus);
    return () => {
      mounted.current = false;
      window.removeEventListener("focus", focus);
    };
  }, [refresh]);

  const confirmOpen = useCallback(
    (next: DisplayedDocument) =>
      !editors.current.size ||
      selected.current?.canonicalUrl === next.canonicalUrl ||
      confirmGoogleDocsNavigation("Switch document"),
    [],
  );
  const confirmRemove = useCallback(
    (id: string) =>
      selected.current?.documentId !== id ||
      !editors.current.size ||
      confirmGoogleDocsNavigation(
        "Remove document from this session and close its editor (the Google Doc will not be deleted)",
      ),
    [],
  );
  const perform = useCallback(
    async (mutation: Mutation): Promise<boolean> => {
      const started = ++epoch.current;
      busyRef.current = true;
      setBusy(true);
      setError(null);
      failure.current = null;
      const applies = () =>
        current() && started === epoch.current && mutation.request.isCurrent(sessionId);
      try {
        const result = await mutation.request.result;
        if (!applies()) return false;
        setEntries(result.entries);
        loaded.current = true;
        restored.current = true;
        setLoadError(null);
        if (mutation.open) setDisplayed(mutation.open);
        if (mutation.remove && selected.current?.documentId === mutation.remove) setDisplayed(null);
        return true;
      } catch (reason) {
        if (applies()) {
          failure.current = mutation;
          setError(reason instanceof Error ? reason.message : "Could not update documents.");
        }
        return false;
      } finally {
        if (current() && started === epoch.current) {
          busyRef.current = false;
          setBusy(false);
          setLoading(false);
          if (refreshPending.current) {
            refreshPending.current = false;
            void refresh();
          }
        }
      }
    },
    [current, sessionId, refresh],
  );
  const membership = useCallback(
    (url: string): "linked" | "unlinked" | "unknown" => {
      if (!loaded.current || loadError || !current()) return "unknown";
      try {
        const id = reference(url).documentId;
        return entries.some((entry) => reference(entry.documentUrl).documentId === id)
          ? "linked"
          : "unlinked";
      } catch {
        return "unknown";
      }
    },
    [loadError, current, entries],
  );
  const addAndOpen = useCallback(
    async (url: string): Promise<boolean> => {
      if (!canEdit || busyRef.current || !current()) return false;
      try {
        const next = reference(url);
        if (!confirmOpen(next)) return false;
        const pending = failure.current;
        const request =
          pending?.open?.canonicalUrl === next.canonicalUrl
            ? pending.request.retry()
            : addSessionDocument(sessionId, next.originalUrl);
        return await perform({ request, open: next });
      } catch (reason) {
        if (current())
          setError(reason instanceof Error ? reason.message : "Could not add document.");
        return false;
      }
    },
    [canEdit, current, confirmOpen, sessionId, perform],
  );
  const openLinked = useCallback(
    (url: string): boolean => {
      if (busyRef.current || !current() || membership(url) !== "linked") return false;
      const next = reference(url);
      if (!confirmOpen(next)) return false;
      restored.current = true;
      setDisplayed(next);
      setError(null);
      return true;
    },
    [current, membership, confirmOpen],
  );
  const remove = useCallback(
    async (entry: SessionDocumentEntry): Promise<boolean> => {
      if (!canEdit || busyRef.current || !current()) return false;
      const id = reference(entry.documentUrl).documentId;
      if (!confirmRemove(id)) return false;
      const pending = failure.current;
      return perform({
        request:
          pending?.remove === id
            ? pending.request.retry()
            : removeSessionDocument(sessionId, entry),
        remove: id,
      });
    },
    [canEdit, current, confirmRemove, sessionId, perform],
  );
  const retry = useCallback(() => {
    if (busyRef.current || !current()) return;
    const pending = failure.current;
    if (!pending) {
      void refresh();
      return;
    }
    if (
      !canEdit ||
      (pending.open && !confirmOpen(pending.open)) ||
      (pending.remove && !confirmRemove(pending.remove))
    )
      return;
    void perform({ ...pending, request: pending.request.retry() });
  }, [current, refresh, canEdit, confirmOpen, confirmRemove, perform]);
  const registerEditor = useCallback(() => {
    const token = Symbol();
    editors.current.add(token);
    return () => {
      editors.current.delete(token);
    };
  }, []);
  const value = useMemo<GoogleDocsSession>(
    () => ({
      sessionId,
      scope,
      entries,
      displayed,
      loading,
      busy,
      error,
      loadError,
      canEdit,
      membership,
      refresh,
      addAndOpen,
      openLinked,
      remove,
      retry,
      registerEditor,
    }),
    [
      sessionId,
      scope,
      entries,
      displayed,
      loading,
      busy,
      error,
      loadError,
      canEdit,
      membership,
      refresh,
      addAndOpen,
      openLinked,
      remove,
      retry,
      registerEditor,
    ],
  );
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

/** One transient collection and selection shared by the pane and adjacent chat actions. */
export function GoogleDocsSessionProvider({
  sessionId,
  permissionLevel,
  children,
}: {
  sessionId: string;
  permissionLevel: number | null;
  children: ReactNode;
}) {
  const scope = useSyncExternalStore(subscribeIdentity, () => scopeOf(sessionId));
  return scope ? (
    <SessionState scope={scope} sessionId={sessionId} permissionLevel={permissionLevel}>
      {children}
    </SessionState>
  ) : (
    children
  );
}

export function GoogleDocsSessionBoundary({
  sessionId,
  permissionLevel,
  children,
}: {
  sessionId?: string;
  permissionLevel: number | null;
  children: ReactNode;
}) {
  const enabled = isFeatureEnabled(useServerInfo(), "google_docs");
  return enabled && sessionId ? (
    <GoogleDocsSessionProvider sessionId={sessionId} permissionLevel={permissionLevel}>
      {children}
    </GoogleDocsSessionProvider>
  ) : (
    children
  );
}
