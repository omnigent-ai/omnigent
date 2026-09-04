import { useMemo, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useTheme } from "next-themes";
import { resolveIdentity } from "@/lib/identity";
import { getOmnigentServerIdentity } from "@/lib/host";
import { fetchGithubInfo } from "@/hooks/useGithub";
import { useNavigate } from "@/lib/routing";
import type { ExtensionCatalogItem, ExtensionPullRequest } from "../types";
import { ExtensionHostServiceError } from "./errors";
import { grantedHostMethods } from "./registry";
import {
  cachedProjectSummaries,
  createProjectSummary,
  listProjectSummaries,
  parseCreateProjectParams,
} from "./projects";
import { cachedInitialSessionPage, listSessionPage, SessionReadLimiter } from "./sessions";
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
  const queryClient = useQueryClient();
  const theme = resolvedTheme === "dark" ? "dark" : "light";
  const writeLimiter = useMemo(() => new ExtensionStorageWriteLimiter(), []);
  const sessionListLimiter = useMemo(() => new SessionReadLimiter(), []);
  const pullRequestLimiter = useMemo(() => new SessionReadLimiter(), []);
  const servedCachedInitialSessionsRef = useRef(false);
  // External URLs the host has handed to this extension; the only ones it may open.
  const externalUrlsRef = useRef(new Set<string>());

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
      "navigation.openExternal": async (params: unknown, signal: AbortSignal) => {
        throwIfAborted(signal);
        const url = objectParams(params).url;
        if (typeof url !== "string" || !externalUrlsRef.current.has(url)) {
          throw new ExtensionHostServiceError(
            "PermissionDenied",
            "URL was not provided by the host",
          );
        }
        window.open(url, "_blank", "noopener,noreferrer");
        return null;
      },
      "navigation.openNewSession": async (params: unknown, signal: AbortSignal) => {
        throwIfAborted(signal);
        const projectId = params === undefined ? undefined : objectParams(params).projectId;
        if (projectId === undefined || projectId === null) {
          navigate("/");
          return null;
        }
        if (typeof projectId !== "string" || !projectId || projectId.length > 256) {
          throw new ExtensionHostServiceError("InvalidParams", "projectId is invalid");
        }
        // The composer takes the project by name (`?project=`), so resolve it here.
        const project = (await listProjectSummaries()).find((item) => item.id === projectId);
        throwIfAborted(signal);
        if (!project) throw new ExtensionHostServiceError("InvalidParams", "Project not found");
        navigate({ pathname: "/", search: `?project=${encodeURIComponent(project.name)}` });
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
      "sessions.listPage": (params: unknown, signal: AbortSignal) => {
        throwIfAborted(signal);
        if (!servedCachedInitialSessionsRef.current) {
          const cached = cachedInitialSessionPage(queryClient, params);
          if (cached) {
            servedCachedInitialSessionsRef.current = true;
            return cached;
          }
        }
        return sessionListLimiter.run(signal, () => listSessionPage(params, signal));
      },
      "sessions.pullRequest": (params: unknown, signal: AbortSignal) =>
        pullRequestLimiter.run(signal, async (): Promise<ExtensionPullRequest | null> => {
          const sessionId = objectParams(params).sessionId;
          if (typeof sessionId !== "string" || !sessionId || sessionId.length > 256) {
            throw new ExtensionHostServiceError("InvalidParams", "sessionId is invalid");
          }
          // Shares the GitHub panel's cache so the runner is asked at most every 30s per session.
          const info = await queryClient.fetchQuery({
            queryKey: ["github-info", sessionId],
            queryFn: () => fetchGithubInfo(sessionId),
            staleTime: 30_000,
          });
          const pr = info.available ? info.pr : null;
          if (!pr || !/^https:\/\//.test(pr.url)) return null;
          externalUrlsRef.current.add(pr.url);
          return { number: pr.number, title: pr.title.slice(0, 256), state: pr.state, url: pr.url };
        }),
      "projects.list": (_params: unknown, signal: AbortSignal) => {
        throwIfAborted(signal);
        return cachedProjectSummaries(queryClient) ?? listProjectSummaries();
      },
      "projects.create": async (params: unknown, signal: AbortSignal) => {
        const name = parseCreateProjectParams(params);
        throwIfAborted(signal);
        const project = await createProjectSummary(name);
        // The sidebar caches its project list; refresh it so the new project shows there too.
        void queryClient.invalidateQueries({ queryKey: ["projects"] });
        return project;
      },
    };
    return grantedHostMethods(extension, implementations);
  }, [
    extension,
    navigate,
    pullRequestLimiter,
    queryClient,
    sessionListLimiter,
    theme,
    writeLimiter,
  ]);
  const events = useMemo(() => ({ "theme.changed": { theme } }), [theme]);
  return { methods, events };
}
