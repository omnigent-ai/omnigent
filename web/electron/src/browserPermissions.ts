import { isLocalhostUrl } from "./localhost_cors.ts";

const LOCAL_NETWORK_PERMISSIONS: ReadonlySet<string> = new Set([
  "local-network-access",
  "local-network",
  "loopback-network",
]);

/** The parts of Electron's permission handler details this policy reads. */
export interface PermissionDetails {
  requestingUrl?: string;
  embeddingOrigin?: string;
  isMainFrame?: boolean;
}

type PermissionCallback = (granted: boolean) => void;

/** Electron calls these with the requesting webContents, or null for a context-free check. */
export type PermissionRequestHandler = (
  contents: unknown,
  permission: string,
  callback: PermissionCallback,
  details?: PermissionDetails,
) => void;
export type PermissionCheckHandler = (
  contents: unknown,
  permission: string,
  requestingOrigin: string,
  details?: PermissionDetails,
) => boolean;

/** The storage partition's session, which owns one handler of each kind. */
export interface PermissionSession {
  setPermissionRequestHandler: (handler: PermissionRequestHandler) => void;
  setPermissionCheckHandler: (handler: PermissionCheckHandler) => void;
}

type NavigationStartListener = (
  event: unknown,
  url: string,
  isInPlace: boolean,
  isMainFrame: boolean,
) => void;

/** The browser view's webContents one policy attaches to. */
export interface PermissionWebContents {
  isDestroyed: () => boolean;
  getURL: () => string;
  reload: () => void;
  on: ((event: "did-start-navigation", listener: NavigationStartListener) => unknown) &
    ((event: "did-navigate", listener: (event: unknown, url: string) => void) => unknown) &
    ((event: "render-process-gone", listener: () => void) => unknown);
  once: (event: "destroyed", listener: () => void) => unknown;
}

interface TabPolicy {
  request: (
    contents: unknown,
    permission: string,
    callback: PermissionCallback,
    details?: PermissionDetails,
  ) => void;
  check: (
    contents: unknown,
    permission: string,
    requestingOrigin: string,
    details?: PermissionDetails,
  ) => boolean;
  checkSaved: (
    permission: string,
    requestingOrigin: string,
    details?: PermissionDetails,
  ) => boolean;
  canPrompt: () => boolean;
}

const sessionPolicies = new WeakMap<PermissionSession, Map<unknown, TabPolicy>>();

// Electron installs handlers per storage partition, but consent and prompts
// belong to the requesting tab, even when several tabs share that partition.
function policiesForSession(session: PermissionSession): Map<unknown, TabPolicy> {
  const existing = sessionPolicies.get(session);
  if (existing) return existing;
  const policies = new Map<unknown, TabPolicy>();
  session.setPermissionRequestHandler((contents, permission, callback, details) => {
    const policy = policies.get(contents);
    if (policy) policy.request(contents, permission, callback, details);
    else {
      try {
        callback(false);
      } catch {
        // Chromium may have discarded the request during teardown.
      }
    }
  });
  session.setPermissionCheckHandler((contents, permission, origin, details) => {
    // Context-free checks can only be attributed to the foreground tab.
    const policy = contents
      ? policies.get(contents)
      : [...policies.values()].find((candidate) => candidate.canPrompt());
    if (policy?.check(contents, permission, origin, details)) return true;
    // Unknown attributed views stay denied. Context-free checks may use saved
    // site grants, but cannot borrow hidden one-visit consent or open a prompt.
    return (
      !contents && [...policies.values()].some((p) => p.checkSaved(permission, origin, details))
    );
  });
  sessionPolicies.set(session, policies);
  return policies;
}

function permissionOrigin(value: string | undefined): string | null {
  // `new URL(undefined)` throws, so a missing value has no origin.
  if (value === undefined) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" || isLocalhostUrl(value) ? url.origin : null;
  } catch {
    return null;
  }
}

export interface BrowserPermissionStoreDeps {
  loadSettings: () => Record<string, unknown>;
  saveSettings: (settings: Record<string, unknown>) => void;
}

/** Saved per-origin decisions, with a revision that changes on every save. */
export interface BrowserPermissionStore {
  get: (origin: string) => boolean | undefined;
  revision: (origin: string) => number;
  /** Save `decision`, or forget the saved one when it is undefined. */
  set: (origin: string, decision: boolean | undefined) => void;
}

/** Saved site decisions apply across conversation browsers, not the shell. */
export function createBrowserPermissionStore({
  loadSettings,
  saveSettings,
}: BrowserPermissionStoreDeps): BrowserPermissionStore {
  const revisions = new Map<string, number>();
  return {
    get(origin) {
      const saved = loadSettings().browser_local_network_permissions;
      const value = saved == null ? undefined : (saved as Record<string, unknown>)[origin];
      return typeof value === "boolean" ? value : undefined;
    },
    revision: (origin) => revisions.get(origin) ?? 0,
    set(origin, decision) {
      if (permissionOrigin(origin) !== origin) throw new Error("Invalid permission origin");
      if (decision !== undefined && typeof decision !== "boolean") {
        throw new Error("Invalid permission decision");
      }
      const settings = loadSettings();
      const saved = settings.browser_local_network_permissions;
      const entries =
        saved && typeof saved === "object" && !Array.isArray(saved) ? Object.entries(saved) : [];
      const choices: Record<string, unknown> = Object.fromEntries(
        entries.filter(([key]) => key !== origin),
      );
      if (decision !== undefined) choices[origin] = decision;
      settings.browser_local_network_permissions = choices;
      saveSettings(settings);
      // Changing a saved choice also invalidates older one-visit grants.
      revisions.set(origin, (revisions.get(origin) ?? 0) + 1);
    },
  };
}

/** What the consent prompt shows; `signal` aborts when the request goes stale. */
export interface PermissionPromptRequest {
  origin: string;
  reload: boolean;
  signal: AbortSignal;
}

export interface BrowserPermissionOptions<Contents extends PermissionWebContents> {
  canPrompt: (contents: Contents | null) => boolean;
  /** Resolves the user's choice; anything but a known choice counts as a dismissal. */
  showPrompt: (request: PermissionPromptRequest) => Promise<string>;
  store: BrowserPermissionStore;
}

export interface BrowserPermissionPolicy<Contents extends PermissionWebContents> {
  attach: (webContents: Contents) => void;
}

interface PermissionContext<Contents> {
  origin: string;
  generation: number;
  webContents: Contents;
}

interface PendingPrompt<Contents> extends PermissionContext<Contents> {
  controller: AbortController;
  storeRevision: number;
  result: Promise<boolean>;
}

/** Install before constructing the view, then attach exactly once to its webContents. */
export function registerBrowserPermissions<Contents extends PermissionWebContents>(
  session: PermissionSession,
  { canPrompt, showPrompt, store }: BrowserPermissionOptions<Contents>,
): BrowserPermissionPolicy<Contents> {
  const policies = policiesForSession(session);
  const visits = new Map<string, { granted: boolean; revision: number }>();
  let contents: Contents | null = null;
  let generation = 0;
  let dismissedGeneration: number | null = null;
  let pending: PendingPrompt<Contents> | null = null;

  function decisionFor(origin: string): boolean | undefined {
    const saved = store.get(origin);
    if (saved !== undefined) return saved;
    const visit = visits.get(origin);
    return visit?.revision === store.revision(origin) ? visit.granted : undefined;
  }

  function context(
    webContents: unknown,
    requestingOrigin: string | undefined,
    details: PermissionDetails,
    check = false,
  ): PermissionContext<Contents> | null {
    const origin = permissionOrigin(requestingOrigin);
    if (!origin || !contents || contents.isDestroyed()) return null;
    if (origin !== permissionOrigin(contents.getURL())) return null;
    if (webContents) {
      if (webContents !== contents || details.isMainFrame !== true) return null;
    } else {
      // Context-free checks must match both the embedding and current origin.
      if (!check || permissionOrigin(details.embeddingOrigin) !== origin) return null;
    }
    if (details.embeddingOrigin && permissionOrigin(details.embeddingOrigin) !== origin) {
      return null;
    }
    return { origin, generation, webContents: contents };
  }

  function isCurrent(ctx: PermissionContext<Contents>): boolean {
    return (
      contents !== null &&
      ctx.webContents === contents &&
      !contents.isDestroyed() &&
      ctx.generation === generation &&
      permissionOrigin(contents.getURL()) === ctx.origin
    );
  }

  function ask(ctx: PermissionContext<Contents> | null, reload: boolean): Promise<boolean> {
    if (!ctx || !isCurrent(ctx)) return Promise.resolve(false);
    const decision = decisionFor(ctx.origin);
    if (decision !== undefined) return Promise.resolve(decision);
    // Visibility gates new consent prompts, not an origin's existing grant.
    if (!canPrompt(contents)) return Promise.resolve(false);
    if (dismissedGeneration === generation) return Promise.resolve(false);
    if (pending) {
      return pending.origin === ctx.origin && pending.generation === ctx.generation
        ? pending.result
        : Promise.resolve(false);
    }

    const controller = new AbortController();
    const storeRevision = store.revision(ctx.origin);
    // The chain starts on a later tick, after `pending` holds this request.
    const result = Promise.resolve()
      .then((): Promise<string> | string => {
        if (!isCurrent(ctx) || !canPrompt(contents)) return "dismiss";
        return showPrompt({ origin: ctx.origin, reload, signal: controller.signal });
      })
      .then((choice) => {
        if (!isCurrent(ctx) || !canPrompt(contents)) return false;
        // Another shell window may have saved a decision while this prompt was
        // open. Never let this stale answer overwrite that newer choice.
        if (store.revision(ctx.origin) !== storeRevision) return false;
        if (choice === "always-allow" || choice === "deny") {
          store.set(ctx.origin, choice === "always-allow");
          visits.delete(ctx.origin);
        } else if (choice === "allow-once") {
          visits.set(ctx.origin, { granted: true, revision: storeRevision });
        } else {
          dismissedGeneration = generation;
          return false;
        }
        const granted = choice !== "deny";
        // isCurrent(ctx) holds, so ctx.webContents is the attached contents.
        if (granted && reload) ctx.webContents.reload();
        return granted;
      })
      .catch(() => {
        if (isCurrent(ctx)) dismissedGeneration = generation;
        return false;
      })
      .finally(() => {
        if (pending === request) pending = null;
      });
    const request: PendingPrompt<Contents> = { ...ctx, controller, storeRevision, result };
    pending = request;
    return result;
  }

  function handleRequest(
    webContents: unknown,
    permission: string,
    callback: PermissionCallback,
    details: PermissionDetails = {},
  ): void {
    if (!LOCAL_NETWORK_PERMISSIONS.has(permission)) {
      callback(false);
      return;
    }
    const ctx = context(webContents, details.requestingUrl, details);
    void ask(ctx, false).then((granted) => {
      try {
        callback(!!(granted && ctx && isCurrent(ctx)));
      } catch {
        // Chromium may have discarded the request during navigation/teardown.
      }
    });
  }

  function handleCheck(
    webContents: unknown,
    permission: string,
    requestingOrigin: string,
    details: PermissionDetails = {},
  ): boolean {
    if (!LOCAL_NETWORK_PERMISSIONS.has(permission)) return false;
    const ctx = context(webContents, requestingOrigin, details, true);
    if (!ctx) return false;
    const decision = decisionFor(ctx.origin);
    if (decision !== undefined) return decision;
    // Electron's synchronous check cannot return "prompt". Query-first auth
    // flows need a reload after the user approves the request.
    void ask(ctx, true);
    return false;
  }

  function checkSaved(
    permission: string,
    requestingOrigin: string,
    details: PermissionDetails = {},
  ): boolean {
    if (!LOCAL_NETWORK_PERMISSIONS.has(permission)) return false;
    const ctx = context(null, requestingOrigin, details, true);
    return !!ctx && store.get(ctx.origin) === true;
  }

  return {
    attach(webContents) {
      if (contents) throw new Error("Browser permission policy is already attached");
      const attached = webContents;
      contents = attached;
      policies.set(attached, {
        request: handleRequest,
        check: handleCheck,
        checkSaved,
        canPrompt: () => canPrompt(contents),
      });
      attached.on("did-start-navigation", (_event, url, isInPlace, isMainFrame) => {
        if (!isMainFrame || isInPlace) return;
        generation++;
        pending?.controller.abort();
        if (permissionOrigin(url) !== permissionOrigin(attached.getURL())) visits.clear();
      });
      attached.on("did-navigate", (_event, url) => {
        for (const origin of visits.keys()) {
          if (origin !== permissionOrigin(url)) visits.delete(origin);
        }
      });
      const invalidateVisit = () => {
        generation++;
        pending?.controller.abort();
        visits.clear();
      };
      attached.on("render-process-gone", invalidateVisit);
      attached.once("destroyed", () => {
        policies.delete(attached);
        invalidateVisit();
      });
    },
  };
}
