import { useCallback, useMemo, useSyncExternalStore } from "react";
import { useTheme } from "next-themes";
import { resolveIdentity } from "@/lib/identity";
import { getOmnigentServerIdentity } from "@/lib/host";
import {
  getSessionSummaryRevision,
  subscribeSessionSummaryChanges,
} from "@/lib/sessionSummaryChanges";
import { useNavigate } from "@/lib/routing";
import type { ExtensionCatalogItem } from "../types";
import { ExtensionHostServiceError } from "./errors";
import { grantedHostMethods } from "./registry";
import { listSessionPage, SessionReadLimiter } from "./sessions";
import {
  ExtensionStorageError,
  ExtensionStorageWriteLimiter,
  ExtensionUserStorage,
} from "./storage";

function objectParams(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ExtensionHostServiceError("InvalidParams", "Expected an object");
  }
  return value as Record<string, unknown>;
}

function throwIfAborted(signal: AbortSignal): void {
  if (signal.aborted) throw new DOMException("Host operation cancelled", "AbortError");
}

async function mapStorageErrors<T>(operation: () => Promise<T>): Promise<T> {
  try {
    return await operation();
  } catch (error) {
    if (error instanceof ExtensionStorageError) {
      throw new ExtensionHostServiceError(error.code, error.message);
    }
    throw error;
  }
}

async function storageFor(extensionId: string): Promise<ExtensionUserStorage> {
  const [userId, serverIdentity] = await Promise.all([
    resolveIdentity(),
    Promise.resolve(getOmnigentServerIdentity()),
  ]);
  if (!userId || !serverIdentity) {
    throw new ExtensionHostServiceError(
      "Unavailable",
      "Extension storage requires resolved user and server identities",
    );
  }
  return new ExtensionUserStorage(serverIdentity, userId, extensionId);
}

function pageSearch(value: unknown): string {
  if (value === undefined) return "";
  const params = objectParams(value);
  const search = new URLSearchParams();
  for (const key of Object.keys(params).sort()) {
    const item = params[key];
    if (typeof item !== "string" && typeof item !== "number" && typeof item !== "boolean") {
      throw new ExtensionHostServiceError("InvalidParams", `Page parameter ${key} is invalid`);
    }
    search.set(key, String(item));
  }
  const serialized = search.toString();
  return serialized ? `?${serialized}` : "";
}

export function useExtensionHostServices(extension: ExtensionCatalogItem) {
  const navigate = useNavigate();
  const { resolvedTheme } = useTheme();
  const theme = resolvedTheme === "dark" ? "dark" : "light";
  const writeLimiter = useMemo(() => new ExtensionStorageWriteLimiter(), []);
  const sessionReadLimiter = useMemo(() => new SessionReadLimiter(), []);
  const sessionsGranted = extension.permissions.includes("sessions.read");
  const subscribeSessionRevision = useCallback(
    (listener: () => void) =>
      sessionsGranted ? subscribeSessionSummaryChanges(listener) : () => {},
    [sessionsGranted],
  );
  const readSessionRevision = useCallback(
    () => (sessionsGranted ? getSessionSummaryRevision() : 0),
    [sessionsGranted],
  );
  const sessionRevision = useSyncExternalStore(
    subscribeSessionRevision,
    readSessionRevision,
    readSessionRevision,
  );

  const methods = useMemo(() => {
    const implementations = {
      "navigation.openPage": (params: unknown, signal: AbortSignal) => {
        throwIfAborted(signal);
        const input = objectParams(params);
        const pageId = input.pageId;
        if (typeof pageId !== "string") {
          throw new ExtensionHostServiceError("InvalidParams", "pageId is required");
        }
        const page = extension.pages.find((item) => item.id === pageId);
        if (!page) {
          throw new ExtensionHostServiceError("PermissionDenied", "Page is not owned by extension");
        }
        navigate({
          pathname: `/extensions/${extension.id}/${page.route}`,
          search: pageSearch(input.params),
        });
        return null;
      },
      "navigation.openSession": (params: unknown, signal: AbortSignal) => {
        throwIfAborted(signal);
        const sessionId = objectParams(params).sessionId;
        if (typeof sessionId !== "string" || !sessionId || sessionId.length > 256) {
          throw new ExtensionHostServiceError("InvalidParams", "sessionId is invalid");
        }
        navigate(`/c/${encodeURIComponent(sessionId)}`);
        return null;
      },
      "navigation.openNewSession": (_params: unknown, signal: AbortSignal) => {
        throwIfAborted(signal);
        navigate("/");
        return null;
      },
      "theme.getCurrent": (_params: unknown, signal: AbortSignal) => {
        throwIfAborted(signal);
        return { theme };
      },
      "theme.subscribe": (_params: unknown, signal: AbortSignal) => {
        throwIfAborted(signal);
        return { theme };
      },
      "storage.user.get": async (params: unknown, signal: AbortSignal) => {
        const storage = await storageFor(extension.id);
        return mapStorageErrors(() => storage.get(objectParams(params).key, signal));
      },
      "storage.user.set": async (params: unknown, signal: AbortSignal) => {
        const input = objectParams(params);
        return writeLimiter.run(signal, async () => {
          const storage = await storageFor(extension.id);
          await mapStorageErrors(() => storage.set(input.key, input.value, signal));
          return null;
        });
      },
      "storage.user.delete": async (params: unknown, signal: AbortSignal) =>
        writeLimiter.run(signal, async () => {
          const storage = await storageFor(extension.id);
          await mapStorageErrors(() => storage.delete(objectParams(params).key, signal));
          return null;
        }),
      "sessions.listPage": (params: unknown, signal: AbortSignal) =>
        sessionReadLimiter.run(signal, () => listSessionPage(params, signal)),
      "sessions.subscribe": (_params: unknown, signal: AbortSignal) => {
        throwIfAborted(signal);
        return { revision: sessionRevision };
      },
    };
    return grantedHostMethods(extension, implementations);
  }, [extension, navigate, sessionReadLimiter, sessionRevision, theme, writeLimiter]);
  const themeEvent = useMemo(() => ({ theme }), [theme]);
  const sessionEvent = useMemo(() => ({ revision: sessionRevision }), [sessionRevision]);
  const events = useMemo(
    () => ({
      "theme.changed": themeEvent,
      ...(sessionsGranted ? { "sessions.changed": sessionEvent } : {}),
    }),
    [sessionEvent, sessionsGranted, themeEvent],
  );
  return { methods, events };
}
